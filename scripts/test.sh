#!/usr/bin/env bash
# scripts/test.sh
# Единая Linux-команда качества; offline HTTP/Chromium изолированы от сети хоста.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo 'Требуется Docker Engine для изолированных offline-тестов'; exit 1; }
uv sync --locked
if [[ "${1:-}" == '--live' ]]; then
  uv run --no-sync python -m src.cli check-live
fi
uv run --no-sync ruff check src tests alembic
uv run --no-sync ruff format --check src tests alembic
test_image=spec-mapper-ai-offline-tests:local
docker build --target test --tag "$test_image" .
test_container=$(docker create --init --network none --shm-size=256m "$test_image" \
  /app/.venv/bin/pytest -m 'not live' --cov=src --cov-report=term-missing -q)
cleanup() {
  # Копируются только результаты собственной проверки; реальные данные не монтируются.
  mkdir -p test-results
  docker cp "$test_container:/app/test-results/." ./test-results/ 2>/dev/null || true
  docker cp "$test_container:/app/.coverage" ./.coverage 2>/dev/null || true
  docker rm --force "$test_container" >/dev/null 2>&1 || true
}
trap cleanup EXIT
test_exit=0
docker start --attach "$test_container" || test_exit=$?
container_exit=$(docker inspect --format '{{.State.ExitCode}}' "$test_container")
if ((container_exit != 0)); then test_exit=$container_exit; fi
if ((test_exit != 0)); then exit "$test_exit"; fi
node --test tests/frontend/*.test.js
git diff --check
if [[ "${1:-}" == '--live' ]]; then
  docker compose --env-file .env.example --env-file .env run --build --rm --no-deps tests /app/.venv/bin/pytest -m live -q
fi
