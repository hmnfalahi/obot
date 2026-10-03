# Obot server: mini app API, Telegram webhooks and background jobs.
FROM ghcr.io/astral-sh/uv:0.11 AS uv

FROM python:3.13-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so code changes reuse this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm
RUN useradd --system --uid 1000 obot
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini plans.yaml ./
COPY alembic ./alembic
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER obot
EXPOSE 8000
# Migrations run on every start; they are idempotent. Single worker: jobs and the
# bot transport live in-process.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn obot.server:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
