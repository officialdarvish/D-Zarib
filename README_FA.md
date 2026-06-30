<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&height=180&color=0:111827,55:7C3AED,100:06B6D4&text=D-Zarib&fontColor=ffffff&fontSize=46&fontAlignY=36&desc=Inbound%20Traffic%20Multiplier%20for%203x-ui%20/%20x-ui&descAlignY=58&descSize=15" alt="D-Zarib Banner" />

<br>

[![GitHub Repo](https://img.shields.io/badge/GitHub-officialdarvish%2FD--Zarib-181717?style=for-the-badge&logo=github)](https://github.com/officialdarvish/D-Zarib)
[![3x-ui](https://img.shields.io/badge/3x--ui-Compatible-22C55E?style=for-the-badge)](https://github.com/MHSanaei/3x-ui)
[![DB](https://img.shields.io/badge/SQLite%20%7C%20PostgreSQL-Supported-7C3AED?style=for-the-badge)](#)

#> حالت اجرا برای محاسبه دقیق اینباندها روی **Webhook** قفل شده است. محاسبه با Polling غیرفعال است چون 3x-ui مصرف کلاینت را بر اساس email نگه می‌دارد، نه inbound.

## [English 🇬🇧](./README.md) | فارسی 🇮🇷

**D-Zarib** یک سرویس سبک و جدا برای **3x-ui / x-ui** است که بدون تغییر سورس اصلی پنل، روی اینباندهای انتخابی ضریب مصرف اعمال می‌کند.

</div>

---

## 🚀 نصب سریع

```bash
bash <(curl -Ls https://raw.githubusercontent.com/officialdarvish/D-Zarib/main/install.sh)
```

اگر branch ریپو شما `master` بود، از این دستور استفاده کن:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/officialdarvish/D-Zarib/master/install.sh)
```


D-Zarib هنگام نصب تلاش می‌کند دیتابیس 3x-ui را خودش تشخیص دهد. فایل‌های محیطی سنایی، مسیرهای معروف SQLite، فایل‌های سیستمی و کانتینرهای Docker بررسی می‌شوند؛ بنابراین کاربر عادی معمولاً نیازی به واردکردن دستی PostgreSQL DSN ندارد.

بعد از نصب، منوی کنترل را باز کن:

```bash
d-zarib
```

---

## ⚡ کار این سرویس چیست؟

D-Zarib به شما اجازه می‌دهد برای اینباندهای مشخص‌شده در 3x-ui ضریب مصرف جداگانه تنظیم کنید.

```text
مصرف واقعی:      100 GB
ضریب:            1.2
مصرف در پنل:     120 GB
```

مثلاً با ضریب `1.2`، هر `100GB` مصرف واقعی داخل پنل `120GB` محاسبه می‌شود.

برای محاسبه دقیق، D-Zarib در حالت Webhook از `External Traffic Inform` خود 3x-ui استفاده می‌کند و فقط ترافیک جدید همان اسکن را ضریب می‌زند؛ بنابراین ترافیک‌های قبلی دوباره محاسبه نمی‌شوند.

---

## ✨ قابلیت‌ها

<table>
  <tr>
    <td align="center"><b>🧩 نصب جدا</b><br>بدون تغییر سورس 3x-ui</td>
    <td align="center"><b>⚙️ ضریب اینباندی</b><br>ضریب جدا برای هر اینباند</td>
    <td align="center"><b>🗄️ تشخیص خودکار دیتابیس</b><br>SQLite و PostgreSQL را خودش پیدا می‌کند</td>
  </tr>
  <tr>
    <td align="center"><b>🧭 منو</b><br>تنظیم بدون دستورهای دستی</td>
    <td align="center"><b>🔗 هماهنگ با اسکن</b><br>سازگار با آپدیت ترافیک 3x-ui</td>
    <td align="center"><b>🧰 آماده CLI</b><br>دستورهای ساده و منوی کنترل</td>
  </tr>
</table>

---

## 🧭 منوی کنترل

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

با این دستور اجرا می‌شود:

```bash
d-zarib
```

یا:

```bash
xui-factorctl menu
```

---

## 🛠️ استفاده پایه

نمایش اینباندها:

```bash
xui-factorctl list-inbounds
```

تنظیم ضریب `1.2` برای اینباند `1`:

```bash
xui-factorctl set-factor --inbound 1 --factor 1.2
```

غیرفعال‌کردن ضریب:

```bash
xui-factorctl disable-factor --inbound 1
```

مشاهده لاگ‌ها:

```bash
journalctl -u xui-factor -f
```

آپدیت D-Zarib با حفظ کانفیگ فعلی:

```bash
xui-factorctl update -y
```

---

## 🔗 لینک‌های رسمی

| مورد | لینک |
|---|---|
| GitHub | https://github.com/officialdarvish/D-Zarib |
| کانال تلگرام | https://t.me/officialdarvishchannel |

---

## ❤️ حمایت مالی

<div align="center">

اگر این پروژه برایت مفید بود، می‌توانی از توسعه آن حمایت کنی.

<br>

[![Donate](https://img.shields.io/badge/Donate-NOWPayments-F97316?style=for-the-badge&logo=bitcoin&logoColor=white)](https://nowpayments.io/donation/officialdarvish)

https://nowpayments.io/donation/officialdarvish

</div>

---

<div align="center">

ساخته‌شده با ❤️ توسط **Official Darvish**

</div>
