#!/bin/sh
set -eu

# Supported commands:
# serve/webhook = webhook mode
# run/polling   = polling mode
# once          = one polling tick then exit
if [ "$#" -eq 0 ]; then
    set -- "${RUN_MODE:-serve}"
fi

case "$1" in
    webhook)
        set -- serve
        ;;
    polling)
        set -- run
        ;;
    once)
        set -- run --once
        ;;
esac

case "$1" in
    serve|run|enable-external-inform|list-inbounds|set-factor|disable-factor|delete-factor|reset-baseline|audit|status|detect-db|menu)
        exec python /app/src/xui_factor.py "$@"
        ;;
    xui-factor|xui-factorctl|d-zarib)
        shift
        exec python /app/src/xui_factor.py "$@"
        ;;
    sh|bash|python|python3)
        exec "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
