# Obot

**An AI bot factory on Telegram.** Users chat with one central bot, `@ObotFactory`, describe what they want, and get their own Telegram bot with a persona and a toolbox. Each bot answers in DMs and groups, runs scheduled tasks, keeps structured data, and can quietly ask the user's other bots for help.

![Python](https://img.shields.io/badge/Python-3.13-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![aiogram](https://img.shields.io/badge/aiogram-3-26A5E4?style=flat-square&logo=telegram&logoColor=white)
![Pydantic AI](https://img.shields.io/badge/Pydantic_AI-E92063?style=flat-square&logo=pydantic&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?style=flat-square&logo=postgresql&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?style=flat-square&logo=react&logoColor=black)
![Tests](https://img.shields.io/badge/tests-442_passing-2EA44F?style=flat-square)

## How it works

1. **Describe the bot.** Tell the factory what you need ("a bot that sends me the gold price every evening"). It guesses a full configuration (name, persona, tools) and shows a draft card. It asks follow-up questions only when something essential is missing.
2. **Create it.** One tap opens Telegram's official *Managed Bots* dialog (Bot API 9.6). The factory receives the new bot's token, encrypts it, and wires up its webhook, description, and commands.
3. **Use it.** The bot talks with its persona in DMs and groups, uses its tools, and remembers context per chat.
4. **Share it.** A bot is a *template*: private by default (owner plus up to 10 people), or public. Everyone who uses it does so on **their own** plan, credits, API keys, and tasks.

## Features

**Agent engine**
- Built on **Pydantic AI** with OpenRouter. Models are picked per plan tier, and the core is independent of Telegram.
- Tools: web search (Tavily), safe URL reading, RSS, scheduling, structured data, and `ask_bot` delegation.
- **Bot-to-bot delegation:** a bot can ask the caller's other bots for help behind the scenes, with depth limited to 1 so there are no loops.

**Memory**
- Short-term memory: the most recent messages of the chat.
- Long-term memory: facts are extracted periodically in batches, embedded, and recalled with **pgvector**.
- Strict isolation: memory and history are always scoped by `(bot_id, chat_id)`. This is enforced in the repository layer and covered by dedicated tests.
- Raw history is kept for a limited time, and `/forget` wipes a chat's memory.

**Scheduled tasks and chains**
- Tasks are defined in natural language in the bot's DM, previewed as a draft, and confirmed before they run.
- Cron or one-off triggers run in the user's timezone. A per-minute scheduler claims due tasks with `FOR UPDATE SKIP LOCKED`.
- **Chains** run up to 5 linear steps, each on a different bot, and the output of each step feeds the next.
- Output goes to the user's DM or to a group or channel where the user is an admin. Failures produce a clear error message instead of failing silently.

**Structured data**
- Bots keep typed collections (personal, shared, or group scope) that the agent can create, query, update, and summarize.
- Reminders and change notifications are delivered through a transactional outbox.
- Every read and write goes through a single `DataSpace` access layer, so personal data never leaks into a group.

**Billing and plans**
- **The caller pays:** every LLM call, tool call, and delegation step is charged to the person who started the request, not to the bot's owner.
- Free plan quotas, paid credits sold for **Telegram Stars**, and manual top-ups.
- **BYOK** (bring your own key) for OpenRouter, OpenAI, and Anthropic: each user picks a key and a model per bot, and keys are encrypted at rest.
- All limits and model prices live in [`plans.yaml`](backend/plans.yaml), not in code.

**Safety and operations**
- Abuse reports, plus an admin **kill switch** that disables a bot or a user instantly and revokes the bot's token.
- SSRF protection on URL fetching: every redirect hop is checked against non-public addresses.
- All outgoing messages go through a single send layer with Redis rate limiting and `429` retry handling.
- Observability with **Langfuse** (LLM traces and cost per bot) and **Sentry**.

**Mini App panel**
- Telegram Mini App built with React 19, Vite, and TypeScript.
- Screens: bot list, bot creation and editing, tasks, data collections and schema editor, and per-user settings and keys.
- Persian and English, with full RTL support.

## Architecture

```
                 ┌───────────────┐
  Telegram ────► │  Caddy (TLS)  │ ──► /*   static Mini App (Vite build)
                 └───────┬───────┘
                         │ /tg/*  webhooks (factory + every managed bot)
                         │ /api/* Mini App API (initData HMAC auth)
                 ┌───────▼────────────────────────┐       ┌─────────┐
                 │  obot.server (single process)  │ ────► │  Redis  │  shared rate limits
                 │  FastAPI + aiogram 3           │       └─────────┘
                 │  agent engine (Pydantic AI)    │       ┌────────────┐
                 │  background jobs: scheduler,   │ ────► │ PostgreSQL │  source of truth
                 │  memory, retention, outbox     │       │ + pgvector │
                 └───────┬────────────────────────┘       └────────────┘
                         └──► OpenRouter / OpenAI / Anthropic · Tavily · web
```

| Path | Contents |
| --- | --- |
| `backend/src/obot/core/` | Agent engine: LLM setup, tools, fetch with SSRF guard, memory recall |
| `backend/src/obot/telegram/` | aiogram adapter: factory, managed bots, groups, the send layer |
| `backend/src/obot/factory/` | Conversational bot designer and draft cards |
| `backend/src/obot/tasks/` | Natural-language planner, schedules, chains |
| `backend/src/obot/data/` | Structured collections, schema, queries, event outbox |
| `backend/src/obot/billing/` | Plans, usage metering, Stars payments |
| `backend/src/obot/api/` | FastAPI routes for the Mini App |
| `miniapp/` | React + Vite Telegram Mini App |
| `deploy/` | Production Docker Compose stack (app, Postgres, Redis, Caddy, backups) |
| `docs/` | [Product and technical spec](docs/SPEC.md) (Persian), [deployment guide](docs/DEPLOY.md), spike notes |

## Getting started

**Requirements:** Python 3.13, [uv](https://docs.astral.sh/uv/), Node.js, Docker, and a Telegram bot token for the factory bot.

```bash
docker compose up -d                       # Postgres + pgvector (5433), Redis (6380)

cd backend
cp .env.example .env                       # set the factory bot token and API keys
uv sync
uv run alembic upgrade head
uv run python -m obot.telegram.run         # factory + bots (long polling) + background jobs
uv run uvicorn obot.api.app:app --reload   # Mini App API, in a second terminal

cd ../miniapp
npm install && npm run dev                 # Vite proxies /api to the backend
```

In production, a single `obot.server` process serves the API, Telegram webhooks, and background jobs. See [docs/DEPLOY.md](docs/DEPLOY.md).

## Tests and linting

```bash
cd backend
uv run pytest                              # 442 tests; needs the dev Postgres from compose
uv run ruff check . && uv run ruff format --check .
```
