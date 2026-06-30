#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/xui-factor"
CONF_DIR="/etc/xui-factor"
CONF_FILE="$CONF_DIR/config.env"
SERVICE_FILE="/etc/systemd/system/xui-factor.service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_RAW_BASE="https://raw.githubusercontent.com/officialdarvish/D-Zarib/main"
UPDATE_MODE=false
KEEP_CONFIG=false

for arg in "$@"; do
  case "$arg" in
    --update|-u) UPDATE_MODE=true ;;
    --keep-config) KEEP_CONFIG=true ;;
  esac
done

is_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|y|Y|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

resolve_source_file() {
  if [[ -f "$SRC_DIR/src/xui_factor.py" ]]; then
    echo "$SRC_DIR/src/xui_factor.py"
    return 0
  fi
  if [[ -f "$SRC_DIR/xui_factor.py" ]]; then
    echo "$SRC_DIR/xui_factor.py"
    return 0
  fi

  echo "Source file not found locally. Downloading src/xui_factor.py from GitHub..." >&2
  TMP_SRC="/tmp/d-zarib-xui-factor.py"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$REPO_RAW_BASE/src/xui_factor.py" -o "$TMP_SRC"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$TMP_SRC" "$REPO_RAW_BASE/src/xui_factor.py"
  else
    echo "curl or wget is required for quick install." >&2
    exit 1
  fi
  echo "$TMP_SRC"
}

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo bash install.sh"
  exit 1
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " D-Zarib installer"
echo " Sidecar traffic multiplier for 3x-ui"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

apt_get_install() {
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y python3 python3-venv python3-pip
  else
    echo "apt-get not found. Make sure python3 is installed."
  fi
}

apt_get_install

sqlite_has_3xui_tables() {
  local db_path="$1"
  [[ -f "$db_path" ]] || return 1
  python3 - "$db_path" <<'PY' >/dev/null 2>&1
import sqlite3, sys
path = sys.argv[1]
try:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('inbounds','client_traffics')")}
    con.close()
    raise SystemExit(0 if {'inbounds','client_traffics'} <= names else 1)
except Exception:
    raise SystemExit(1)
PY
}

read_env_key() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 1
  python3 - "$file" "$key" <<'PY'
import re, shlex, sys
path, wanted = sys.argv[1], sys.argv[2]
try:
    lines = open(path, encoding='utf-8', errors='ignore').read().splitlines()
except OSError:
    raise SystemExit(1)
for line in lines:
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    if line.startswith('export '):
        line = line[7:].strip()
    if '=' not in line:
        continue
    k, v = line.split('=', 1)
    if k.strip() != wanted:
        continue
    v = v.strip()
    try:
        parts = shlex.split(v, posix=True)
        print(parts[0] if len(parts) == 1 else v)
    except Exception:
        print(v.strip('"').strip("'"))
    raise SystemExit(0)
raise SystemExit(1)
PY
}

mask_dsn() {
  local dsn="$1"
  python3 - "$dsn" <<'PY'
import re, sys
print(re.sub(r'(postgres(?:ql)?://[^:/@\s]+:)([^@\s]+)(@)', r'\1***\3', sys.argv[1]))
PY
}

DETECTED_DB_TYPE=""
DETECTED_SQLITE_PATH=""
DETECTED_POSTGRES_DSN=""
DETECTED_SOURCE=""
DETECTED_NOTE=""

set_detected() {
  DETECTED_DB_TYPE="$1"
  DETECTED_SQLITE_PATH="$2"
  DETECTED_POSTGRES_DSN="$3"
  DETECTED_SOURCE="$4"
  DETECTED_NOTE="$5"
}

detect_from_env_file() {
  local f="$1"
  [[ -f "$f" ]] || return 1
  local dbtype dsn folder sqlite_path
  dbtype="$(read_env_key "$f" XUI_DB_TYPE 2>/dev/null || true)"
  dsn="$(read_env_key "$f" XUI_DB_DSN 2>/dev/null || true)"
  folder="$(read_env_key "$f" XUI_DB_FOLDER 2>/dev/null || true)"

  if [[ "$dbtype" =~ ^(postgres|postgresql|pg)$ && -n "$dsn" ]]; then
    sqlite_path="${folder:-/etc/x-ui}"
    set_detected "postgres" "${sqlite_path%/}/x-ui.db" "$dsn" "$f" "Found XUI_DB_TYPE/XUI_DB_DSN in 3x-ui environment."
    return 0
  fi

  if [[ -n "$folder" ]]; then
    sqlite_path="${folder%/}/x-ui.db"
    if sqlite_has_3xui_tables "$sqlite_path"; then
      set_detected "sqlite" "$sqlite_path" "" "$f" "Found XUI_DB_FOLDER and valid x-ui.db."
      return 0
    fi
  fi
  return 1
}

detect_from_docker() {
  command -v docker >/dev/null 2>&1 || return 1
  local line cid cname env_text dbtype dsn mounts src dst sqlite_path
  while IFS=$'\t' read -r cid cname; do
    [[ -n "$cid" ]] || continue
    if ! [[ "$cname" =~ (3x[-_]?ui|x[-_]?ui|sanaei) ]]; then
      continue
    fi
    env_text="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" 2>/dev/null || true)"
    dbtype="$(printf '%s\n' "$env_text" | awk -F= '$1=="XUI_DB_TYPE"{print $2; exit}')"
    dsn="$(printf '%s\n' "$env_text" | awk -F= '$1=="XUI_DB_DSN"{print $2; exit}')"
    if [[ "$dbtype" =~ ^(postgres|postgresql|pg)$ && -n "$dsn" ]]; then
      set_detected "postgres" "/etc/x-ui/x-ui.db" "$dsn" "docker:$cname" "Found PostgreSQL settings in 3x-ui container."
      return 0
    fi

    mounts="$(docker inspect -f '{{range .Mounts}}{{println .Source "=>" .Destination}}{{end}}' "$cid" 2>/dev/null || true)"
    while IFS= read -r line; do
      src="${line%% => *}"
      dst="${line##* => }"
      if [[ "${dst%/}" == "/etc/x-ui" && -n "$src" ]]; then
        sqlite_path="${src%/}/x-ui.db"
        if sqlite_has_3xui_tables "$sqlite_path"; then
          set_detected "sqlite" "$sqlite_path" "" "docker:$cname" "Found host-mounted /etc/x-ui directory from 3x-ui container."
          return 0
        fi
      fi
    done <<< "$mounts"
  done < <(docker ps --format '{{.ID}}	{{.Names}}' 2>/dev/null || true)
  return 1
}

detect_from_filesystem() {
  local p
  local candidates=(
    "/etc/x-ui/x-ui.db"
    "/usr/local/x-ui/x-ui.db"
    "/opt/x-ui/x-ui.db"
    "/opt/3x-ui/x-ui.db"
    "/root/x-ui/x-ui.db"
  )
  for p in "${candidates[@]}"; do
    if sqlite_has_3xui_tables "$p"; then
      set_detected "sqlite" "$p" "" "filesystem" "Found valid x-ui.db with required 3x-ui tables."
      return 0
    fi
  done
  while IFS= read -r p; do
    if sqlite_has_3xui_tables "$p"; then
      set_detected "sqlite" "$p" "" "filesystem" "Found valid x-ui.db with required 3x-ui tables."
      return 0
    fi
  done < <(find /etc /opt /root /usr/local -maxdepth 5 -name 'x-ui.db' -type f 2>/dev/null | head -20)
  return 1
}

auto_detect_xui_db() {
  local f
  for f in /etc/default/x-ui /etc/sysconfig/x-ui /etc/x-ui/x-ui.env /etc/x-ui/install-result.env; do
    detect_from_env_file "$f" && return 0
  done
  detect_from_docker && return 0
  detect_from_filesystem && return 0
  return 1
}

print_detected_db() {
  echo
  echo "✅ 3x-ui database detected automatically"
  echo "   Type:   $DETECTED_DB_TYPE"
  if [[ "$DETECTED_DB_TYPE" == "postgres" ]]; then
    echo "   Target: $(mask_dsn "$DETECTED_POSTGRES_DSN")"
  else
    echo "   Target: $DETECTED_SQLITE_PATH"
  fi
  echo "   Source: $DETECTED_SOURCE"
  echo "   Note:   $DETECTED_NOTE"
}


normalize_run_mode() {
  local v="${1:-}"
  case "${v,,}" in
    webhook|serve|external|external-inform|polling|poll|run) echo "serve" ;;
    *) echo "" ;;
  esac
}

mask_url_token() {
  local url="$1"
  python3 - "$url" <<'PY'
import re, sys
print(re.sub(r'([?&]token=)[^&\s]+', r'\1***', sys.argv[1]))
PY
}

auto_select_run_mode() {
  # Non-interactive smart default.
  # Always use webhook mode. Polling cannot know the exact inbound for each
  # 3x-ui scan and may produce wrong multipliers. Even if DZARIB_RUN_MODE=polling
  # is provided, it is migrated to webhook for safety.
  local requested public_url
  requested="$(normalize_run_mode "${DZARIB_RUN_MODE:-}")"
  public_url="${DZARIB_PUBLIC_URL:-${PUBLIC_HOOK_URL:-}}"

  POLL="${DZARIB_POLL_INTERVAL_SECONDS:-3}"
  WEBHOOK_HOST="${DZARIB_WEBHOOK_HOST:-127.0.0.1}"
  WEBHOOK_PORT="${DZARIB_WEBHOOK_PORT:-19090}"
  WEBHOOK_PATH="${DZARIB_WEBHOOK_PATH:-/xui-factor/hook}"
  WEBHOOK_TOKEN="${DZARIB_WEBHOOK_TOKEN:-}"

  if [[ -z "$WEBHOOK_TOKEN" ]]; then
    if command -v openssl >/dev/null 2>&1; then
      WEBHOOK_TOKEN="$(openssl rand -hex 24)"
    else
      WEBHOOK_TOKEN="$(python3 - <<'PY2'
import secrets
print(secrets.token_hex(24))
PY2
)"
    fi
  fi

  if [[ -n "$requested" ]]; then
    RUN_MODE="$requested"
    RUN_MODE_SOURCE="DZARIB_RUN_MODE"
  elif [[ -n "$public_url" ]]; then
    RUN_MODE="serve"
    RUN_MODE_SOURCE="public webhook URL provided"
  else
    RUN_MODE="serve"
    RUN_MODE_SOURCE="accurate automatic default"
  fi

  if [[ "$RUN_MODE" == "serve" ]]; then
    echo
    echo "✅ Run mode selected automatically: Webhook"
    echo "   Source: $RUN_MODE_SOURCE"
    echo "   Listen: http://${WEBHOOK_HOST}:${WEBHOOK_PORT}${WEBHOOK_PATH}"
    if [[ -n "$public_url" ]]; then
      echo "   Public: $(mask_url_token "$public_url")"
    else
      echo "   Note:   Accurate billing needs 3x-ui External Traffic Inform. Add Nginx/public HTTPS URL, then enable it from the menu."
    fi
  else
    RUN_MODE="serve"
    echo
    echo "✅ Run mode selected automatically: Webhook"
    echo "   Source: safe migration from polling"
    echo "   Listen: http://${WEBHOOK_HOST}:${WEBHOOK_PORT}${WEBHOOK_PATH}"
  fi
}

mkdir -p "$APP_DIR" "$CONF_DIR"
SOURCE_FILE="$(resolve_source_file)"
install -m 0755 "$SOURCE_FILE" "$APP_DIR/xui_factor.py"
ln -sf "$APP_DIR/xui_factor.py" /usr/local/bin/xui-factor
ln -sf "$APP_DIR/xui_factor.py" /usr/local/bin/xui-factorctl
ln -sf "$APP_DIR/xui_factor.py" /usr/local/bin/d-zarib

if [[ -f "$CONF_FILE" ]]; then
  echo "Existing config found: $CONF_FILE"
  if [[ "$UPDATE_MODE" == "true" || "$KEEP_CONFIG" == "true" ]] || is_true "${DZARIB_KEEP_CONFIG:-}"; then
    echo "Keeping existing config."
  else
    read -r -p "Overwrite config? [y/N]: " overwrite
    if [[ ! "$overwrite" =~ ^[Yy]$ ]]; then
      echo "Keeping existing config."
    else
      rm -f "$CONF_FILE"
    fi
  fi
fi

if [[ ! -f "$CONF_FILE" ]]; then
  DB_TYPE="sqlite"
  SQLITE_PATH="/etc/x-ui/x-ui.db"
  POSTGRES_DSN=""

  if auto_detect_xui_db; then
    print_detected_db
    DB_TYPE="$DETECTED_DB_TYPE"
    SQLITE_PATH="${DETECTED_SQLITE_PATH:-/etc/x-ui/x-ui.db}"
    POSTGRES_DSN="$DETECTED_POSTGRES_DSN"
  else
    echo
    echo "⚠️  Could not auto-detect 3x-ui database."
    echo "Database type:"
    echo "  1) SQLite"
    echo "  2) PostgreSQL"
    read -r -p "Choose [1]: " choice
    choice="${choice:-1}"
    if [[ "$choice" == "2" ]]; then
      DB_TYPE="postgres"
      read -r -p "PostgreSQL DSN: " POSTGRES_DSN
      if [[ -z "$POSTGRES_DSN" ]]; then
        echo "POSTGRES_DSN cannot be empty. Example: postgres://user:pass@127.0.0.1:5432/xui?sslmode=disable"
        exit 1
      fi
    else
      DB_TYPE="sqlite"
      read -r -p "SQLite DB path [/etc/x-ui/x-ui.db]: " SQLITE_PATH
      SQLITE_PATH="${SQLITE_PATH:-/etc/x-ui/x-ui.db}"
      if [[ ! -f "$SQLITE_PATH" ]]; then
        echo "Warning: SQLite file not found now: $SQLITE_PATH"
      fi
    fi
  fi

  if [[ "$DB_TYPE" == "postgres" ]] && command -v apt-get >/dev/null 2>&1; then
    apt-get install -y python3-psycopg2 || python3 -m pip install --break-system-packages psycopg2-binary || python3 -m pip install psycopg2-binary
  fi

  auto_select_run_mode

  cat > "$CONF_FILE" <<EOF_CONF
# D-Zarib config
# This service is separate from 3x-ui and only touches xui_factor_* tables + client_traffics extra usage.
DB_TYPE=${DB_TYPE}
SQLITE_PATH=${SQLITE_PATH}
POSTGRES_DSN=${POSTGRES_DSN}
POLL_INTERVAL_SECONDS=${POLL}
POLLING_BILLING_ENABLED=false
STRICT_SINGLE_INBOUND=true
ALLOW_MULTI_INBOUND_BEST_EFFORT=false
ACTIVE_INBOUND_STRICT=true
CHARGE_DIRECTION=proportional
LOG_LEVEL=info
WEBHOOK_HOST=${WEBHOOK_HOST}
WEBHOOK_PORT=${WEBHOOK_PORT}
WEBHOOK_PATH=${WEBHOOK_PATH}
WEBHOOK_TOKEN=${WEBHOOK_TOKEN}
RUN_MODE=${RUN_MODE}
EOF_CONF
  chmod 0640 "$CONF_FILE"
else
  # Safety migration: accurate D-Zarib billing must run in webhook mode.
  # Old installs may have RUN_MODE=run/polling, which can over-bill because
  # 3x-ui stores client traffic by email and polling cannot identify the inbound.
  # Keep DZARIB_KEEP_RUN_MODE=true only if you intentionally want fallback polling.
  if [[ "${DZARIB_KEEP_RUN_MODE:-false}" != "true" ]]; then
    if grep -Eq '^RUN_MODE=(run|poll|polling)' "$CONF_FILE" 2>/dev/null; then
      cp "$CONF_FILE" "$CONF_FILE.bak.$(date +%F-%H%M%S)" || true
      sed -i 's/^RUN_MODE=.*/RUN_MODE=serve/' "$CONF_FILE"
      echo "Migrated RUN_MODE to Webhook for accurate inbound billing."
    fi
    if ! grep -q '^POLLING_BILLING_ENABLED=' "$CONF_FILE" 2>/dev/null; then
      echo 'POLLING_BILLING_ENABLED=false' >> "$CONF_FILE"
    else
      sed -i 's/^POLLING_BILLING_ENABLED=.*/POLLING_BILLING_ENABLED=false/' "$CONF_FILE"
    fi
  fi
  RUN_MODE="$(grep -E '^RUN_MODE=' "$CONF_FILE" | tail -1 | cut -d= -f2- || true)"
  RUN_MODE="${RUN_MODE:-serve}"
fi

cat > "$SERVICE_FILE" <<EOF_SERVICE
[Unit]
Description=xui-factor - 3x-ui inbound traffic multiplier sidecar
After=network-online.target x-ui.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=${CONF_FILE}
ExecStart=/usr/bin/python3 ${APP_DIR}/xui_factor.py --config ${CONF_FILE} ${RUN_MODE}
Restart=always
RestartSec=5
User=root
WorkingDirectory=${APP_DIR}

[Install]
WantedBy=multi-user.target
EOF_SERVICE

systemctl daemon-reload
systemctl enable xui-factor >/dev/null

if [[ "$UPDATE_MODE" == "true" ]] || is_true "${DZARIB_RESTART_AFTER_INSTALL:-}"; then
  if systemctl is-active --quiet xui-factor 2>/dev/null; then
    systemctl restart xui-factor
    echo "xui-factor service restarted."
  fi
fi

echo
echo "Installed successfully."
echo "Config:  $CONF_FILE"
echo "Service: $SERVICE_FILE"
echo
echo "Useful commands:"
echo "  d-zarib                         # interactive menu"
echo "  xui-factorctl menu              # interactive menu"
echo "  xui-factorctl detect-db         # auto-detect 3x-ui database"
echo "  xui-factorctl list-inbounds"
echo "  xui-factorctl set-factor --inbound 1 --factor 1.2 --note 'Germany premium'"
echo "  systemctl start xui-factor"
echo "  journalctl -u xui-factor -f"
echo
if grep -q '^RUN_MODE=serve' "$CONF_FILE"; then
  WEBHOOK_PATH_PRINT="$(grep -E '^WEBHOOK_PATH=' "$CONF_FILE" | tail -1 | cut -d= -f2-)"
  WEBHOOK_TOKEN_PRINT="$(grep -E '^WEBHOOK_TOKEN=' "$CONF_FILE" | tail -1 | cut -d= -f2-)"
  echo "Webhook mode enabled. After you add an Nginx reverse proxy, enable 3x-ui inform like this:"
  echo "  xui-factorctl enable-external-inform --url 'https://YOUR-DOMAIN${WEBHOOK_PATH_PRINT}?token=${WEBHOOK_TOKEN_PRINT}'"
  echo
fi
echo "Important: first run only creates baselines; it does not charge old traffic retroactively."
