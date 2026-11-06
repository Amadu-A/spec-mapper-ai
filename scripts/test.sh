#!/usr/bin/env bash
# scripts/test.sh
# Единая Linux-команда качества; обычный режим не требует внешних зависимостей.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --locked
uv run --no-sync ruff check src tests alembic
uv run --no-sync ruff format --check src tests alembic
uv run --no-sync playwright install chromium
uv run --no-sync pytest -m 'not live' --cov=src --cov-report=term-missing -q
node --test tests/frontend/*.test.js
git diff --check
if [[ "${1:-}" == '--live' ]]; then
  docker compose --env-file .env.example --env-file .env run --build --rm --no-deps tests /app/.venv/bin/pytest -m live -q
fi
