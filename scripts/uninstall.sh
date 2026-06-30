#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${CONFIG_PATH:-/etc/xui-factor/config.env}"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo bash scripts/uninstall.sh"
  exit 1
fi

confirm="${1:-}"
if [[ "$confirm" != "--yes" && "$confirm" != "-y" ]]; then
  echo "Full Uninstall"
  echo "This removes D-Zarib service, commands, files, config, and xui_factor_* database tables."
  echo "3x-ui itself will NOT be removed."
  read -r -p "Continue? [y/N]: " answer
  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    echo "Canceled."
    exit 1
  fi
  read -r -p "Type UNINSTALL to confirm: " typed
  if [[ "$typed" != "UNINSTALL" ]]; then
    echo "Canceled."
    exit 1
  fi
fi

if command -v xui-factorctl >/dev/null 2>&1; then
  xui-factorctl --config "$CONFIG_PATH" uninstall -y || true
else
  systemctl disable --now xui-factor 2>/dev/null || true
  rm -f /etc/systemd/system/xui-factor.service
  rm -f /usr/local/bin/xui-factor /usr/local/bin/xui-factorctl /usr/local/bin/d-zarib
  rm -rf /opt/xui-factor /etc/xui-factor
  systemctl daemon-reload 2>/dev/null || true
  echo "D-Zarib files removed. Database tables were not dropped because xui-factorctl was not available."
fi
