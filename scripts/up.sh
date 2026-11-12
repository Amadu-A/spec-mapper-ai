#!/usr/bin/env bash
# scripts/up.sh
# Единственная команда запуска на подготовленном Linux-хосте; чужие сервисы и volumes не меняет.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo 'Требуется Docker Engine + Compose v2'; exit 1; }
command -v uv >/dev/null || { echo 'Требуется uv; установите его до запуска сервиса'; exit 1; }
uv sync --locked --no-dev
uv run --no-sync python -m src.cli preflight
uv run --no-sync python -m src.cli secrets
compose=(docker compose --env-file .env.example --env-file .env)
"${compose[@]}" config --quiet
"${compose[@]}" build api
uv run --no-sync python -m src.cli rabbitmq
"${compose[@]}" run --rm --no-deps api /app/.venv/bin/python -m src.cli shared-health
"${compose[@]}" up -d --wait --wait-timeout 120 postgres qdrant
"${compose[@]}" run --rm --no-deps migrate
"${compose[@]}" up -d --no-deps api worker operator-gateway
echo 'Ожидание каталога из сохранённого источника; журналы собственных API/worker/SSH-входа.'
"${compose[@]}" logs --follow --tail=30 api worker operator-gateway &
logs_pid=$!
trap 'kill "$logs_pid" 2>/dev/null || true' EXIT
"${compose[@]}" exec -T api /app/.venv/bin/python -m src.cli import-catalog --configured-source --wait
for ((attempt=1; attempt<=24; attempt++)); do
  if "${compose[@]}" exec -T api /app/.venv/bin/python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health/ready",timeout=15)' 2>/dev/null; then
    if "${compose[@]}" exec -T operator-gateway /app/.venv/bin/python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health/live",timeout=5)' 2>/dev/null; then
      uv run --no-sync python -c 'from src.config import Settings; s=Settings(); print(f"Готово: {s.app_public_base_url.rstrip(chr(47))}/"); print(f"SSH-вход: loopback 127.0.0.1:{s.operator_port}"); print("Для polling upstream задайте EXTRACTING_API_KEY в .env" if not s.extracting_api_key.get_secret_value() else "Серверный ключ upstream задан")'
      exit 0
    fi
  fi
  sleep 5
done
echo 'Сервис не достиг ready. Проверьте catalog/status, shared-embedding и журналы worker/SSH-входа.'
"${compose[@]}" ps
exit 1
