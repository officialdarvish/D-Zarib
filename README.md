<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&height=180&color=0:111827,55:7C3AED,100:06B6D4&text=D-Zarib&fontColor=ffffff&fontSize=46&fontAlignY=36&desc=Inbound%20Traffic%20Multiplier%20for%203x-ui%20/%20x-ui&descAlignY=58&descSize=15" alt="D-Zarib Banner" />

<br>

[![GitHub Repo](https://img.shields.io/badge/GitHub-officialdarvish%2FD--Zarib-181717?style=for-the-badge&logo=github)](https://github.com/officialdarvish/D-Zarib)
[![3x-ui](https://img.shields.io/badge/3x--ui-Compatible-22C55E?style=for-the-badge)](https://github.com/MHSanaei/3x-ui)
[![DB](https://img.shields.io/badge/SQLite%20%7C%20PostgreSQL-Supported-7C3AED?style=for-the-badge)](#)

#> Run mode is forced to **Webhook** for accurate inbound billing. Polling billing is disabled because 3x-ui stores client traffic by email, not by inbound.

## English 🇬🇧 | [فارسی 🇮🇷](./README_FA.md)

**D-Zarib** is a lightweight sidecar service for **3x-ui / x-ui** that applies traffic multipliers to selected inbounds without changing the original panel source.

</div>

---

## 🚀 Quick Install

```bash
bash <(curl -Ls https://raw.githubusercontent.com/officialdarvish/D-Zarib/main/install.sh)
```

If your repository branch is `master`, use:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/officialdarvish/D-Zarib/master/install.sh)
```


D-Zarib tries to detect the existing 3x-ui database automatically during installation. It checks 3x-ui environment files, common SQLite paths, system files, and Docker containers, so normal users usually do not need to enter a PostgreSQL DSN manually.

Open the control menu after installation:

```bash
d-zarib
```

---

## ⚡ What It Does

D-Zarib lets you set a custom multiplier for specific 3x-ui inbounds.

```text
Real usage:   100 GB
Factor:       1.2
Panel usage:  120 GB
```

For example, with factor `1.2`, every real `100GB` usage is counted as `120GB` in the panel.

For accurate billing, D-Zarib uses 3x-ui `External Traffic Inform` in Webhook mode and multiplies only the new traffic from the current scan; previous traffic is not charged again.

---

## ✨ Features

<table>
  <tr>
    <td align="center"><b>🧩 Sidecar</b><br>No 3x-ui source changes</td>
    <td align="center"><b>⚙️ Per Inbound</b><br>Separate factor for each inbound</td>
    <td align="center"><b>🗄️ Auto DB</b><br>Detects SQLite/PostgreSQL automatically</td>
  </tr>
  <tr>
    <td align="center"><b>🧭 Menu</b><br>Configure without manual commands</td>
    <td align="center"><b>🔗 Scan Friendly</b><br>Works with 3x-ui traffic updates</td>
    <td align="center"><b>🧰 CLI Ready</b><br>Simple commands and control menu</td>
  </tr>
</table>

---

## 🧭 Control Menu

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

Run it with:

```bash
d-zarib
```

or:

```bash
xui-factorctl menu
```

---

## 🛠️ Basic Usage

List available inbounds:

```bash
xui-factorctl list-inbounds
```

Set factor `1.2` for inbound `1`:

```bash
xui-factorctl set-factor --inbound 1 --factor 1.2
```

Disable the factor:

```bash
xui-factorctl disable-factor --inbound 1
```

Check logs:

```bash
journalctl -u xui-factor -f
```

Update D-Zarib while keeping the current config:

```bash
xui-factorctl update -y
```

---

## 🔗 Official Links

| Item | Link |
|---|---|
| GitHub | https://github.com/officialdarvish/D-Zarib |
| Telegram Channel | https://t.me/officialdarvishchannel |

---

## ❤️ Donate

<div align="center">

If this project helps you, you can support its development.

<br>

[![Donate](https://img.shields.io/badge/Donate-NOWPayments-F97316?style=for-the-badge&logo=bitcoin&logoColor=white)](https://nowpayments.io/donation/officialdarvish)

https://nowpayments.io/donation/officialdarvish

</div>

---

<div align="center">

Made with ❤️ by **Official Darvish**

</div>
