# 🧭 D-Zarib Control Menu

Interactive terminal menu for configuring D-Zarib without typing commands one by one.

```bash
d-zarib
```

or:

```bash
xui-factorctl menu
```

## Preview

```text
╭────────────────────────────────────────────────────────────────────────────╮
│ ⚡ D-Zarib Control Center                                                   │
├────────────────────────────────────────────────────────────────────────────┤
│  STATUS ────────────────────────────────────────────────────────────────── │
│ ● Database    : POSTGRES                                                   │
│ ● Target      : postgres://hqdkKBVX:***@127.0.0.1:5432/xui?...            │
│ ● Run Mode    : Webhook                                                    │
│ ● Charge      : proportional                                               │
│ ● Strict      : ON                                                         │
│ ● Webhook     : 127.0.0.1:19090/xui-factor/hook                            │
├────────────────────────────────────────────────────────────────────────────┤
│  SETUP ─────────────────────────────────────────────────────────────────── │
│  1) 🗄️ Database                                                            │
│  2) 🚦 Run Mode                                                            │
│  3) 📊 Traffic                                                             │
│  TRAFFIC ───────────────────────────────────────────────────────────────── │
│  4) ⚙️ Inbound Factors                                                     │
│  5) 🔗 3x-ui Inform                                                        │
│  SYSTEM ────────────────────────────────────────────────────────────────── │
│  6) 🩺 Status                                                              │
│  7) 🧩 Service                                                             │
│  8) 📄 Config                                                              │
│  9) ⬆️ Update                                                              │
│ 10) 🗑️ Uninstall                                                           │
│  0) 🚪 Exit                                                                │
╰────────────────────────────────────────────────────────────────────────────╯
```

## Menu Sections

| Section | Options | Purpose |
|---|---:|---|
| SETUP | 1-3 | Auto database detection, run mode, and traffic behavior |
| TRAFFIC | 4-5 | Inbound factors and 3x-ui traffic inform |
| SYSTEM | 6-10 | Status, service control, config view, update, and full uninstall |

