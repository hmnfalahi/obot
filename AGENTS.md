# Obot

AI bot factory on Telegram. Product and technical decisions live in `docs/SPEC.md` — read it before making changes.

## Layout

- `backend/` — Python 3.13 (uv). FastAPI (`obot.api`), aiogram 3 for Telegram; background jobs (scheduled tasks, memory, retention) run in-process via `obot.jobs`. Source in `backend/src/obot/`.
- `miniapp/` — Telegram Mini App, Vite + React + TypeScript.
- `compose.yaml` — local dev infrastructure (Postgres + pgvector on port 5433, Redis on 6380).
- `deploy/` — production stack (Docker Compose: app, Postgres, Redis, Caddy, backups); see `docs/DEPLOY.md`.
- `docs/` — spec, spike notes and deployment guide.

## Processes

- Development: `obot.telegram.run` (factory + bots by long polling, plus background jobs) and `obot.api.app` (mini app API) run separately; Vite proxies `/api`.
- Production: one `obot.server` process serves the API, Telegram webhooks (`/tg/...`) and background jobs. Run exactly one instance.
- Never run development polling and production webhooks with the same factory bot at once.

## Commands

```bash
docker compose up -d                        # dev infra
cd backend && uv run pytest                 # tests
cd backend && uv run ruff check . && uv run ruff format .
cd backend && uv run python -m obot.telegram.run   # bots (polling)
cd backend && uv run uvicorn obot.api.app:app --reload
cd miniapp && npm run dev
cd deploy && docker compose up -d --build          # production stack
```

## Rules

- Chat memory and message history are always scoped by `(bot_id, tg_chat_id)`. Never query them without both — enforce it in the repository layer.
- Bot data records are read and written only through `obot.data` with a `DataSpace` (who is asking, on which bot, in which chat). Never query `Record` or `RecordKey` elsewhere.
- Plan limits and model credit rates come from config, never hard-coded.
- Bot tokens and BYOK keys are encrypted at rest.
- All outgoing Telegram messages go through the single send layer (rate limiting, retries).
