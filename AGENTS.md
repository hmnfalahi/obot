# Obot

AI bot factory on Telegram. Product and technical decisions live in `docs/SPEC.md` — read it before making changes.

## Layout

- `backend/` — Python 3.13 (uv). FastAPI (`obot.api`), Taskiq workers, aiogram 3 for Telegram. Source in `backend/src/obot/`.
- `miniapp/` — Telegram Mini App, Vite + React + TypeScript.
- `compose.yaml` — local dev infrastructure (Postgres + pgvector on port 5433, Redis on 6380).
- `docs/` — spec and design notes.

## Commands

```bash
docker compose up -d                        # dev infra
cd backend && uv run pytest                 # tests
cd backend && uv run ruff check . && uv run ruff format .
cd backend && uv run uvicorn obot.api.app:app --reload
cd miniapp && npm run dev
```

## Rules

- Chat memory and message history are always scoped by `(bot_id, tg_chat_id)`. Never query them without both — enforce it in the repository layer.
- Plan limits and model credit rates come from config, never hard-coded.
- Bot tokens and BYOK keys are encrypted at rest.
- All outgoing Telegram messages go through the single send layer (rate limiting, retries).
