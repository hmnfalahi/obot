# Obot

A Telegram bot factory: users chat with `@ObotFactory` to create their own AI bots that answer in DMs and groups, run scheduled tasks, and work with other bots.

See [docs/SPEC.md](docs/SPEC.md) for the full spec and [AGENTS.md](AGENTS.md) for layout and commands.

## Quick start

```bash
docker compose up -d
cd backend && cp .env.example .env && uv sync && uv run pytest
cd ../miniapp && npm install && npm run dev
```
