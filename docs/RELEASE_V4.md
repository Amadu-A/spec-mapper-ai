<!-- docs/RELEASE_V4.md -->
# V4 — результат, проверки и обновление

Дата отчёта: 12.11.2026. Изменения сделаны только в Windows checkout.
Commit/push, production deployment, firewall и shared lifecycle не выполнялись.

## Сверка и результат

Начальное дерево чистое. Local HEAD, GitHub main и mirror Mapper:
`ec26642e06d28947a752f960dd1312a62c84c34f`.
Extracting: `9844c4ee68fbd35840b0e5bb634ab6a5eef0879e`;
shared infrastructure: `839d419432fc25bc988e5871f7e53f6d282a2e77`.
Контракт Export проверен по фактическому `app/domain/models.py` upstream.
[Анализ до реализации](V4_GAP_ANALYSIS.md), [исходное полное ТЗ](TECHNICAL_SPECIFICATION_V4.md).

- Полный JSON/XML HTTP источник с прежними allowlist/DNS pin/TLS safeguards,
  completion marker, ограничениями тела/глубины/числа строк и shrink guard.
- Durable webhook: отдельный ключ, SQL commit до 202, dedup/409, batch по 50 GUID,
  объединение событий за 20 секунд, прежний Rabbit outbox/lease/fence и общий staging/alias.
- Полная сверка закрывает доказанно покрытые старые события, включая missing upsert;
  события, полученные после начала download, остаются для последующей обработки.
- GUID registry сохраняет однозначно связанный legacy UUID, различает internal ID
  и настоящую ссылку 1С, поддерживает смену кода, карантин и tombstones.
- Semantic fingerprint отделён от metadata hash; V3 cache переносится при том же
  embedding input/identity. Metadata-only изменения не вызывают новую векторизацию.
- file_name и восемь полей проходят import/polling, PostgreSQL, UI, JSON/Excel/approve.
  Legacy source и уже сохранённые approved snapshots не переписываются.
- В GUI появились JSON/XML и события 1С; секрет webhook в браузер не выдаётся.
  XML необязателен для HTTP bootstrap и запуска с исправным сохранённым каталогом.

Миграция 0004 additive. Сохранены Top-5, null/очистка, массовое утверждение,
owner/CSRF/API keys/TTL, 8087, loopback gateway 8088 и постоянные volumes.

## Проверки

| Проверка | Результат и границы |
| --- | --- |
| Базовый Windows gate до изменений | 163 Python + 13 Node |
| Итоговый Windows `ops/check-quality.ps1` | 216 Python, в том числе 6 Chromium E2E; 14 Node; coverage 85%; Ruff/format/diff check |
| Linux offline, контейнер `--network none` | 210 Python, Ruff/format успешно; AI/1С явно mocked |
| Linux Node | 14 успешно, временный каталог удалён автоматически |
| Linux integration | 11 passed, 1 skipped; реальные PG/Qdrant/RabbitMQ/shared embedding/API, два worker; 1С — отдельный HTTP mock |
| Restart smoke | Реальный restart временного API после commit inbox/outbox до публикации RabbitMQ; повтор идемпотентен, два worker успешно применили событие |
| Additive upgrade | SQLite и реальная PostgreSQL: 0002→0003→0004 сохраняют прежние jobs/approval/каталог |
| Большой JSON | Синтетические 65 000 карточек, >23 МБ; bounded parser, без генерации 65k векторов |
| Реальная 1С | `blocked_by_external_1c`: нет подтверждённых completion marker, безопасного транспорта и сервисной учётной записи |

Единственный live skip — получение результата реального Extracting job: не передан
авторизованный готовый `--source-job-id`. Новый контракт Extracting проверен отдельно
на upstream коде, mock polling и сквозном real Mapper HTTP/worker экспорте.
Реальный полный справочник 1С с настоящими GUID в production не мигрировался:
счётчики миграции/нулевой metadata-only embedding проверены на изолированных fixtures.
Для реальной выгрузки число новых point ID/embeddings зависит от совпадения данных.

Windows логи: `.v4-quality-complete.log`; Linux: `.v4-linux.log`,
`.v4-live.log`, `.v4-node-linux.log`, `.v4-restart.log`. Скриншоты Chromium — `test-results/v4-*.png`.
Логи/скриншоты/временные harness не коммитятся. Не выдавать mock 1С за live.

Временные контейнеры и сети удалены с проверкой namespace/меток и отсутствия
persistent mounts. После удаления подтверждены неизменные production/shared containers,
HTTP ready 200 и прежний каталог: 62 052 позиции, версия
`d548a881-17a4-4d9a-9811-25bc6647a2a4`. Server checkout остался на `ec26642`.
Linux `bash -n scripts/up.sh` и Compose config проверены без применения конфигурации.

## Windows: проверка и ручная публикация

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\check-quality.ps1
git diff --check
git status --short
```

Ниже точный staging только файлов этой доработки. Выполнить владельцу после просмотра diff.

```powershell
git add -- `
  '.env.example' `
  'AGENTS.md' `
  'README.md' `
  'alembic/versions/0004_catalog_events.py' `
  'docker-compose.yml' `
  'docs/API.md' `
  'docs/DASHBOARD_AND_SYNC.md' `
  'docs/INTEGRATION_1C.md' `
  'docs/LLM_CONTEXT.md' `
  'docs/RELEASE_V4.md' `
  'docs/TECHNICAL_SPECIFICATION_V4.md' `
  'docs/V4_GAP_ANALYSIS.md' `
  'docs/architecture.md' `
  'docs/operations.md' `
  'scripts/up.sh' `
  'src/application/catalog.py' `
  'src/application/catalog_events.py' `
  'src/application/event_ports.py' `
  'src/application/mappings.py' `
  'src/application/ports.py' `
  'src/application/processing.py' `
  'src/application/reports.py' `
  'src/application/sync_ports.py' `
  'src/bootstrap.py' `
  'src/cli.py' `
  'src/config.py' `
  'src/domain/catalog_sync.py' `
  'src/domain/matching.py' `
  'src/domain/models.py' `
  'src/infrastructure/catalog_events.py' `
  'src/infrastructure/catalog_sources.py' `
  'src/infrastructure/database.py' `
  'src/infrastructure/json_catalog.py' `
  'src/infrastructure/repositories.py' `
  'src/infrastructure/secrets.py' `
  'src/infrastructure/sync_repository.py' `
  'src/infrastructure/telemetry.py' `
  'src/main.py' `
  'src/presentation/catalog_events.py' `
  'src/presentation/dashboard_schemas.py' `
  'src/presentation/middleware.py' `
  'src/presentation/routes.py' `
  'src/presentation/schemas.py' `
  'src/web/static/css/blocks/catalog-events.css' `
  'src/web/static/css/blocks/drawer.css' `
  'src/web/static/css/blocks/table.css' `
  'src/web/static/css/blocks/workspace.css' `
  'src/web/static/css/style.css' `
  'src/web/static/js/app.js' `
  'src/web/static/js/catalog-events.js' `
  'src/web/static/js/mapping.js' `
  'src/web/static/js/selection.js' `
  'src/web/static/js/source-fields.js' `
  'src/web/static/js/sync-settings.js' `
  'src/web/templates/base.html' `
  'src/web/templates/components/catalog_events.html' `
  'src/web/templates/components/sync_panel.html' `
  'src/web/templates/mapping.html' `
  'src/web/templates/selection.html' `
  'src/worker.py' `
  'tests/fixtures/onec_mock_server.py' `
  'tests/frontend/source-fields.test.js' `
  'tests/test_bootstrap_security.py' `
  'tests/test_browser_dashboard.py' `
  'tests/test_browser_v4.py' `
  'tests/test_bulk_approval.py' `
  'tests/test_catalog_events.py' `
  'tests/test_catalog_http_source.py' `
  'tests/test_catalog_json.py' `
  'tests/test_catalog_resume.py' `
  'tests/test_cli_worker.py' `
  'tests/test_dashboard_migrations.py' `
  'tests/test_specification_v4.py' `
  'tests/test_v4_live.py'
```

```powershell
git diff --cached --check
git diff --cached --stat
git commit -m "feat: JSON каталог 1С, durable webhook и расширенные спецификации"
git push git@github.com:Amadu-A/spec-mapper-ai.git main
git push git@github.com:neo-term-it/spec-mapper-ai.git main
```

В текущем origin настроены два push URL; выше они указаны явно, чтобы видеть
результат каждой публикации. Самостоятельно агент эти команды не выполняет.

## Linux: безопасное обновление владельцем

Сначала убедиться, что server checkout чистый. Перед первым V4 сохранить идентификатор
текущего каталога; это техническая метаинформация, без карточек и секретов.

```bash
cd ~/projects/spec-mapper-ai
git status --short
test -z "$(git status --porcelain)" || exit 1
mkdir -p .cache
docker compose --env-file .env.example --env-file .env exec -T api /app/.venv/bin/python -c '
import asyncio,json
from src.bootstrap import build_container
from src.config import Settings
async def main():
    c=build_container(Settings())
    try:
        s=await c.catalog.status()
        assert s["ready"]
        print(json.dumps({k:s[k] for k in ("version","collection","count")}))
    finally:
        await c.close()
asyncio.run(main())
' > .cache/catalog-before-v4.json
docker image tag spec-mapper-ai:local spec-mapper-ai:before-v4
git pull --ff-only origin main
bash scripts/up.sh
bash scripts/test.sh --live
```

Редирект выше создаёт только backup идентификатора каталога. Проверка/запуск показывают
логи как прежде. `up.sh` готовит отсутствующий webhook key, применяет 0004, использует
SQL-настройки источника и не удаляет volumes/staging. Повторный обычный запуск —
`bash scripts/up.sh`; bootstrap env не перезаписывает уже сохранённый XML/расписание.

До подтверждения внешней 1С оставить `ONEC_CATALOG_CONTRACT_CONFIRMED=false`.
HTTP Basic с временным пустым паролем не включать. Согласовать HTTPS, read-only пароль,
allowlist и completion marker, задать секреты в `.env`, сохранить HTTP JSON источник,
выполнить probe и ручной импорт, проверить counts, затем включить daily 15:00 UTC.
Программисту 1С: [инструкция и webhook](INTEGRATION_1C.md).

### Восстановление каталога без удаления данных

При штатной ошибке импорта rollback alias/pointer происходит автоматически.
Для адресного возврата к сохранённому опубликованному snapshot V4 предоставляет
прежние repository/index операции. Во время расследования остановить только worker;
API/просмотр сохраняются, новые задания будут ожидать. Не запускать параллельный импорт.

```bash
cd ~/projects/spec-mapper-ai
test -s .cache/catalog-before-v4.json || exit 1
docker compose --env-file .env.example --env-file .env stop worker
docker compose --env-file .env.example --env-file .env run --rm --no-deps -T api /app/.venv/bin/python -c '
import asyncio,json,sys
from uuid import UUID
from src.bootstrap import build_container
from src.config import Settings
target=json.load(sys.stdin)
async def main():
    c=build_container(Settings())
    try:
        version=str(UUID(target["version"]))
        assert target["collection"] == "spec_mapper_" + version.replace("-", "")
        async with c.catalog.lock():
            async with c.catalog.guard():
                assert await c.index.exists(target["collection"]), "Снимок уже собран: нужен сохранённый backup"
                assert await c.index.count(target["collection"]) == target["count"]
                assert len(await c.catalog.guids(version)) == target["count"]
                old=await c.index.switch(target["collection"])
                try:
                    await c.catalog.activate(version)
                except BaseException:
                    await c.index.restore(old)
                    raise
        print("Каталог восстановлен; worker остановлен до завершения диагностики")
    finally:
        await c.close()
asyncio.run(main())
' < .cache/catalog-before-v4.json
```

После устранения причины: `docker compose --env-file .env.example --env-file .env up -d --no-deps worker`.
Очередь событий сохраняется. При возобновлении актуальная 1С снова становится источником истины.
Не делать downgrade 0004/сброс PG/Qdrant. Возврат бинарного V3 после JSON-миграции требует
отдельной проверки legacy snapshot и очереди: старый worker не понимает catalog_events,
а старые DTO каталога не знают новых metadata полей. Слепая смена image на V3 не является
безопасным rollback; сохранённый tag нужен для контролируемого восстановления владельцем.

## Ручная приёмка

1. Открыть `http://192.168.55.3:8087/`: рабочий каталог/счётчики, честный статус ожидания
   контракта 1С, реальные агрегаты событий. Ключей в странице и network responses нет.
2. В обычном режиме источник/время/расписание недоступны для изменения; в SSH-входе
   оператора доступны JSON/XML, probe, история событий. Публичный 8087 не выдаёт ticket.
3. Импортировать JSON Extracting v2: имя файла, Brand/Article/Note видны в таблице и
   перевыборе; поиск находит новые реквизиты. Дубли, пробелы, нули и `Quantity="0,5"` сохраняются.
4. Перевыбрать каталог, очистить крестиком, утвердить все строки одним действием.
   Скачать source/result/approved JSON и Excel; исходные поля дословны, пустой выбор — null.
5. Reload сохраняет выбор/approval; чужой owner и истёкшие документы недоступны.
6. После реальной настройки 1С: проверить ручной refresh с 60-секундным cooldown,
   полный импорт/deleted/карантин, событие upsert/delete и повтор того же event_id,
   счётчики applied и одну изменённую позицию Qdrant, затем ежедневную контрольную сверку.

## Файлы

- [.env.example](../.env.example)
- [AGENTS.md](../AGENTS.md)
- [README.md](../README.md)
- [alembic/versions/0004_catalog_events.py](../alembic/versions/0004_catalog_events.py)
- [docker-compose.yml](../docker-compose.yml)
- [docs/API.md](../docs/API.md)
- [docs/DASHBOARD_AND_SYNC.md](../docs/DASHBOARD_AND_SYNC.md)
- [docs/INTEGRATION_1C.md](../docs/INTEGRATION_1C.md)
- [docs/LLM_CONTEXT.md](../docs/LLM_CONTEXT.md)
- [docs/RELEASE_V4.md](../docs/RELEASE_V4.md)
- [docs/TECHNICAL_SPECIFICATION_V4.md](../docs/TECHNICAL_SPECIFICATION_V4.md)
- [docs/V4_GAP_ANALYSIS.md](../docs/V4_GAP_ANALYSIS.md)
- [docs/architecture.md](../docs/architecture.md)
- [docs/operations.md](../docs/operations.md)
- [scripts/up.sh](../scripts/up.sh)
- [src/application/catalog.py](../src/application/catalog.py)
- [src/application/catalog_events.py](../src/application/catalog_events.py)
- [src/application/event_ports.py](../src/application/event_ports.py)
- [src/application/mappings.py](../src/application/mappings.py)
- [src/application/ports.py](../src/application/ports.py)
- [src/application/processing.py](../src/application/processing.py)
- [src/application/reports.py](../src/application/reports.py)
- [src/application/sync_ports.py](../src/application/sync_ports.py)
- [src/bootstrap.py](../src/bootstrap.py)
- [src/cli.py](../src/cli.py)
- [src/config.py](../src/config.py)
- [src/domain/catalog_sync.py](../src/domain/catalog_sync.py)
- [src/domain/matching.py](../src/domain/matching.py)
- [src/domain/models.py](../src/domain/models.py)
- [src/infrastructure/catalog_events.py](../src/infrastructure/catalog_events.py)
- [src/infrastructure/catalog_sources.py](../src/infrastructure/catalog_sources.py)
- [src/infrastructure/database.py](../src/infrastructure/database.py)
- [src/infrastructure/json_catalog.py](../src/infrastructure/json_catalog.py)
- [src/infrastructure/repositories.py](../src/infrastructure/repositories.py)
- [src/infrastructure/secrets.py](../src/infrastructure/secrets.py)
- [src/infrastructure/sync_repository.py](../src/infrastructure/sync_repository.py)
- [src/infrastructure/telemetry.py](../src/infrastructure/telemetry.py)
- [src/main.py](../src/main.py)
- [src/presentation/catalog_events.py](../src/presentation/catalog_events.py)
- [src/presentation/dashboard_schemas.py](../src/presentation/dashboard_schemas.py)
- [src/presentation/middleware.py](../src/presentation/middleware.py)
- [src/presentation/routes.py](../src/presentation/routes.py)
- [src/presentation/schemas.py](../src/presentation/schemas.py)
- [src/web/static/css/blocks/catalog-events.css](../src/web/static/css/blocks/catalog-events.css)
- [src/web/static/css/blocks/drawer.css](../src/web/static/css/blocks/drawer.css)
- [src/web/static/css/blocks/table.css](../src/web/static/css/blocks/table.css)
- [src/web/static/css/blocks/workspace.css](../src/web/static/css/blocks/workspace.css)
- [src/web/static/css/style.css](../src/web/static/css/style.css)
- [src/web/static/js/app.js](../src/web/static/js/app.js)
- [src/web/static/js/catalog-events.js](../src/web/static/js/catalog-events.js)
- [src/web/static/js/mapping.js](../src/web/static/js/mapping.js)
- [src/web/static/js/selection.js](../src/web/static/js/selection.js)
- [src/web/static/js/source-fields.js](../src/web/static/js/source-fields.js)
- [src/web/static/js/sync-settings.js](../src/web/static/js/sync-settings.js)
- [src/web/templates/base.html](../src/web/templates/base.html)
- [src/web/templates/components/catalog_events.html](../src/web/templates/components/catalog_events.html)
- [src/web/templates/components/sync_panel.html](../src/web/templates/components/sync_panel.html)
- [src/web/templates/mapping.html](../src/web/templates/mapping.html)
- [src/web/templates/selection.html](../src/web/templates/selection.html)
- [src/worker.py](../src/worker.py)
- [tests/fixtures/onec_mock_server.py](../tests/fixtures/onec_mock_server.py)
- [tests/frontend/source-fields.test.js](../tests/frontend/source-fields.test.js)
- [tests/test_bootstrap_security.py](../tests/test_bootstrap_security.py)
- [tests/test_browser_dashboard.py](../tests/test_browser_dashboard.py)
- [tests/test_browser_v4.py](../tests/test_browser_v4.py)
- [tests/test_bulk_approval.py](../tests/test_bulk_approval.py)
- [tests/test_catalog_events.py](../tests/test_catalog_events.py)
- [tests/test_catalog_http_source.py](../tests/test_catalog_http_source.py)
- [tests/test_catalog_json.py](../tests/test_catalog_json.py)
- [tests/test_catalog_resume.py](../tests/test_catalog_resume.py)
- [tests/test_cli_worker.py](../tests/test_cli_worker.py)
- [tests/test_dashboard_migrations.py](../tests/test_dashboard_migrations.py)
- [tests/test_specification_v4.py](../tests/test_specification_v4.py)
- [tests/test_v4_live.py](../tests/test_v4_live.py)

