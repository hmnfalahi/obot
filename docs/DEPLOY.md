# استقرار Obot

پشته‌ی production در `deploy/` است: Postgres (pgvector)، Redis، سرور Obot (API پنل + Webhook تلگرام + کارهای پس‌زمینه)، Caddy (HTTPS خودکار + سرو Mini App) و بک‌آپ روزانه.

```
اینترنت ──HTTPS──► Caddy ─┬─ /api/*, /tg/*, /health ──► app (obot.server)
                          └─ بقیه ──► فایل‌های استاتیک Mini App
app ──► db (Postgres + pgvector), redis
backup ──► pg_dump روزانه در deploy/backups (نگهداری ۷ روز)
```

## پیش‌نیازها

- یک VPS خارج از ایران با Docker و Docker Compose (حداقل ۱ CPU و ۲ GB RAM).
  برخی ارائه‌دهنده‌ها به ساکنین ایران سرویس نمی‌دهند؛ پیش از خرید بررسی کنید.
- یک دامنه که رکورد `A` آن به IP سرور اشاره کند (مثلاً `obot.example.com`).
- پورت‌های ۸۰ و ۴۴۳ باز باشند (Caddy برای گرفتن گواهی Let's Encrypt به هر دو نیاز دارد).

## راه‌اندازی

```bash
git clone git@github.com:hmnfalahi/obot.git && cd obot/deploy
cp .env.example .env
```

در `deploy/.env`:

| متغیر | توضیح |
|---|---|
| `DOMAIN` | نام دامنه، مثلاً `obot.example.com` |
| `PUBLIC_URL` | `https://` + دامنه؛ آدرس Webhook و Mini App |
| `POSTGRES_PASSWORD` | رمز طولانی و تصادفی |
| `OBOT_FACTORY_BOT_TOKEN` | توکن ربات کارخانه (Bot Management Mode روشن) |
| `OBOT_TOKEN_ENCRYPTION_KEY` | **همان کلیدی که توکن‌های موجود با آن رمز شده‌اند.** با تغییرش توکن‌های ذخیره‌شده باز نمی‌شوند |
| `OBOT_OPENROUTER_API_KEY`، `OBOT_TAVILY_API_KEY` | کلیدهای مدل و جستجو |
| `OBOT_ADMIN_TG_IDS` | شناسه‌ی تلگرامی ادمین‌ها، جدا با کاما |
| `OBOT_SENTRY_DSN`، `OBOT_LANGFUSE_*` | اختیاری |

سپس:

```bash
docker compose up -d --build
docker compose logs -f app
```

در لاگ باید `factory bot @... , webhook mode` و `launched N managed bots` دیده شود. Migrationها در هر شروع خودکار اجرا می‌شوند.

## بررسی سلامت

```bash
curl https://obot.example.com/health          # {"status":"ok"}
docker compose exec app python -c "print('ok')"
```

در تلگرام: `/start` به ربات کارخانه، دکمه‌ی **Panel** باید Mini App را باز کند.

## به‌روزرسانی

```bash
git pull
docker compose up -d --build
```

`app` فقط یک نمونه باید اجرا شود: کارهای زمان‌بندی‌شده و ثبت Webhook داخل همین فرایند است.

## بک‌آپ و بازیابی

بک‌آپ‌ها هر روز در `deploy/backups/obot-YYYY-MM-DD.dump` ذخیره می‌شوند. **آن‌ها را به جای دیگری هم منتقل کنید** (یک دیسک روی همان سرور بک‌آپ کافی نیست).

بازیابی:

```bash
docker compose stop app
docker compose exec -T db pg_restore -U obot -d obot --clean --if-exists < backups/obot-2026-10-03.dump
docker compose start app
```

## برگشت به حالت توسعه

اجرای `uv run python -m obot.telegram.run` روی سیستم توسعه Webhookها را پاک می‌کند و به polling برمی‌گرداند. هم‌زمان production و توسعه را با **یک ربات کارخانه** اجرا نکنید؛ برای توسعه یک ربات کارخانه‌ی جدا بسازید.
