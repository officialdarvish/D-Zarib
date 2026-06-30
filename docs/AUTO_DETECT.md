# Auto Detection

D-Zarib keeps installation simple by detecting common 3x-ui settings automatically.

## Database

During installation and from the control menu, D-Zarib checks:

1. `/etc/default/x-ui`
2. `/etc/sysconfig/x-ui`
3. `/etc/x-ui/x-ui.env`
4. `/etc/x-ui/install-result.env`
5. Running Docker containers matching `3x-ui`, `3xui`, `x-ui`, or `sanaei`
6. Common SQLite paths such as `/etc/x-ui/x-ui.db`

If PostgreSQL is detected, the DSN is saved in `/etc/xui-factor/config.env`, but passwords are masked when shown.

If SQLite is detected, D-Zarib verifies that the database contains the required 3x-ui tables:

- `inbounds`
- `client_traffics`

Manual input is only requested if auto-detection fails.

## Run mode

The installer also selects the run mode automatically:

- `Polling` is selected by default because it works without domain or Nginx.
- `Webhook` is selected if you pass `DZARIB_RUN_MODE=webhook` or provide `DZARIB_PUBLIC_URL`.

Examples:

```bash
# Fully automatic safe install
bash <(curl -Ls https://raw.githubusercontent.com/officialdarvish/D-Zarib/main/install.sh)

# Force webhook mode
DZARIB_RUN_MODE=webhook bash install.sh

# Webhook mode with public URL hint
DZARIB_PUBLIC_URL="https://panel.example.com/xui-factor/hook" bash install.sh
```

## Commands

```bash
xui-factorctl detect-db
xui-factorctl menu
d-zarib
```
