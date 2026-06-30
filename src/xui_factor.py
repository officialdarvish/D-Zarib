#!/usr/bin/env python3
"""
xui-factor: a standalone sidecar for 3x-ui / x-ui inbound traffic multipliers.

It does not import or modify 3x-ui source. It only creates tables prefixed with
xui_factor_ and updates client_traffics.up/down with the extra billed traffic.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import hashlib
import os
import re
import shlex
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

APP_NAME = "xui-factor"
DEFAULT_CONFIG = "/etc/xui-factor/config.env"
DEFAULT_SQLITE_PATH = "/etc/x-ui/x-ui.db"
DEFAULT_INSTALL_URL = "https://raw.githubusercontent.com/officialdarvish/D-Zarib/main/install.sh"


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}


def parse_env_file(path: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        try:
            parts = shlex.split(val, posix=True)
            env[key] = parts[0] if len(parts) == 1 else val
        except ValueError:
            env[key] = val.strip('"').strip("'")
    return env


def load_config(path: str) -> Dict[str, str]:
    cfg = parse_env_file(path)
    # Environment variables override config file values for systemd/manual runs.
    for key in [
        "DB_TYPE",
        "SQLITE_PATH",
        "POSTGRES_DSN",
        "POLL_INTERVAL_SECONDS",
        "POLLING_BILLING_ENABLED",
        "STRICT_SINGLE_INBOUND",
        "ALLOW_MULTI_INBOUND_BEST_EFFORT",
        "CHARGE_DIRECTION",
        "LOG_LEVEL",
        "WEBHOOK_HOST",
        "WEBHOOK_PORT",
        "WEBHOOK_PATH",
        "WEBHOOK_TOKEN",
        "RUN_MODE",
        "ACTIVE_INBOUND_STRICT",
        "POLLING_BILLING_ENABLED",
    ]:
        if os.getenv(key) is not None:
            cfg[key] = os.environ[key]
    cfg.setdefault("DB_TYPE", "sqlite")
    cfg.setdefault("SQLITE_PATH", DEFAULT_SQLITE_PATH)
    cfg.setdefault("POLL_INTERVAL_SECONDS", "6")
    cfg.setdefault("STRICT_SINGLE_INBOUND", "true")
    cfg.setdefault("ALLOW_MULTI_INBOUND_BEST_EFFORT", "false")
    cfg.setdefault("CHARGE_DIRECTION", "proportional")
    cfg.setdefault("LOG_LEVEL", "info")
    cfg.setdefault("WEBHOOK_HOST", "127.0.0.1")
    cfg.setdefault("WEBHOOK_PORT", "19090")
    cfg.setdefault("WEBHOOK_PATH", "/xui-factor/hook")
    cfg.setdefault("WEBHOOK_TOKEN", "")
    cfg.setdefault("RUN_MODE", "serve")
    # Safety migration: old configs may still contain RUN_MODE=run/polling.
    # Accurate inbound-specific billing must use webhook mode; polling cannot reliably
    # know which inbound produced a client email traffic change.
    if str(cfg.get("RUN_MODE", "serve")).lower() in {"run", "poll", "polling"}:
        cfg["RUN_MODE"] = "serve"
        cfg["POLLING_BILLING_ENABLED"] = "false"
    # In webhook mode, only charge when the factored inbound tag is present in the same traffic scan.
    # This prevents a factor from leaking to another inbound that shares the same client email.
    cfg.setdefault("ACTIVE_INBOUND_STRICT", "true")
    # Polling cannot reliably attribute email-level traffic to a specific inbound in 3x-ui.
    # Keep it disabled by default to prevent over-billing. Accurate billing uses webhook mode.
    cfg.setdefault("POLLING_BILLING_ENABLED", "false")
    return cfg


class Logger:
    levels = {"debug": 10, "info": 20, "warning": 30, "error": 40}

    def __init__(self, level: str = "info"):
        self.level = self.levels.get(level.lower(), 20)

    def _log(self, level: str, msg: str) -> None:
        if self.levels[level] >= self.level:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}] {level.upper():7s} {msg}", flush=True)

    def debug(self, msg: str) -> None:
        self._log("debug", msg)

    def info(self, msg: str) -> None:
        self._log("info", msg)

    def warning(self, msg: str) -> None:
        self._log("warning", msg)

    def error(self, msg: str) -> None:
        self._log("error", msg)


class DB:
    def __init__(self, cfg: Dict[str, str]):
        self.cfg = cfg
        self.kind = cfg.get("DB_TYPE", "sqlite").strip().lower()
        if self.kind in {"postgresql", "pg"}:
            self.kind = "postgres"
        if self.kind not in {"sqlite", "postgres"}:
            raise SystemExit(f"Unsupported DB_TYPE={self.kind}; use sqlite or postgres")
        self.conn: Any = None

    @property
    def param(self) -> str:
        return "%s" if self.kind == "postgres" else "?"

    def adapt_sql(self, sql: str) -> str:
        if self.kind == "postgres":
            return sql.replace("?", "%s")
        return sql

    def connect(self) -> "DB":
        if self.kind == "sqlite":
            db_path = self.cfg.get("SQLITE_PATH", DEFAULT_SQLITE_PATH)
            if not db_path:
                raise SystemExit("SQLITE_PATH is empty")
            self.conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.execute("PRAGMA foreign_keys=OFF")
            return self

        dsn = self.cfg.get("POSTGRES_DSN", "").strip()
        if not dsn:
            raise SystemExit("POSTGRES_DSN is empty")
        try:
            import psycopg2  # type: ignore
            import psycopg2.extras  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on host packages
            raise SystemExit(
                "PostgreSQL mode needs psycopg2. Install with: apt install python3-psycopg2 "
                "or pip install psycopg2-binary"
            ) from exc
        self.conn = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
        self.conn.autocommit = False
        # Do not let menu/reset operations hang forever behind a database lock.
        # 3x-ui may write traffic counters while the operator is using the menu.
        # Short lock timeouts make failures explicit instead of freezing the terminal.
        try:
            cur = self.conn.cursor()
            cur.execute("SET lock_timeout TO '5s'")
            cur.execute("SET statement_timeout TO '30s'")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
        return self

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()

    def commit(self) -> None:
        if self.conn is not None:
            self.conn.commit()

    @contextlib.contextmanager
    def tx(self):
        if self.kind == "sqlite":
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()
        else:
            try:
                yield self
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self.kind == "sqlite":
            return self.conn.execute(sql, tuple(params))
        cur = self.conn.cursor()
        cur.execute(self.adapt_sql(sql), tuple(params))
        return cur

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        if self.kind == "sqlite":
            self.conn.executemany(sql, list(seq))
        else:
            cur = self.conn.cursor()
            cur.executemany(self.adapt_sql(sql), list(seq))

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        cur = self.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row is not None else None

    def table_exists(self, name: str) -> bool:
        if self.kind == "sqlite":
            row = self.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        else:
            row = self.fetchone(
                "SELECT table_name AS name FROM information_schema.tables WHERE table_schema='public' AND table_name=?",
                (name,),
            )
        return row is not None


@dataclass
class Candidate:
    email: str
    up: int
    down: int
    inbound_id: int
    inbound_tag: str
    factor: Decimal
    client_id: int
    client_inbound_count: int
    factor_note: str = ""


def qbool(db: DB, value: bool) -> Any:
    if db.kind == "postgres":
        return value
    return 1 if value else 0


def ensure_schema(db: DB) -> None:
    if db.kind == "sqlite":
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS xui_factor_inbounds (
              inbound_id INTEGER PRIMARY KEY,
              factor REAL NOT NULL CHECK (factor >= 1.0),
              enabled INTEGER NOT NULL DEFAULT 1,
              note TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS xui_factor_state (
              email TEXT PRIMARY KEY,
              last_raw_up INTEGER NOT NULL DEFAULT 0,
              last_raw_down INTEGER NOT NULL DEFAULT 0,
              extra_up INTEGER NOT NULL DEFAULT 0,
              extra_down INTEGER NOT NULL DEFAULT 0,
              last_factor REAL NOT NULL DEFAULT 1.0,
              last_inbound_id INTEGER,
              skipped_reason TEXT NOT NULL DEFAULT '',
              updated_at INTEGER NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS xui_factor_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              email TEXT NOT NULL,
              inbound_id INTEGER,
              factor REAL NOT NULL,
              raw_delta_up INTEGER NOT NULL,
              raw_delta_down INTEGER NOT NULL,
              extra_up INTEGER NOT NULL,
              extra_down INTEGER NOT NULL,
              created_at INTEGER NOT NULL,
              note TEXT NOT NULL DEFAULT ''
            )
            """
        )
    else:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS xui_factor_inbounds (
              inbound_id INTEGER PRIMARY KEY,
              factor DOUBLE PRECISION NOT NULL CHECK (factor >= 1.0),
              enabled BOOLEAN NOT NULL DEFAULT TRUE,
              note TEXT NOT NULL DEFAULT '',
              created_at BIGINT NOT NULL,
              updated_at BIGINT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS xui_factor_state (
              email TEXT PRIMARY KEY,
              last_raw_up BIGINT NOT NULL DEFAULT 0,
              last_raw_down BIGINT NOT NULL DEFAULT 0,
              extra_up BIGINT NOT NULL DEFAULT 0,
              extra_down BIGINT NOT NULL DEFAULT 0,
              last_factor DOUBLE PRECISION NOT NULL DEFAULT 1.0,
              last_inbound_id INTEGER,
              skipped_reason TEXT NOT NULL DEFAULT '',
              updated_at BIGINT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS xui_factor_events (
              id BIGSERIAL PRIMARY KEY,
              email TEXT NOT NULL,
              inbound_id INTEGER,
              factor DOUBLE PRECISION NOT NULL,
              raw_delta_up BIGINT NOT NULL,
              raw_delta_down BIGINT NOT NULL,
              extra_up BIGINT NOT NULL,
              extra_down BIGINT NOT NULL,
              created_at BIGINT NOT NULL,
              note TEXT NOT NULL DEFAULT ''
            )
            """
        )
    if db.kind == "sqlite":
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS xui_factor_webhook_seen (
              fingerprint TEXT PRIMARY KEY,
              created_at INTEGER NOT NULL
            )
            """
        )
    else:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS xui_factor_webhook_seen (
              fingerprint TEXT PRIMARY KEY,
              created_at BIGINT NOT NULL
            )
            """
        )
    db.execute("CREATE INDEX IF NOT EXISTS idx_xui_factor_state_inbound ON xui_factor_state(last_inbound_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_xui_factor_events_created ON xui_factor_events(created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_xui_factor_seen_created ON xui_factor_webhook_seen(created_at)")


def require_3xui_tables(db: DB) -> None:
    required = ["inbounds", "clients", "client_inbounds", "client_traffics"]
    missing = [t for t in required if not db.table_exists(t)]
    if missing:
        raise SystemExit("3x-ui DB schema not found. Missing tables: " + ", ".join(missing))


def list_inbounds(db: DB) -> List[Dict[str, Any]]:
    rows = db.fetchall(
        """
        SELECT
          i.id,
          i.tag,
          i.remark,
          i.protocol,
          i.enable,
          i.up,
          i.down,
          COALESCE(cnt.client_count, 0) AS client_count,
          f.factor,
          f.enabled AS factor_enabled,
          f.note AS factor_note
        FROM inbounds i
        LEFT JOIN (
          SELECT inbound_id, COUNT(*) AS client_count
          FROM client_inbounds
          GROUP BY inbound_id
        ) cnt ON cnt.inbound_id = i.id
        LEFT JOIN xui_factor_inbounds f ON f.inbound_id = i.id
        ORDER BY i.id ASC
        """
    )
    return rows


def print_table(rows: List[Dict[str, Any]], columns: List[Tuple[str, str]]) -> None:
    if not rows:
        print("No rows.")
        return
    widths: Dict[str, int] = {}
    for key, title in columns:
        widths[key] = max(len(title), *(len(str(row.get(key, ""))) for row in rows))
    header = "  ".join(title.ljust(widths[key]) for key, title in columns)
    print(header)
    print("  ".join("-" * widths[key] for key, _ in columns))
    for row in rows:
        print("  ".join(str(row.get(key, "")).ljust(widths[key]) for key, _ in columns))


def cmd_list_inbounds(db: DB) -> None:
    rows = list_inbounds(db)
    for r in rows:
        r["enable"] = "on" if parse_bool(r.get("enable")) else "off"
        factor = r.get("factor")
        r["factor"] = "-" if factor is None else f"{float(factor):.4g}"
        fe = r.get("factor_enabled")
        r["factor_enabled"] = "-" if fe is None else ("on" if parse_bool(fe) else "off")
    print_table(
        rows,
        [
            ("id", "ID"),
            ("tag", "Tag"),
            ("remark", "Remark"),
            ("protocol", "Proto"),
            ("enable", "Inbound"),
            ("client_count", "Clients"),
            ("factor", "Factor"),
            ("factor_enabled", "FactorOn"),
        ],
    )


def set_factor(db: DB, inbound_id: int, factor: Decimal, note: str = "", enabled: bool = True) -> None:
    if factor < Decimal("1.0"):
        raise SystemExit("factor must be >= 1.0")
    inbound = db.fetchone("SELECT id, tag, remark FROM inbounds WHERE id=?", (inbound_id,))
    if not inbound:
        raise SystemExit(f"Inbound id {inbound_id} not found")
    ts = now_ms()
    if db.kind == "postgres":
        db.execute(
            """
            INSERT INTO xui_factor_inbounds (inbound_id, factor, enabled, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (inbound_id) DO UPDATE SET
              factor = EXCLUDED.factor,
              enabled = EXCLUDED.enabled,
              note = EXCLUDED.note,
              updated_at = EXCLUDED.updated_at
            """,
            (inbound_id, float(factor), enabled, note, ts, ts),
        )
    else:
        db.execute(
            """
            INSERT INTO xui_factor_inbounds (inbound_id, factor, enabled, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(inbound_id) DO UPDATE SET
              factor = excluded.factor,
              enabled = excluded.enabled,
              note = excluded.note,
              updated_at = excluded.updated_at
            """,
            (inbound_id, float(factor), qbool(db, enabled), note, ts, ts),
        )
    refreshed = refresh_baseline_for_inbound(db, inbound_id)
    print(f"Factor set: inbound_id={inbound_id} tag={inbound.get('tag')} factor={factor}")
    print(f"Baseline refreshed for {refreshed} client(s). Old usage will not be charged.")


def refresh_baseline_for_inbound(db: DB, inbound_id: int) -> int:
    """Baseline all emails attached to an inbound at their current raw usage.

    This prevents old traffic from being charged immediately after a factor is
    created or changed. It also subtracts previously-applied extra bytes if a
    state row already exists.
    """
    ensure_schema(db)
    rows = db.fetchall(
        """
        SELECT DISTINCT c.email AS email, ct.up AS up, ct.down AS down
        FROM clients c
        JOIN client_inbounds ci ON ci.client_id = c.id
        JOIN client_traffics ct ON ct.email = c.email
        WHERE ci.inbound_id = ? AND c.email IS NOT NULL AND c.email <> ''
        """,
        (inbound_id,),
    )
    count = 0
    for row in rows:
        email = str(row.get("email") or "")
        state = db.fetchone("SELECT * FROM xui_factor_state WHERE email=?", (email,))
        applied_extra_up = int(state.get("extra_up") or 0) if state else 0
        applied_extra_down = int(state.get("extra_down") or 0) if state else 0
        raw_up = max(0, int(row.get("up") or 0) - applied_extra_up)
        raw_down = max(0, int(row.get("down") or 0) - applied_extra_down)
        insert_or_update_state(db, email, raw_up, raw_down, applied_extra_up, applied_extra_down, Decimal("1.0"), inbound_id, "baseline_refreshed")
        count += 1
    return count


def disable_factor(db: DB, inbound_id: int, delete: bool = False) -> None:
    if delete:
        db.execute("DELETE FROM xui_factor_inbounds WHERE inbound_id=?", (inbound_id,))
        print(f"Factor deleted for inbound_id={inbound_id}")
    else:
        db.execute("UPDATE xui_factor_inbounds SET enabled=?, updated_at=? WHERE inbound_id=?", (qbool(db, False), now_ms(), inbound_id))
        print(f"Factor disabled for inbound_id={inbound_id}")


def factor_rows_for_tick(db: DB) -> List[Dict[str, Any]]:
    # Load every email->inbound attachment, not only factored inbounds.
    # 3x-ui stores client_traffics by email globally, so we need the full
    # attachment set to avoid applying a factor to a different inbound that
    # happens to use the same email.
    return db.fetchall(
        """
        SELECT
          c.id AS client_id,
          c.email AS email,
          c.enable AS client_enabled,
          ct.enable AS traffic_enabled,
          ct.up AS up,
          ct.down AS down,
          ci.inbound_id AS inbound_id,
          i.tag AS inbound_tag,
          f.factor AS factor,
          f.enabled AS factor_enabled,
          f.note AS factor_note
        FROM clients c
        JOIN client_traffics ct ON ct.email = c.email
        JOIN client_inbounds ci ON ci.client_id = c.id
        JOIN inbounds i ON i.id = ci.inbound_id
        LEFT JOIN xui_factor_inbounds f ON f.inbound_id = ci.inbound_id
        WHERE c.email IS NOT NULL AND c.email <> ''
        ORDER BY c.email ASC, ci.inbound_id ASC, c.id ASC
        """
    )


def _row_factor_enabled(row: Dict[str, Any]) -> bool:
    try:
        factor = Decimal(str(row.get("factor") or "1"))
    except Exception:
        return False
    return factor > Decimal("1.0") and parse_bool(row.get("factor_enabled"), False)


def _unique_inbound_count(items: List[Dict[str, Any]]) -> int:
    return len({int(r.get("inbound_id") or 0) for r in items if int(r.get("inbound_id") or 0) > 0})


def choose_candidates(
    rows: List[Dict[str, Any]],
    cfg: Dict[str, str],
    log: Logger,
    active_inbound_tags: Optional[set[str]] = None,
) -> Tuple[List[Candidate], List[Tuple[str, str]]]:
    strict = parse_bool(cfg.get("STRICT_SINGLE_INBOUND"), True)
    active_strict = parse_bool(cfg.get("ACTIVE_INBOUND_STRICT"), True)
    allow_best_effort = parse_bool(cfg.get("ALLOW_MULTI_INBOUND_BEST_EFFORT"), False)

    by_email: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if not parse_bool(row.get("client_enabled"), True) or not parse_bool(row.get("traffic_enabled"), True):
            continue
        by_email.setdefault(str(row.get("email")), []).append(row)

    candidates: List[Candidate] = []
    skipped: List[Tuple[str, str]] = []

    for email, items in by_email.items():
        inbound_count = _unique_inbound_count(items)
        factored_items = [r for r in items if _row_factor_enabled(r)]
        if not factored_items:
            continue

        selected: Optional[Dict[str, Any]] = None

        if active_inbound_tags is not None and active_strict:
            # Webhook mode: the payload tells which inbound tags moved bytes in this scan.
            # Only charge if exactly one factored inbound for this email was active and no
            # non-factored sibling inbound for this same email was active.
            active_items = [r for r in items if str(r.get("inbound_tag") or "") in active_inbound_tags]
            active_factored = [r for r in active_items if _row_factor_enabled(r)]
            active_unfactored = [r for r in active_items if not _row_factor_enabled(r)]
            if not active_factored:
                skipped.append((email, "factored_inbound_not_active"))
                continue
            if len({int(r.get("inbound_id") or 0) for r in active_factored}) > 1:
                skipped.append((email, "multiple_active_factored_inbounds"))
                continue
            if active_unfactored:
                skipped.append((email, "active_unfactored_sibling_inbound"))
                continue
            selected = active_factored[0]
        else:
            # Polling mode has no per-scan inbound tag payload. For safety, an email
            # attached to multiple inbounds is ambiguous and must not be charged.
            if len({int(r.get("inbound_id") or 0) for r in factored_items}) > 1:
                skipped.append((email, "multiple_factored_inbounds"))
                continue
            if strict and inbound_count != 1:
                skipped.append((email, f"strict_skip_shared_email:{inbound_count}"))
                continue
            if (not strict) and inbound_count != 1 and not allow_best_effort:
                skipped.append((email, f"shared_email_best_effort_disabled:{inbound_count}"))
                continue
            selected = factored_items[0]

        if selected is None:
            continue

        try:
            factor = Decimal(str(selected.get("factor")))
        except Exception:
            skipped.append((email, "invalid_factor"))
            continue
        if factor <= Decimal("1.0"):
            continue

        candidates.append(
            Candidate(
                email=email,
                up=int(selected.get("up") or 0),
                down=int(selected.get("down") or 0),
                inbound_id=int(selected.get("inbound_id") or 0),
                inbound_tag=str(selected.get("inbound_tag") or ""),
                factor=factor,
                client_id=int(selected.get("client_id") or 0),
                client_inbound_count=inbound_count,
                factor_note=str(selected.get("factor_note") or ""),
            )
        )

    if skipped:
        log.debug(f"Skipped {len(skipped)} clients/inbound mappings")
    return candidates, skipped


def active_inbound_tags_from_payload(payload: Optional[Dict[str, Any]]) -> Optional[set[str]]:
    if not payload:
        return None
    tags: set[str] = set()
    traffics = payload.get("inboundTraffics") or payload.get("traffics") or []
    if not isinstance(traffics, list):
        return None
    for tr in traffics:
        if not isinstance(tr, dict):
            continue
        is_inbound = tr.get("IsInbound") if "IsInbound" in tr else tr.get("isInbound", True)
        if is_inbound is False:
            continue
        tag = str(tr.get("Tag") if "Tag" in tr else tr.get("tag") or "")
        try:
            up = int(tr.get("Up") if "Up" in tr else tr.get("up") or 0)
            down = int(tr.get("Down") if "Down" in tr else tr.get("down") or 0)
        except Exception:
            up = down = 0
        if tag and (up > 0 or down > 0):
            tags.add(tag)
    return tags

def rounded_int(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def insert_or_update_state(
    db: DB,
    email: str,
    last_raw_up: int,
    last_raw_down: int,
    extra_up: int,
    extra_down: int,
    factor: Decimal,
    inbound_id: Optional[int],
    skipped_reason: str = "",
) -> None:
    ts = now_ms()
    if db.kind == "postgres":
        db.execute(
            """
            INSERT INTO xui_factor_state
              (email, last_raw_up, last_raw_down, extra_up, extra_down, last_factor, last_inbound_id, skipped_reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (email) DO UPDATE SET
              last_raw_up = EXCLUDED.last_raw_up,
              last_raw_down = EXCLUDED.last_raw_down,
              extra_up = EXCLUDED.extra_up,
              extra_down = EXCLUDED.extra_down,
              last_factor = EXCLUDED.last_factor,
              last_inbound_id = EXCLUDED.last_inbound_id,
              skipped_reason = EXCLUDED.skipped_reason,
              updated_at = EXCLUDED.updated_at
            """,
            (email, last_raw_up, last_raw_down, extra_up, extra_down, float(factor), inbound_id, skipped_reason, ts),
        )
    else:
        db.execute(
            """
            INSERT INTO xui_factor_state
              (email, last_raw_up, last_raw_down, extra_up, extra_down, last_factor, last_inbound_id, skipped_reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
              last_raw_up = excluded.last_raw_up,
              last_raw_down = excluded.last_raw_down,
              extra_up = excluded.extra_up,
              extra_down = excluded.extra_down,
              last_factor = excluded.last_factor,
              last_inbound_id = excluded.last_inbound_id,
              skipped_reason = excluded.skipped_reason,
              updated_at = excluded.updated_at
            """,
            (email, last_raw_up, last_raw_down, extra_up, extra_down, float(factor), inbound_id, skipped_reason, ts),
        )



def _payload_value(obj: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in obj:
            return obj.get(key)
    return default


def client_deltas_from_payload(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return raw client deltas from 3x-ui External Traffic Inform payload.

    3x-ui sends the values returned by Xray for this exact scan interval. They
    are already deltas, not totals. Webhook mode must use these values directly;
    reading totals from client_traffics after we have written extra bytes causes
    recursive charging and inflated traffic.
    """
    if not payload:
        return []
    items = payload.get("clientTraffics") or payload.get("client_traffics") or []
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        email = str(_payload_value(item, "Email", "email", default="") or "").strip()
        if not email:
            continue
        try:
            up = int(_payload_value(item, "Up", "up", default=0) or 0)
            down = int(_payload_value(item, "Down", "down", default=0) or 0)
        except Exception:
            continue
        if up <= 0 and down <= 0:
            continue
        out.append({"email": email, "up": max(0, up), "down": max(0, down)})
    return out


def webhook_fingerprint(payload: Optional[Dict[str, Any]]) -> str:
    # Keep only the scan deltas that influence billing. This makes the duplicate
    # guard stable while ignoring unrelated payload field ordering.
    client = client_deltas_from_payload(payload)
    tags = sorted(active_inbound_tags_from_payload(payload) or [])
    client = sorted(client, key=lambda r: (str(r.get("email")), int(r.get("up") or 0), int(r.get("down") or 0)))
    raw = json.dumps({"client": client, "tags": tags}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def claim_webhook_fingerprint(db: DB, fingerprint: str, window_ms: int = 15000) -> bool:
    """Return True when this webhook payload can be processed.

    Some reverse proxies can retry a POST. We only suppress the exact same
    payload for a very short window so two legitimate equal-sized scans several
    seconds apart will still be charged correctly.
    """
    ts = now_ms()
    db.execute("DELETE FROM xui_factor_webhook_seen WHERE created_at < ?", (ts - max(1000, window_ms),))
    if db.kind == "postgres":
        cur = db.execute(
            """
            INSERT INTO xui_factor_webhook_seen (fingerprint, created_at)
            VALUES (?, ?)
            ON CONFLICT (fingerprint) DO NOTHING
            """,
            (fingerprint, ts),
        )
        return getattr(cur, "rowcount", 0) == 1
    cur = db.execute("INSERT OR IGNORE INTO xui_factor_webhook_seen (fingerprint, created_at) VALUES (?, ?)", (fingerprint, ts))
    return getattr(cur, "rowcount", 0) == 1


def process_webhook_delta(db: DB, cfg: Dict[str, str], log: Logger, payload: Dict[str, Any]) -> Dict[str, int]:
    """Apply factors using only the raw deltas sent by 3x-ui for this scan.

    Desired billing model:
      scan_delta_raw = traffic reported by 3x-ui for this refresh only
      extra = scan_delta_raw * (factor - 1)
      client_traffics += extra

    Previous totals are never re-read to compute webhook billing. This prevents
    recursive multiplication like 100MB -> 400MB and the first-tick spike issue.
    """
    stats = {"seen": 0, "baselined": 0, "charged": 0, "skipped": 0, "reset_detected": 0, "extra_bytes": 0}
    deltas = client_deltas_from_payload(payload)
    if not deltas:
        return stats
    active_tags = active_inbound_tags_from_payload(payload) or set()
    rows = factor_rows_for_tick(db)
    by_email: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if not parse_bool(row.get("client_enabled"), True) or not parse_bool(row.get("traffic_enabled"), True):
            continue
        email = str(row.get("email") or "")
        if email:
            by_email.setdefault(email, []).append(row)

    direction = str(cfg.get("CHARGE_DIRECTION", "proportional")).strip().lower()
    if direction not in {"proportional", "down"}:
        direction = "proportional"

    ts = now_ms()
    with db.tx():
        fp = webhook_fingerprint(payload)
        if not claim_webhook_fingerprint(db, fp):
            stats["skipped"] += len(deltas)
            log.debug("duplicate webhook payload skipped")
            return stats

        for d in deltas:
            email = str(d.get("email") or "")
            delta_up = int(d.get("up") or 0)
            delta_down = int(d.get("down") or 0)
            stats["seen"] += 1
            items = by_email.get(email, [])
            if not items:
                stats["skipped"] += 1
                continue

            # Use the active inbound tags from the same 3x-ui scan. When an email
            # is shared across several inbounds and the scan cannot prove the
            # factored inbound is the only active owner, skip it instead of
            # charging a wrong inbound.
            active_items = [r for r in items if str(r.get("inbound_tag") or "") in active_tags]
            if not active_items and len(items) == 1:
                # Fallback for unusual payloads without inboundTraffics.
                active_items = items
            active_factored = [r for r in active_items if _row_factor_enabled(r)]
            active_unfactored = [r for r in active_items if not _row_factor_enabled(r)]
            unique_factored = {int(r.get("inbound_id") or 0) for r in active_factored}

            if not active_factored:
                stats["skipped"] += 1
                insert_or_update_state(db, email, 0, 0, 0, 0, Decimal("1.0"), None, "webhook_unfactored")
                continue
            if len(unique_factored) != 1:
                stats["skipped"] += 1
                insert_or_update_state(db, email, 0, 0, 0, 0, Decimal("1.0"), None, "webhook_ambiguous_factored")
                continue
            if active_unfactored:
                stats["skipped"] += 1
                insert_or_update_state(db, email, 0, 0, 0, 0, Decimal("1.0"), None, "webhook_active_unfactored_sibling")
                continue

            selected = active_factored[0]
            try:
                factor = Decimal(str(selected.get("factor") or "1"))
            except Exception:
                stats["skipped"] += 1
                continue
            if factor <= Decimal("1.0"):
                stats["skipped"] += 1
                continue

            state = db.fetchone("SELECT * FROM xui_factor_state WHERE email=?", (email,))
            current_panel_up = int(selected.get("up") or 0)
            current_panel_down = int(selected.get("down") or 0)
            selected_inbound_id = int(selected.get("inbound_id") or 0)

            if state is None:
                # First webhook after install/factor change: baseline the actual
                # panel counters that already include 3x-ui's raw write for this
                # scan. Do not bill this first unknown tick.
                insert_or_update_state(
                    db,
                    email,
                    max(0, current_panel_up),
                    max(0, current_panel_down),
                    0,
                    0,
                    factor,
                    selected_inbound_id,
                    "webhook_initial_panel_baseline",
                )
                stats["baselined"] += 1
                continue

            prev_raw_up = int(state.get("last_raw_up") or 0)
            prev_raw_down = int(state.get("last_raw_down") or 0)
            applied_extra_up = int(state.get("extra_up") or 0)
            applied_extra_down = int(state.get("extra_down") or 0)

            # Idempotency guard based on the panel DB counters, not only on the
            # HTTP payload. 3x-ui adds the raw delta to client_traffics before it
            # calls External Traffic Inform. D-Zarib then adds only the extra part.
            # If the same webhook is delivered again, client_traffics has not
            # advanced by a new raw delta, so available_* becomes zero and we skip.
            prev_panel_up = prev_raw_up + applied_extra_up
            prev_panel_down = prev_raw_down + applied_extra_down
            if current_panel_up < prev_panel_up or current_panel_down < prev_panel_down:
                # The user probably reset traffic in 3x-ui or edited counters.
                # Start from the current panel value and never retroactively bill.
                insert_or_update_state(
                    db,
                    email,
                    max(0, current_panel_up),
                    max(0, current_panel_down),
                    0,
                    0,
                    factor,
                    selected_inbound_id,
                    "webhook_counter_reset",
                )
                stats["reset_detected"] += 1
                continue

            available_up = max(0, current_panel_up - prev_panel_up)
            available_down = max(0, current_panel_down - prev_panel_down)
            charge_delta_up = min(max(0, delta_up), available_up)
            charge_delta_down = min(max(0, delta_down), available_down)

            if charge_delta_up == 0 and charge_delta_down == 0:
                stats["skipped"] += 1
                insert_or_update_state(
                    db,
                    email,
                    prev_raw_up,
                    prev_raw_down,
                    applied_extra_up,
                    applied_extra_down,
                    factor,
                    selected_inbound_id,
                    "webhook_duplicate_or_no_new_db_delta",
                )
                continue

            if charge_delta_up != delta_up or charge_delta_down != delta_down:
                log.debug(
                    f"webhook delta capped for {email}: "
                    f"payload=({delta_up},{delta_down}) available=({available_up},{available_down}) "
                    f"charged=({charge_delta_up},{charge_delta_down})"
                )

            add_ratio = factor - Decimal("1.0")
            if direction == "down":
                extra_up = 0
                extra_down = rounded_int(Decimal(charge_delta_up + charge_delta_down) * add_ratio)
            else:
                extra_up = rounded_int(Decimal(charge_delta_up) * add_ratio)
                extra_down = rounded_int(Decimal(charge_delta_down) * add_ratio)

            if extra_up or extra_down:
                db.execute("UPDATE client_traffics SET up = up + ?, down = down + ? WHERE email = ?", (extra_up, extra_down, email))
                db.execute(
                    """
                    INSERT INTO xui_factor_events
                      (email, inbound_id, factor, raw_delta_up, raw_delta_down, extra_up, extra_down, created_at, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        email,
                        selected_inbound_id,
                        float(factor),
                        charge_delta_up,
                        charge_delta_down,
                        extra_up,
                        extra_down,
                        ts,
                        "webhook_delta_v4_idempotent",
                    ),
                )
                stats["charged"] += 1
                stats["extra_bytes"] += extra_up + extra_down

            insert_or_update_state(
                db,
                email,
                prev_raw_up + charge_delta_up,
                prev_raw_down + charge_delta_down,
                applied_extra_up + max(0, extra_up),
                applied_extra_down + max(0, extra_down),
                factor,
                selected_inbound_id,
                "webhook_delta_v4_idempotent",
            )
    return stats

def run_once(db: DB, cfg: Dict[str, str], log: Logger, webhook_payload: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    require_3xui_tables(db)
    ensure_schema(db)
    stats = {
        "seen": 0,
        "baselined": 0,
        "charged": 0,
        "skipped": 0,
        "reset_detected": 0,
        "extra_bytes": 0,
    }
    # In webhook mode 3x-ui sends raw deltas for this exact scan. Use them
    # directly. Do not compute deltas from DB totals because this service also
    # writes extra bytes to those totals, which would recursively multiply usage.
    if webhook_payload is not None:
        return process_webhook_delta(db, cfg, log, webhook_payload)

    rows = factor_rows_for_tick(db)
    active_tags = active_inbound_tags_from_payload(webhook_payload)
    candidates, skipped = choose_candidates(rows, cfg, log, active_tags)
    stats["skipped"] += len(skipped)
    current_by_email: Dict[str, Dict[str, int]] = {}
    for row in rows:
        email = str(row.get("email") or "")
        if email and email not in current_by_email:
            current_by_email[email] = {"up": int(row.get("up") or 0), "down": int(row.get("down") or 0)}
    ts = now_ms()
    direction = str(cfg.get("CHARGE_DIRECTION", "proportional")).strip().lower()
    if direction not in {"proportional", "down"}:
        direction = "proportional"

    with db.tx():
        # Record skip reasons and advance the raw baseline for skipped emails.
        # This is critical for shared-email clients: traffic from a non-factored
        # inbound must not be accumulated and later charged when the factored
        # inbound becomes active.
        for email, reason in skipped[:5000]:
            existing = db.fetchone("SELECT * FROM xui_factor_state WHERE email=?", (email,))
            current = current_by_email.get(email, {"up": 0, "down": 0})
            applied_extra_up = int(existing.get("extra_up") or 0) if existing else 0
            applied_extra_down = int(existing.get("extra_down") or 0) if existing else 0
            raw_up = max(0, int(current.get("up") or 0) - applied_extra_up)
            raw_down = max(0, int(current.get("down") or 0) - applied_extra_down)
            insert_or_update_state(
                db,
                email=email,
                last_raw_up=raw_up,
                last_raw_down=raw_down,
                extra_up=applied_extra_up,
                extra_down=applied_extra_down,
                factor=Decimal(str(existing.get("last_factor") or "1")) if existing else Decimal("1.0"),
                inbound_id=existing.get("last_inbound_id") if existing else None,
                skipped_reason=reason,
            )
        for c in candidates:
            stats["seen"] += 1
            state = db.fetchone("SELECT * FROM xui_factor_state WHERE email=?", (c.email,))
            if state is None:
                insert_or_update_state(db, c.email, c.up, c.down, 0, 0, c.factor, c.inbound_id, "")
                stats["baselined"] += 1
                continue

            prev_raw_up = int(state.get("last_raw_up") or 0)
            prev_raw_down = int(state.get("last_raw_down") or 0)
            applied_extra_up = int(state.get("extra_up") or 0)
            applied_extra_down = int(state.get("extra_down") or 0)

            raw_up = c.up - applied_extra_up
            raw_down = c.down - applied_extra_down
            if raw_up < 0 or raw_down < 0 or raw_up < prev_raw_up or raw_down < prev_raw_down:
                # A reset/manual edit probably happened. Do not retroactively charge; start a new baseline.
                raw_up = max(0, c.up)
                raw_down = max(0, c.down)
                insert_or_update_state(db, c.email, raw_up, raw_down, 0, 0, c.factor, c.inbound_id, "reset_detected")
                stats["reset_detected"] += 1
                continue

            delta_up = raw_up - prev_raw_up
            delta_down = raw_down - prev_raw_down
            if delta_up == 0 and delta_down == 0:
                insert_or_update_state(db, c.email, raw_up, raw_down, applied_extra_up, applied_extra_down, c.factor, c.inbound_id, "")
                continue

            add_ratio = c.factor - Decimal("1.0")
            if direction == "down":
                extra_up = 0
                extra_down = rounded_int(Decimal(delta_up + delta_down) * add_ratio)
            else:
                extra_up = rounded_int(Decimal(delta_up) * add_ratio)
                extra_down = rounded_int(Decimal(delta_down) * add_ratio)

            if extra_up or extra_down:
                db.execute(
                    "UPDATE client_traffics SET up = up + ?, down = down + ? WHERE email = ?",
                    (extra_up, extra_down, c.email),
                )
                db.execute(
                    """
                    INSERT INTO xui_factor_events
                      (email, inbound_id, factor, raw_delta_up, raw_delta_down, extra_up, extra_down, created_at, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (c.email, c.inbound_id, float(c.factor), delta_up, delta_down, extra_up, extra_down, ts, c.factor_note),
                )
                stats["charged"] += 1
                stats["extra_bytes"] += extra_up + extra_down

            insert_or_update_state(
                db,
                c.email,
                raw_up,
                raw_down,
                applied_extra_up + extra_up,
                applied_extra_down + extra_down,
                c.factor,
                c.inbound_id,
                "",
            )
    return stats


def human_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    num = float(value)
    for unit in units:
        if abs(num) < 1024 or unit == units[-1]:
            return f"{num:.2f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{value} B"


def cmd_audit(db: DB, limit: int = 20) -> None:
    ensure_schema(db)
    rows = db.fetchall(
        """
        SELECT id, email, inbound_id, factor, raw_delta_up, raw_delta_down, extra_up, extra_down, created_at, note
        FROM xui_factor_events
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    if not rows:
        print("No factor events.")
        return
    out = []
    for r in rows:
        raw = int(r.get("raw_delta_up") or 0) + int(r.get("raw_delta_down") or 0)
        extra = int(r.get("extra_up") or 0) + int(r.get("extra_down") or 0)
        out.append({
            "id": r.get("id"),
            "email": r.get("email"),
            "inbound": r.get("inbound_id"),
            "factor": f"{float(r.get('factor') or 1):.4g}",
            "raw": human_bytes(raw),
            "extra": human_bytes(extra),
            "billed": human_bytes(raw + extra),
        })
    print_table(out, [("id", "ID"), ("email", "Email"), ("inbound", "Inbound"), ("factor", "Factor"), ("raw", "Raw"), ("extra", "Extra"), ("billed", "Billed")])


def cmd_status(db: DB) -> None:
    ensure_schema(db)
    factor_count = db.fetchone("SELECT COUNT(*) AS n FROM xui_factor_inbounds WHERE enabled=?", (qbool(db, True),))
    state_count = db.fetchone("SELECT COUNT(*) AS n FROM xui_factor_state", ())
    event_count = db.fetchone("SELECT COUNT(*) AS n, COALESCE(SUM(extra_up + extra_down), 0) AS extra FROM xui_factor_events", ())
    print(f"Enabled factors: {factor_count.get('n', 0) if factor_count else 0}")
    print(f"Tracked clients:  {state_count.get('n', 0) if state_count else 0}")
    print(f"Events:           {event_count.get('n', 0) if event_count else 0}")
    print(f"Extra bytes:      {event_count.get('extra', 0) if event_count else 0}")


def reset_baseline(db: DB, email: Optional[str] = None) -> None:
    if email is not None:
        email = str(email).strip()
        if not email or email.lower() in {"all", "*", "none"}:
            email = None

    """Safely refresh baselines at the current 3x-ui counters.

    Older builds deleted xui_factor_state and waited for the next webhook to
    recreate baselines. On active servers that can race with an incoming 3x-ui
    scan and make the next tick look like a fresh/unknown state. The safe reset
    below snapshots the current panel counters immediately and preserves the
    already-applied extra counters, so old traffic is never charged again and
    the service does not need to stop. It also clears the short duplicate-webhook
    cache so the next real scan is evaluated normally.
    """
    ensure_schema(db)
    rows = factor_rows_for_tick(db)
    by_email: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        e = str(row.get("email") or "")
        if not e:
            continue
        if email and e != email:
            continue
        by_email.setdefault(e, []).append(row)

    refreshed = 0
    for e, items in by_email.items():
        # Prefer a factored inbound for the saved metadata; otherwise use the
        # first attached inbound. State is still keyed by email because 3x-ui
        # stores client_traffics globally by email.
        selected = None
        for item in items:
            if _row_factor_enabled(item):
                selected = item
                break
        if selected is None:
            selected = items[0]

        current_up = int(selected.get("up") or 0)
        current_down = int(selected.get("down") or 0)
        inbound_id = int(selected.get("inbound_id") or 0) or None
        try:
            factor = Decimal(str(selected.get("factor") or "1"))
        except Exception:
            factor = Decimal("1.0")

        old = db.fetchone("SELECT * FROM xui_factor_state WHERE email=?", (e,))
        old_extra_up = int(old.get("extra_up") or 0) if old else 0
        old_extra_down = int(old.get("extra_down") or 0) if old else 0

        raw_up = max(0, current_up - old_extra_up)
        raw_down = max(0, current_down - old_extra_down)
        insert_or_update_state(
            db,
            e,
            raw_up,
            raw_down,
            old_extra_up,
            old_extra_down,
            factor,
            inbound_id,
            "manual_safe_baseline_reset",
        )
        refreshed += 1

    # The duplicate cache is intentionally short-lived, but clearing it during a
    # manual reset prevents a same-size future scan from being incorrectly seen
    # as an old retry.
    db.execute("DELETE FROM xui_factor_webhook_seen")

    if email:
        if refreshed:
            print(f"Safe baseline refreshed for {email}.")
        else:
            print(f"No matching client found for {email}.")
    else:
        print(f"Safe baseline refreshed for {refreshed} client(s).")
    print("3x-ui counters were not changed.")


def set_3xui_setting(db: DB, key: str, value: str) -> None:
    if not db.table_exists("settings"):
        raise SystemExit("3x-ui settings table not found")
    cur = db.execute("UPDATE settings SET value=? WHERE key=?", (value, key))
    # sqlite3 and psycopg2 cursors both expose rowcount.
    if getattr(cur, "rowcount", 0) == 0:
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))


def cmd_enable_external_inform(db: DB, url: str) -> None:
    if not (url.startswith("https://") or url.startswith("http://")):
        raise SystemExit("URL must start with http:// or https://")
    with db.tx():
        set_3xui_setting(db, "externalTrafficInformEnable", "true")
        set_3xui_setting(db, "externalTrafficInformURI", url)
    print("3x-ui External Traffic Inform enabled")
    print(f"URI: {url}")
    print("Note: 3x-ui blocks localhost/private URLs here. Use a public domain that reverse-proxies to this service.")


def run_webhook_server(db: DB, cfg: Dict[str, str], log: Logger) -> None:
    require_3xui_tables(db)
    ensure_schema(db)
    host = str(cfg.get("WEBHOOK_HOST", "127.0.0.1") or "127.0.0.1")
    port = int(str(cfg.get("WEBHOOK_PORT", "19090") or "19090"))
    path = str(cfg.get("WEBHOOK_PATH", "/xui-factor/hook") or "/xui-factor/hook")
    if not path.startswith("/"):
        path = "/" + path
    token = str(cfg.get("WEBHOOK_TOKEN", "") or "")

    class Handler(BaseHTTPRequestHandler):
        server_version = "xui-factor-webhook/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("http " + (fmt % args))

        def _send(self, code: int, payload: Dict[str, Any]) -> None:
            body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send(200, {"ok": True, "app": APP_NAME})
                return
            self._send(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != path:
                self._send(404, {"ok": False, "error": "not_found"})
                return
            if token:
                qs = parse_qs(parsed.query)
                supplied = self.headers.get("X-XUI-Factor-Token") or (qs.get("token", [""])[0])
                if supplied != token:
                    self._send(403, {"ok": False, "error": "forbidden"})
                    return
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                length = 0
            payload: Optional[Dict[str, Any]] = None
            if length > 0:
                body = self.rfile.read(min(length, 10 * 1024 * 1024))
                if length > 10 * 1024 * 1024:
                    self._send(413, {"ok": False, "error": "payload_too_large"})
                    return
                try:
                    parsed_payload = json.loads(body.decode("utf-8", errors="ignore") or "{}")
                    if isinstance(parsed_payload, dict):
                        payload = parsed_payload
                except Exception:
                    payload = None
            try:
                stats = run_once(db, cfg, log, webhook_payload=payload)
                self._send(200, {"ok": True, "stats": stats})
                if stats.get("charged") or stats.get("baselined") or stats.get("reset_detected"):
                    log.info(
                        "webhook "
                        f"seen={stats['seen']} charged={stats['charged']} baselined={stats['baselined']} "
                        f"reset={stats['reset_detected']} extra_bytes={stats['extra_bytes']} skipped={stats['skipped']}"
                    )
            except Exception as exc:
                log.error(f"webhook failed: {exc}")
                self._send(500, {"ok": False, "error": str(exc)})

    httpd = HTTPServer((host, port), Handler)
    log.info(f"webhook listening on http://{host}:{port}{path} db={db.kind}")
    if token:
        log.info("webhook token protection is enabled")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutdown requested")
    finally:
        httpd.server_close()


def run_loop(db: DB, cfg: Dict[str, str], log: Logger, once: bool = False) -> None:
    require_3xui_tables(db)
    ensure_schema(db)
    interval = float(cfg.get("POLL_INTERVAL_SECONDS", "6") or "6")
    stop = False

    def handler(signum, frame):  # type: ignore[no-untyped-def]
        nonlocal stop
        stop = True
        log.info("shutdown requested")

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    polling_enabled = parse_bool(cfg.get("POLLING_BILLING_ENABLED"), False)
    log.info(f"started db={db.kind} interval={interval}s strict={cfg.get('STRICT_SINGLE_INBOUND')} direction={cfg.get('CHARGE_DIRECTION')} polling_billing={polling_enabled}")
    if not polling_enabled:
        log.warning("polling billing is disabled by default because 3x-ui stores client traffic by email, not by inbound. Use webhook mode for accurate billing.")
    while not stop:
        try:
            if not polling_enabled:
                if once:
                    return
                time.sleep(max(1.0, interval))
                continue
            stats = run_once(db, cfg, log)
            if stats["charged"] or stats["baselined"] or stats["reset_detected"]:
                log.info(
                    "tick "
                    f"seen={stats['seen']} charged={stats['charged']} baselined={stats['baselined']} "
                    f"reset={stats['reset_detected']} extra_bytes={stats['extra_bytes']} skipped={stats['skipped']}"
                )
            else:
                log.debug(f"tick seen={stats['seen']} skipped={stats['skipped']}")
        except Exception as exc:
            log.error(f"tick failed: {exc}")
        if once:
            return
        time.sleep(max(1.0, interval))




def env_quote(value: Any) -> str:
    text = "" if value is None else str(value)
    if re.match(r"^[A-Za-z0-9_./:@%+=,?&-]*$", text):
        return text
    return shlex.quote(text)


def save_config(path: str, cfg: Dict[str, str]) -> None:
    ordered = [
        "DB_TYPE",
        "SQLITE_PATH",
        "POSTGRES_DSN",
        "POLL_INTERVAL_SECONDS",
        "POLLING_BILLING_ENABLED",
        "STRICT_SINGLE_INBOUND",
        "ALLOW_MULTI_INBOUND_BEST_EFFORT",
        "CHARGE_DIRECTION",
        "LOG_LEVEL",
        "WEBHOOK_HOST",
        "WEBHOOK_PORT",
        "WEBHOOK_PATH",
        "WEBHOOK_TOKEN",
        "RUN_MODE",
        "ACTIVE_INBOUND_STRICT",
    ]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# D-Zarib config",
        "# Generated by interactive menu. You can edit it manually too.",
    ]
    for key in ordered:
        if key in cfg:
            lines.append(f"{key}={env_quote(cfg.get(key, ''))}")
    # Keep any extra custom keys at the bottom.
    for key in sorted(k for k in cfg if k not in ordered):
        lines.append(f"{key}={env_quote(cfg.get(key, ''))}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(str(p), 0o640)
    except PermissionError:
        pass


def random_token() -> str:
    try:
        import secrets

        return secrets.token_hex(24)
    except Exception:
        return str(int(time.time() * 1000000))


def prompt(message: str, default: Optional[str] = None) -> str:
    if default is None or default == "":
        raw = input(f"{message}: ").strip()
    else:
        raw = input(f"{message} [{default}]: ").strip()
    return raw if raw else (default or "")


def prompt_yes_no(message: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    raw = input(f"{message} [{suffix}]: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "1", "true", "on", "بله", "اره", "آره"}


def mask_secret_text(value: str) -> str:
    """Mask password-like parts before showing DSNs in the menu."""
    if not value:
        return ""
    # postgres://user:password@host:port/db -> postgres://user:***@host:port/db
    return re.sub(r"(postgres(?:ql)?://[^:/@\s]+:)([^@\s]+)(@)", r"\1***\3", value)


def sqlite_has_3xui_tables(path: str) -> bool:
    if not path or not Path(path).is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('inbounds','client_traffics')"
            ).fetchall()
            names = {str(r[0]) for r in rows}
            return {'inbounds', 'client_traffics'}.issubset(names)
        finally:
            conn.close()
    except Exception:
        return False


def _candidate_sqlite_paths() -> List[str]:
    candidates = [
        DEFAULT_SQLITE_PATH,
        "/etc/x-ui/x-ui.db",
        "/usr/local/x-ui/x-ui.db",
        "/opt/x-ui/x-ui.db",
        "/opt/3x-ui/x-ui.db",
        "/root/x-ui/x-ui.db",
    ]
    for base in ["/etc", "/opt", "/root", "/usr/local"]:
        try:
            for found in Path(base).glob("**/x-ui.db"):
                candidates.append(str(found))
        except Exception:
            pass
    seen: List[str] = []
    for item in candidates:
        if item and item not in seen:
            seen.append(item)
    return seen


def _detect_from_env_file(path: str) -> Optional[Dict[str, str]]:
    env = parse_env_file(path)
    if not env:
        return None
    db_type = env.get("XUI_DB_TYPE", "").strip().lower()
    dsn = env.get("XUI_DB_DSN", "").strip()
    if db_type in {"postgres", "postgresql", "pg"} and dsn:
        return {
            "DB_TYPE": "postgres",
            "POSTGRES_DSN": dsn,
            "SQLITE_PATH": env.get("XUI_DB_FOLDER", DEFAULT_SQLITE_PATH).rstrip("/") + "/x-ui.db",
            "DETECTED_SOURCE": path,
            "DETECTED_NOTE": "Found XUI_DB_TYPE/XUI_DB_DSN in 3x-ui environment file.",
        }
    folder = env.get("XUI_DB_FOLDER", "").strip()
    if folder:
        sqlite_path = str(Path(folder) / "x-ui.db")
        if sqlite_has_3xui_tables(sqlite_path):
            return {
                "DB_TYPE": "sqlite",
                "SQLITE_PATH": sqlite_path,
                "POSTGRES_DSN": "",
                "DETECTED_SOURCE": path,
                "DETECTED_NOTE": "Found XUI_DB_FOLDER in 3x-ui environment file.",
            }
    return None


def _detect_from_docker() -> Optional[Dict[str, str]]:
    try:
        import json as _json
        import subprocess

        ps = subprocess.check_output(
            ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        for line in ps.splitlines():
            if not re.search(r"(3x[-_]?ui|x[-_]?ui|sanaei)", line, re.I):
                continue
            container_id = line.split("\t", 1)[0].strip()
            container_name = line.split("\t", 1)[1].strip() if "\t" in line else container_id
            env_text = subprocess.check_output(
                ["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", container_id],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            env: Dict[str, str] = {}
            for env_line in env_text.splitlines():
                if "=" in env_line:
                    k, v = env_line.split("=", 1)
                    env[k] = v
            db_type = env.get("XUI_DB_TYPE", "").strip().lower()
            dsn = env.get("XUI_DB_DSN", "").strip()
            if db_type in {"postgres", "postgresql", "pg"} and dsn:
                return {
                    "DB_TYPE": "postgres",
                    "POSTGRES_DSN": dsn,
                    "SQLITE_PATH": DEFAULT_SQLITE_PATH,
                    "DETECTED_SOURCE": f"docker:{container_name}",
                    "DETECTED_NOTE": "Found PostgreSQL settings in 3x-ui container environment.",
                }
            mounts_json = subprocess.check_output(
                ["docker", "inspect", "-f", "{{json .Mounts}}", container_id],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            mounts = _json.loads(mounts_json or "[]")
            for mount in mounts:
                src = str(mount.get("Source", ""))
                dst = str(mount.get("Destination", ""))
                if dst.rstrip("/") == "/etc/x-ui" and src:
                    sqlite_path = str(Path(src) / "x-ui.db")
                    if sqlite_has_3xui_tables(sqlite_path):
                        return {
                            "DB_TYPE": "sqlite",
                            "SQLITE_PATH": sqlite_path,
                            "POSTGRES_DSN": "",
                            "DETECTED_SOURCE": f"docker:{container_name}",
                            "DETECTED_NOTE": "Found host-mounted /etc/x-ui directory from 3x-ui container.",
                        }
    except Exception:
        return None
    return None


def auto_detect_xui_database() -> Optional[Dict[str, str]]:
    """Detect 3x-ui DB automatically. Never asks the user for DSN."""
    for env_file in [
        "/etc/default/x-ui",
        "/etc/sysconfig/x-ui",
        "/etc/x-ui/x-ui.env",
        "/etc/x-ui/install-result.env",
    ]:
        if Path(env_file).is_file():
            found = _detect_from_env_file(env_file)
            if found:
                return found

    docker_found = _detect_from_docker()
    if docker_found:
        return docker_found

    for sqlite_path in _candidate_sqlite_paths():
        if sqlite_has_3xui_tables(sqlite_path):
            return {
                "DB_TYPE": "sqlite",
                "SQLITE_PATH": sqlite_path,
                "POSTGRES_DSN": "",
                "DETECTED_SOURCE": "filesystem",
                "DETECTED_NOTE": "Found a valid x-ui.db with required 3x-ui tables.",
            }
    return None


def apply_detected_database(cfg: Dict[str, str], detected: Dict[str, str]) -> Dict[str, str]:
    cfg["DB_TYPE"] = detected.get("DB_TYPE", "sqlite")
    cfg["SQLITE_PATH"] = detected.get("SQLITE_PATH", cfg.get("SQLITE_PATH", DEFAULT_SQLITE_PATH))
    cfg["POSTGRES_DSN"] = detected.get("POSTGRES_DSN", "") if cfg["DB_TYPE"] == "postgres" else ""
    return cfg


def print_detected_database(detected: Optional[Dict[str, str]]) -> None:
    if not detected:
        print(yellow("⚠️  Auto-detect could not find 3x-ui database settings."))
        return
    db_type = detected.get("DB_TYPE", "sqlite")
    target = detected.get("SQLITE_PATH", "") if db_type == "sqlite" else mask_secret_text(detected.get("POSTGRES_DSN", ""))
    print(green("✅ 3x-ui database detected automatically"))
    print(f"   Type:   {db_type}")
    print(f"   Target: {target}")
    print(f"   Source: {detected.get('DETECTED_SOURCE', 'auto')}")
    note = detected.get("DETECTED_NOTE", "")
    if note:
        print(f"   Note:   {note}")


def pause() -> None:
    try:
        input("\nPress Enter to continue...")
    except EOFError:
        pass


def color_enabled() -> bool:
    return os.environ.get("NO_COLOR", "").lower() not in {"1", "true", "yes"} and sys.stdout.isatty()


def paint(text: str, code: str) -> str:
    if not color_enabled():
        return text
    return f"\033[{code}m{text}\033[0m"


def muted(text: str) -> str:
    return paint(text, "2")


def cyan(text: str) -> str:
    return paint(text, "36;1")


def green(text: str) -> str:
    return paint(text, "32;1")


def yellow(text: str) -> str:
    return paint(text, "33;1")


def red(text: str) -> str:
    return paint(text, "31;1")


def magenta(text: str) -> str:
    return paint(text, "35;1")


def visual_len(text: str) -> int:
    """Approximate visible length without ANSI codes."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    # Emojis and Persian can be wider in some terminals; this keeps layout stable enough.
    return len(plain)


def menu_line(width: int = 78, left: str = "╭", fill: str = "─", right: str = "╮") -> str:
    return left + (fill * (width - 2)) + right


def menu_text(text: str = "", width: int = 78) -> str:
    pad = max(0, width - 4 - visual_len(text))
    return "│ " + text + (" " * pad) + " │"


def menu_divider(width: int = 78) -> None:
    print(cyan(menu_line(width, "├", "─", "┤")))


def menu_title(title: str, subtitle: str = "", width: int = 78) -> None:
    print("\n" + cyan(menu_line(width, "╭", "─", "╮")))
    print(cyan(menu_text(title, width)))
    if subtitle:
        print(cyan(menu_text(muted(subtitle), width)))
    print(cyan(menu_line(width, "├", "─", "┤")))


def menu_footer(width: int = 78) -> None:
    print(cyan(menu_line(width, "╰", "─", "╯")))


def badge(label: str, value: str, ok: bool = True) -> str:
    icon = "●"
    color = green if ok else red
    return f"{color(icon)} {label}: {value}"


def kv(label: str, value: str, ok: bool = True) -> str:
    icon = green("●") if ok else red("●")
    return f"{icon} {label}: {value}"


def ellipsize(text: str, max_len: int) -> str:
    plain = str(text or "")
    if visual_len(plain) <= max_len:
        return plain
    if max_len <= 1:
        return "…"
    return plain[: max_len - 1] + "…"


def status_row(label: str, value: str, width: int = 78, ok: bool = True) -> None:
    icon = green("●") if ok else red("●")
    label_text = f"{label:<14}"
    max_value_len = max(8, width - 4 - visual_len(icon) - 1 - visual_len(label_text) - 2)
    safe_value = ellipsize(value or "not set", max_value_len)
    print(menu_text(f"{icon} {label_text}: {safe_value}", width))


def section(title: str, width: int = 78) -> None:
    text = f" {title} "
    line_len = max(0, width - 4 - visual_len(text))
    print("│ " + magenta(text) + muted("─" * line_len) + " │")


def option(num: str, title: str, hint: str = "", icon: str = "•", width: int = 78) -> None:
    number = paint(f"{num:>2}", "36;1")
    left = f"{number}) {icon} {title}"
    gap = max(0, width - 4 - visual_len(left))
    print("│ " + left + (" " * gap) + " │")


def format_run_mode(cfg: Dict[str, str]) -> str:
    mode = str(cfg.get("RUN_MODE", "serve")).lower()
    if mode in {"serve", "webhook"}:
        return "Webhook"
    if mode in {"run", "poll", "polling"}:
        return "Polling"
    return mode or "not set"


def format_db_target(cfg: Dict[str, str]) -> str:
    db_type = str(cfg.get("DB_TYPE", "sqlite")).lower()
    if db_type in {"postgres", "postgresql", "pg"}:
        return mask_secret_text(cfg.get("POSTGRES_DSN", "")) or "not set"
    return cfg.get("SQLITE_PATH", "") or "not set"


def print_menu_header(cfg: Dict[str, str], config_path: str) -> None:
    width = 78
    menu_title("⚡ D-Zarib Control Center", "", width)
    section("STATUS", width)
    db_type = str(cfg.get("DB_TYPE", "sqlite")).upper()
    if db_type in {"POSTGRESQL", "PG"}:
        db_type = "POSTGRES"
    status_row("Database", db_type, width)
    status_row("Target", format_db_target(cfg), width)
    status_row("Run Mode", format_run_mode(cfg), width)
    status_row("Charge", cfg.get("CHARGE_DIRECTION", "proportional"), width)
    strict = "ON" if parse_bool(cfg.get("STRICT_SINGLE_INBOUND"), True) else "OFF"
    status_row("Strict", strict, width)
    webhook = f"{cfg.get('WEBHOOK_HOST', '127.0.0.1')}:{cfg.get('WEBHOOK_PORT', '19090')}{cfg.get('WEBHOOK_PATH', '/xui-factor/hook')}"
    status_row("Webhook", webhook, width)
    menu_divider(width)


def with_db(cfg: Dict[str, str]) -> DB:
    db = DB(cfg).connect()
    require_3xui_tables(db)
    ensure_schema(db)
    db.commit()
    return db


def menu_database(config_path: str, cfg: Dict[str, str]) -> Dict[str, str]:
    print("\nDatabase Setup")
    detected = auto_detect_xui_database()
    print_detected_database(detected)
    print()
    print("0) Auto-detect and save")
    print("1) SQLite manual")
    print("2) PostgreSQL manual")
    default_choice = "0" if detected else ("2" if str(cfg.get("DB_TYPE", "sqlite")).lower() in {"postgres", "postgresql", "pg"} else "1")
    choice = prompt("Choose", default_choice)
    if choice == "0":
        if not detected:
            print(yellow("Auto-detect failed. Please use manual SQLite or PostgreSQL."))
            return cfg
        cfg = apply_detected_database(cfg, detected)
    elif choice == "2":
        cfg["DB_TYPE"] = "postgres"
        cfg["POSTGRES_DSN"] = prompt(
            "PostgreSQL DSN",
            cfg.get("POSTGRES_DSN") or "postgresql://USER:PASSWORD@127.0.0.1:5432/DBNAME?sslmode=disable",
        )
    else:
        cfg["DB_TYPE"] = "sqlite"
        cfg["SQLITE_PATH"] = prompt("SQLite DB path", cfg.get("SQLITE_PATH") or DEFAULT_SQLITE_PATH)
        cfg["POSTGRES_DSN"] = ""
    save_config(config_path, cfg)
    print(green("✅ Config saved."))
    return cfg


def menu_run_mode(config_path: str, cfg: Dict[str, str]) -> Dict[str, str]:
    print("\nRun Mode")
    print("1) Webhook")
    print("2) Polling disabled")
    prompt("Choose", "1")
    cfg["RUN_MODE"] = "serve"
    cfg["POLLING_BILLING_ENABLED"] = "false"
    cfg["WEBHOOK_HOST"] = prompt("Webhook host", cfg.get("WEBHOOK_HOST") or "127.0.0.1")
    cfg["WEBHOOK_PORT"] = prompt("Webhook port", cfg.get("WEBHOOK_PORT") or "19090")
    cfg["WEBHOOK_PATH"] = prompt("Webhook path", cfg.get("WEBHOOK_PATH") or "/xui-factor/hook")
    if not cfg.get("WEBHOOK_TOKEN") or prompt_yes_no("Generate new webhook token?", False):
        cfg["WEBHOOK_TOKEN"] = random_token()
    save_config(config_path, cfg)
    print(green("✅ Webhook mode saved."))
    return cfg


def menu_behavior(config_path: str, cfg: Dict[str, str]) -> Dict[str, str]:
    print("\nTraffic Behavior")
    print("1) proportional")
    print("2) down")
    direction = prompt("Charge direction", cfg.get("CHARGE_DIRECTION") or "proportional").lower()
    if direction not in {"proportional", "down"}:
        direction = "proportional"
    cfg["CHARGE_DIRECTION"] = direction
    cfg["STRICT_SINGLE_INBOUND"] = "true" if prompt_yes_no("Strict single-inbound clients?", parse_bool(cfg.get("STRICT_SINGLE_INBOUND"), True)) else "false"
    if cfg["STRICT_SINGLE_INBOUND"] == "true":
        cfg["ALLOW_MULTI_INBOUND_BEST_EFFORT"] = "false"
    else:
        cfg["ALLOW_MULTI_INBOUND_BEST_EFFORT"] = "true" if prompt_yes_no("Allow multi-inbound best-effort?", parse_bool(cfg.get("ALLOW_MULTI_INBOUND_BEST_EFFORT"), False)) else "false"
    cfg["LOG_LEVEL"] = prompt("Log level: debug/info/warning/error", cfg.get("LOG_LEVEL") or "info")
    save_config(config_path, cfg)
    print(green("✅ Config saved."))
    return cfg


def menu_factors(cfg: Dict[str, str]) -> None:
    """Inbound factor menu.

    Important safety detail: do not keep one DB connection open while the user
    sits at prompts. PostgreSQL starts a transaction even for SELECT; keeping it
    open can create confusing locks and make reset feel like a crash. Every menu
    action below opens a short-lived connection, commits/rolls back, then closes.
    """

    def open_db() -> DB:
        return with_db(cfg)

    while True:
        print("\nInbound Factors")
        db = None
        try:
            db = open_db()
            cmd_list_inbounds(db)
        except Exception as exc:
            print(red(f"❌ DB error: {exc}"))
        finally:
            try:
                if db:
                    db.close()
            except Exception:
                pass

        print("\n1) Set / update factor")
        print("2) Disable factor")
        print("3) Delete factor")
        print("4) Sync baseline")
        print("5) Audit")
        print("0) Back")
        choice = prompt("Choose", "0").strip()
        if choice == "0":
            return

        if choice == "1":
            try:
                inbound = int(prompt("Inbound ID"))
                factor = Decimal(prompt("Factor, example 1.5", "1.5"))
                note = prompt("Note", "")
                db = open_db()
                with db.tx():
                    set_factor(db, inbound, factor, note, True)
                print(green("✅ Factor saved."))
            except Exception as exc:
                print(red(f"❌ Failed: {exc}"))
            finally:
                try:
                    db.close()
                except Exception:
                    pass

        elif choice == "2":
            try:
                inbound = int(prompt("Inbound ID"))
                db = open_db()
                with db.tx():
                    disable_factor(db, inbound, False)
                print(green("✅ Factor disabled."))
            except Exception as exc:
                print(red(f"❌ Failed: {exc}"))
            finally:
                try:
                    db.close()
                except Exception:
                    pass

        elif choice == "3":
            try:
                inbound = int(prompt("Inbound ID"))
                if prompt_yes_no("Delete this factor?", False):
                    db = open_db()
                    with db.tx():
                        disable_factor(db, inbound, True)
                    print(green("✅ Factor deleted."))
            except Exception as exc:
                print(red(f"❌ Failed: {exc}"))
            finally:
                try:
                    db.close()
                except Exception:
                    pass

        elif choice == "4":
            # This is intentionally a baseline sync only. It never changes 3x-ui
            # counters and never restarts/stops services from inside the menu.
            email = prompt("Client email (Enter/all = all)", "").strip()
            if email.lower() in {"all", "*", "none"}:
                email = ""
            if prompt_yes_no("Sync baseline now? 3x-ui counters will not be changed.", False):
                db = None
                try:
                    db = open_db()
                    with db.tx():
                        reset_baseline(db, email or None)
                    print(green("✅ Baseline synced."))
                except Exception as exc:
                    print(red(f"❌ Baseline sync failed: {exc}"))
                    print("Nothing was changed in 3x-ui counters.")
                finally:
                    try:
                        if db:
                            db.close()
                    except Exception:
                        pass

        elif choice == "5":
            try:
                db = open_db()
                cmd_audit(db, 20)
            except Exception as exc:
                print(red(f"❌ Audit failed: {exc}"))
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        else:
            print("Invalid choice.")

def menu_external_inform(cfg: Dict[str, str]) -> None:
    print("\nExternal Traffic Inform")
    url = prompt("Public webhook URL")
    if not url:
        print("Canceled.")
        return
    try:
        db = with_db(cfg)
    except Exception as exc:
        print(red(f"❌ DB error: {exc}"))
        return
    try:
        cmd_enable_external_inform(db, url)
    finally:
        db.close()


def run_shell(command: Sequence[str]) -> int:
    import subprocess

    try:
        return subprocess.call(list(command))
    except FileNotFoundError:
        print(f"Command not found: {command[0]}")
        return 127



def download_file(url: str, dest: str) -> None:
    import shutil
    import subprocess
    from urllib.request import urlopen

    if shutil.which("curl"):
        subprocess.check_call(["curl", "-fsSL", url, "-o", dest])
        return
    if shutil.which("wget"):
        subprocess.check_call(["wget", "-qO", dest, url])
        return
    with urlopen(url, timeout=30) as response:
        Path(dest).write_bytes(response.read())


def drop_dzarib_tables(config_path: str) -> bool:
    """Drop only D-Zarib sidecar tables. Never drops 3x-ui tables."""
    cfg = load_config(config_path)
    db = DB(cfg).connect()
    try:
        with db.tx():
            for table in [
                "xui_factor_webhook_seen",
                "xui_factor_events",
                "xui_factor_state",
                "xui_factor_inbounds",
            ]:
                db.execute(f"DROP TABLE IF EXISTS {table}")
        return True
    finally:
        db.close()


def cmd_uninstall(config_path: str, assume_yes: bool = False, drop_tables: bool = True, remove_config: bool = True) -> int:
    import shutil
    import subprocess

    print("\nFull Uninstall")
    print("This will remove D-Zarib service, commands, files, config, and D-Zarib database tables.")
    print("3x-ui itself will NOT be removed.")
    if not assume_yes:
        print("\nItems to remove:")
        print("  - systemd service: xui-factor")
        print("  - commands: xui-factor, xui-factorctl, d-zarib")
        print("  - app directory: /opt/xui-factor")
        print("  - config directory: /etc/xui-factor")
        print("  - database tables: xui_factor_*")
        if not prompt_yes_no("Continue uninstall?", False):
            print("Canceled.")
            return 1
        confirm = prompt("Type UNINSTALL to confirm", "")
        if confirm != "UNINSTALL":
            print("Canceled.")
            return 1

    if os.geteuid() != 0:
        print(red("❌ Please run uninstall as root: sudo d-zarib"))
        return 1

    if drop_tables:
        try:
            drop_dzarib_tables(config_path)
            print(green("✅ D-Zarib database tables removed."))
        except Exception as exc:
            print(yellow(f"⚠️  Could not drop D-Zarib database tables: {exc}"))

    for cmd in [
        ["systemctl", "disable", "--now", "xui-factor"],
    ]:
        subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for path in [
        "/etc/systemd/system/xui-factor.service",
        "/usr/local/bin/xui-factor",
        "/usr/local/bin/xui-factorctl",
        "/usr/local/bin/d-zarib",
    ]:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass

    for path in ["/opt/xui-factor"]:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    if remove_config:
        try:
            shutil.rmtree("/etc/xui-factor", ignore_errors=True)
        except Exception:
            pass

    subprocess.call(["systemctl", "daemon-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(green("✅ D-Zarib fully uninstalled."))
    print("3x-ui was not changed or removed.")
    return 0


def menu_uninstall(config_path: str) -> None:
    cmd_uninstall(config_path, assume_yes=False, drop_tables=True, remove_config=True)


def cmd_update(config_path: str, assume_yes: bool = False) -> int:
    import shutil
    import subprocess

    url = os.environ.get("DZARIB_INSTALL_URL", DEFAULT_INSTALL_URL)
    print("\nUpdate")
    print(f"Source: {url}")
    if not assume_yes and not prompt_yes_no("Start update now?", True):
        print("Canceled.")
        return 1

    cfg_path = Path(config_path)
    if cfg_path.exists():
        backup = cfg_path.with_name(cfg_path.name + ".bak." + time.strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.copy2(str(cfg_path), str(backup))
            print(green(f"✅ Config backup: {backup}"))
        except Exception as exc:
            print(yellow(f"⚠️  Could not create config backup: {exc}"))

    tmp = "/tmp/d-zarib-install.sh"
    try:
        download_file(url, tmp)
        os.chmod(tmp, 0o755)
    except Exception as exc:
        print(red(f"❌ Download failed: {exc}"))
        return 1

    env = os.environ.copy()
    env["DZARIB_KEEP_CONFIG"] = "1"
    env["DZARIB_RESTART_AFTER_INSTALL"] = "1"
    cmd = ["bash", tmp, "--update"]
    if os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    rc = subprocess.call(cmd, env=env)
    if rc == 0:
        print(green("✅ Update completed."))
    else:
        print(red(f"❌ Update failed with exit code {rc}."))
    return rc


def menu_update(config_path: str) -> None:
    cmd_update(config_path, assume_yes=False)

def menu_service() -> None:
    print("\nService")
    print("1) Start")
    print("2) Stop")
    print("3) Restart")
    print("4) Status")
    print("5) Logs")
    print("0) Back")
    choice = prompt("Choose", "4")
    if choice == "1":
        run_shell(["systemctl", "start", "xui-factor"])
    elif choice == "2":
        run_shell(["systemctl", "stop", "xui-factor"])
    elif choice == "3":
        run_shell(["systemctl", "restart", "xui-factor"])
    elif choice == "4":
        run_shell(["systemctl", "status", "xui-factor", "--no-pager"])
    elif choice == "5":
        run_shell(["journalctl", "-u", "xui-factor", "-n", "120", "--no-pager"])


def menu_status(cfg: Dict[str, str]) -> None:
    try:
        db = with_db(cfg)
    except Exception as exc:
        print(red(f"❌ DB error: {exc}"))
        return
    try:
        cmd_status(db)
    finally:
        db.close()


def interactive_menu(config_path: str) -> None:
    cfg = load_config(config_path)
    while True:
        print_menu_header(cfg, config_path)
        section("SETUP")
        option("1", "Database", "", "🗄️")
        option("2", "Run Mode", "", "🚦")
        option("3", "Traffic", "", "📊")
        section("TRAFFIC")
        option("4", "Inbound Factors", "", "⚙️")
        option("5", "3x-ui Inform", "", "🔗")
        section("SYSTEM")
        option("6", "Status", "", "🩺")
        option("7", "Service", "", "🧩")
        option("8", "Config", "", "📄")
        option("9", "Update", "", "⬆️")
        option("10", "Uninstall", "", "🗑️")
        option("0", "Exit", "", "🚪")
        menu_footer()
        choice = prompt("Choose", "0")
        try:
            if choice == "0":
                print("Bye.")
                return
            if choice == "1":
                cfg = menu_database(config_path, cfg)
            elif choice == "2":
                cfg = menu_run_mode(config_path, cfg)
            elif choice == "3":
                cfg = menu_behavior(config_path, cfg)
            elif choice == "4":
                menu_factors(cfg)
            elif choice == "5":
                menu_external_inform(cfg)
            elif choice == "6":
                menu_status(cfg)
            elif choice == "7":
                menu_service()
            elif choice == "8":
                print(Path(config_path).read_text(encoding="utf-8", errors="ignore") if Path(config_path).exists() else "Config file not found.")
            elif choice == "9":
                menu_update(config_path)
            elif choice == "10":
                menu_uninstall(config_path)
                return
            else:
                print("Invalid choice.")
        except KeyboardInterrupt:
            print("\nCanceled.")
        except Exception as exc:
            print(red(f"❌ Error: {exc}"))
        pause()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="3x-ui inbound traffic multiplier sidecar")
    p.add_argument("--config", default=DEFAULT_CONFIG, help=f"config env path (default: {DEFAULT_CONFIG})")
    sub = p.add_subparsers(dest="cmd")

    runp = sub.add_parser("run", help="run polling service loop")
    runp.add_argument("--once", action="store_true", help="run one tick and exit")

    sub.add_parser("serve", help="run HTTP webhook server for 3x-ui External Traffic Inform")

    eip = sub.add_parser("enable-external-inform", help="enable 3x-ui External Traffic Inform URI in DB")
    eip.add_argument("--url", required=True, help="public webhook URL, e.g. https://panel.example.com/xui-factor/hook?token=...")

    sub.add_parser("list-inbounds", help="list 3x-ui inbounds and assigned factors")

    setp = sub.add_parser("set-factor", help="set factor for inbound id")
    setp.add_argument("--inbound", type=int, required=True, help="inbound id")
    setp.add_argument("--factor", required=True, help="factor >= 1.0, e.g. 1.2")
    setp.add_argument("--note", default="", help="optional note")

    disp = sub.add_parser("disable-factor", help="disable factor for inbound id")
    disp.add_argument("--inbound", type=int, required=True)

    delp = sub.add_parser("delete-factor", help="delete factor row for inbound id")
    delp.add_argument("--inbound", type=int, required=True)

    rb = sub.add_parser("reset-baseline", help="reset sidecar baselines; does not change 3x-ui counters")
    rb.add_argument("--email", default=None, help="optional client email")

    ap = sub.add_parser("audit", help="show recent factor calculation events")
    ap.add_argument("--limit", type=int, default=20)

    sub.add_parser("status", help="show sidecar status")
    sub.add_parser("detect-db", help="auto-detect 3x-ui database settings")
    upd = sub.add_parser("update", help="update D-Zarib from GitHub and keep existing config")
    upd.add_argument("-y", "--yes", action="store_true", help="run update without confirmation")
    unp = sub.add_parser("uninstall", help="fully uninstall D-Zarib and remove xui_factor_* tables")
    unp.add_argument("-y", "--yes", action="store_true", help="run uninstall without confirmation")
    sub.add_parser("menu", help="open interactive configuration menu")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    log = Logger(cfg.get("LOG_LEVEL", "info"))
    invoked_as = Path(sys.argv[0]).name
    cmd = args.cmd or ("menu" if invoked_as in {"d-zarib", "dzarib"} else "run")
    if cmd == "update":
        raise SystemExit(cmd_update(args.config, assume_yes=getattr(args, "yes", False)))
    if cmd == "uninstall":
        raise SystemExit(cmd_uninstall(args.config, assume_yes=getattr(args, "yes", False), drop_tables=True, remove_config=True))
    if cmd == "menu":
        interactive_menu(args.config)
        return 0
    if cmd == "detect-db":
        print_detected_database(auto_detect_xui_database())
        return 0
    db = DB(cfg).connect()
    try:
        require_3xui_tables(db)
        ensure_schema(db)
        db.commit()
        if cmd == "run":
            # Backward-compatible safety: old systemd units may still call
            # `xui_factor.py run`. Polling cannot attribute email-level traffic
            # to a specific inbound, so by default `run` is migrated to the
            # webhook server. Explicit polling is only allowed with
            # DZARIB_ALLOW_POLLING=true.
            if parse_bool(os.environ.get("DZARIB_ALLOW_POLLING"), False):
                run_loop(db, cfg, log, once=bool(getattr(args, "once", False)))
            else:
                log.warning("legacy run command received; starting webhook server instead")
                run_webhook_server(db, cfg, log)
        elif cmd == "serve":
            run_webhook_server(db, cfg, log)
        elif cmd == "enable-external-inform":
            cmd_enable_external_inform(db, str(args.url))
        elif cmd == "list-inbounds":
            cmd_list_inbounds(db)
        elif cmd == "set-factor":
            with db.tx():
                set_factor(db, int(args.inbound), Decimal(str(args.factor)), str(args.note), True)
        elif cmd == "disable-factor":
            with db.tx():
                disable_factor(db, int(args.inbound), False)
        elif cmd == "delete-factor":
            with db.tx():
                disable_factor(db, int(args.inbound), True)
        elif cmd == "reset-baseline":
            with db.tx():
                reset_baseline(db, getattr(args, "email", None))
        elif cmd == "audit":
            cmd_audit(db, int(getattr(args, "limit", 20)))
        elif cmd == "status":
            cmd_status(db)
        else:
            parser.print_help()
            return 2
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
