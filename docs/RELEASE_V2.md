<!-- docs/RELEASE_V2.md -->
# Проверка и выпуск главной V2

База: `677f1d2298cd9786ca927b876143afad0e6e57f4`, ветка `main`.
Первичный [gap analysis](DASHBOARD_GAP_ANALYSIS.md),
[API/настройки/безопасность/метрики](DASHBOARD_AND_SYNC.md).
Агент не выполнял staging/commit/push или production deployment.

## Проверенные результаты

- До изменений Windows quality: 85 Python + 6 Node, 80% покрытия.
- Windows quality V2: 136 Python (включая два Chromium E2E), 10 Node,
  Ruff/форматирование/diff check; 82% покрытия. Внешние AI/broker в браузерных
  тестах явно fake; это не live тест 1С.
- Linux offline: 134 passed; Ruff/format и Compose config --quiet успешны.
- Linux Docker build test/runtime успешен. Реальные PostgreSQL/Qdrant/RabbitMQ
  работают в отдельных временных контейнерах с tmpfs, shared-embedding используется
  как реальный внешний сервис. Финальный live suite: **8 passed, 1 skipped**.
- Проверены additive 0002→0003 с прежним catalog/job/approval, конкурирующие PG
  scheduler replicas, реальный ручной импорт, идемпотентность, отчёты JSON/XLSX,
  пропущенный плановый слот через worker/RabbitMQ, отсутствие повторной векторизации,
  сохранение SQL настроек после рестарта тестовых API/worker; ready=200.
- Полный реальный XML прочитан только read-only: 65 493 строки, 3 436 удалённых,
  карантин 6 строк трёх конфликтующих кодов, 62 052 активных. Полная повторная
  векторизация этих 62 052 позиций в V2 тесте не выполнялась; live pipeline проверен
  на небольшом синтетическом справочнике. Production-каталог уже завершён и готов.
- Единственный пропуск live: polling конкретного upstream extraction job без
  переданного `--source-job-id`; для проверки нужен актуальный UUID, принадлежащий
  configured upstream API key. Offline polling/retry/owner регрессии прошли.
- ID и StartedAt production mapper/PG/Qdrant и shared RabbitMQ/embedding до/после
  тестов совпадают. Production checkout, firewall и persistent volumes не изменялись. Временные тестовые
  контейнеры/сеть удалены; production ready=200, версия/62 052 позиции сохранены.

Внутренние логи успешных прогонов — `.v2-quality.log`, `.v2-remote-build.log`,
`.v2-remote-live.log`, `.v2-final-live.log`, `.v2-linux-offline.log`, `.v2-cleanup.log`; они игнорируются Git.
Chromium screenshots — `test-results/browser-dashboard.png`,
`test-results/browser-dashboard-mobile.png` и прежние mapping/selection/approval.
Визуальная проверка выполнена для desktop и mobile, горизонтальный overflow
страницы проверяется тестом; таблица прокручивается внутри собственного контейнера.

## Windows PowerShell: проверка владельцем

```powershell
Set-Location C:\Users\i.mein\PycharmProjects\spec-mapper-ai
.\ops\check-quality.ps1
git diff --check
git diff --stat
git status --short
```

Первая команда уже включает Ruff, format --check, pytest с coverage, Chromium и
Node. Для отдельного повторения нужной группы (вывод не скрывается):

```powershell
uv run --no-sync ruff check src tests alembic
uv run --no-sync ruff format --check src tests alembic
uv run --no-sync pytest -m 'not live and not e2e' -q
uv run --no-sync pytest -m e2e -q
node --test tests/frontend/*.test.js
```

## Linux: безопасное обновление владельцем

Перед pull убедиться, что текущий импорт завершён. Открыть действующий интерфейс
и проверить статус либо выполнить read-only проверку active_catalog ниже. Если
есть queued/running/retry job, дождаться его завершения; staging не удалять.
При dirty checkout остановиться и разобрать локальные изменения, не делать reset.

```bash
cd ~/projects/spec-mapper-ai
git status --short
docker compose --env-file .env.example --env-file .env exec -T api /app/.venv/bin/python - <<'PY'
import asyncio
from src.bootstrap import build_container
from src.config import Settings
from src.domain.models import TERMINAL
from src.infrastructure.database import JobRow
from sqlalchemy import select
async def main():
    c = build_container(Settings())
    try:
        async with c.jobs.sessions() as session:
            job = await session.scalar(select(JobRow).where(
                JobRow.kind == 'catalog', JobRow.state.not_in(TERMINAL),
                JobRow.expires_at > c.jobs.clock(),
            ).limit(1))
        print({k: getattr(job, k) for k in ('id', 'state', 'stage')} if job else 'Активного импорта нет')
        if job:
            raise SystemExit('Дождитесь текущего импорта перед обновлением')
    finally:
        await c.close()
asyncio.run(main())
PY
PREVIOUS_HEAD=$(git rev-parse HEAD)
git pull --ff-only origin main
docker compose --env-file .env.example --env-file .env config --quiet
bash scripts/up.sh
curl --fail --show-error http://127.0.0.1:8087/health/live
curl --fail --show-error http://127.0.0.1:8087/health/ready
docker compose --env-file .env.example --env-file .env logs --tail=100 api worker
```

Каждую следующую операцию выполнять только после успешной предыдущей. `up.sh`
применяет additive миграцию и поднимает только собственный stack; не требует
удаления volumes, повторного создания ключей или перезапуска shared. Новый
`catalog_staging` volume принадлежит только mapper. Порт остаётся **8087**.

Linux проверка одной существующей командой (нужны host uv, Node и browser deps):

```bash
bash scripts/test.sh
```

Дополнительно `bash scripts/test.sh --live` проверяет реальные зависимости. Для
polling конкретного принадлежащего ключу задания можно передать UUID явно:

```bash
docker compose --env-file .env.example --env-file .env run --build --rm --no-deps tests /app/.venv/bin/pytest -m live --source-job-id UUID_ГОТОВОГО_ЗАДАНИЯ -q
```

Тест изменения глобального расписания намеренно пропускается вне специального
изолированного `mapper-v2-test-*` окружения; production-настройки тестами не меняются.
Обычные live тесты работают в собственных smoke схемах/коллекциях/очередях.

## Что проверить руками

1. `http://192.168.55.3:8087/`: настоящие count/версия/UTC дата и пять независимых
   компонентов. Анонимная сессия не может менять расписание/источник/запускать импорт.
   Пустая статистика не подменяется цифрами эталона, scope подписан.
2. Создать SSH-туннель, получить operator-ticket и войти на `/operator`. На `/settings`
   сохранить OFF/время, reload: значения остаются. При local XML включение разрешено
   после проверки файла. При HTTP 1С без контракта виден статус ожидания, включение
   и запуск заблокированы. Никакие пароли/ключи не вводятся в браузер.
3. Ручное обновление локального XML: один job, прогресс после reload, финальный отчёт,
   количество/карантин/векторы/последняя попытка отдельно от успеха. Не запускать
   второй импорт во время первого. Неизменный XML не должен заново считать векторы.
4. Импортировать JSON или передать свой extracting UUID; открыть задание, Top-5,
   поиск всего каталога, manual/null, reload, фильтры/пагинация, approve и четыре
   экспорта. Исходные пять полей остаются строками без изменений.
5. Задания/общий обзор оператора, копирование UUID, сортировка, история, отчёты,
   аудит, `/docs`, OpenAPI, инструкция и `?`; reload/back/forward и мобильный экран.
   Чужой API job виден только как метаданные и не открывает документ.
6. Logout/истечение роли: global management запрещён; обычный owner сохраняется.

## Rollback

При неудачном обновлении сохранить диагностику и дождаться активного импорта.
Вернуться к записанному PREVIOUS_HEAD без миграционного downgrade и без удаления
volumes. Дополнительные V2 таблицы не мешают старому коду:

```bash
git status --short
git switch --detach "$PREVIOUS_HEAD"
bash scripts/up.sh
```

Не выполнять `down -v`, `volume rm`, `prune`, reset БД/Qdrant, удаление staging или
переиндексацию ради отката UI. После возврата на V2 вновь используется прежняя SQL
конфигурация расписания; для rollback заранее выключить автообновление, если возможно.

## Что ещё требуется от 1С

Реальная интеграция HTTP заблокирована отсутствием согласованных URL, метода,
авторизации, XML/XSD/GUID/deleted semantics, гарантии полного snapshot/completion,
лимитов и сетевого/TLS доступа. Mock HTTP тесты не являются live подключением 1С.
Существующий клиентский mapper API для 1С/бота полностью сохранён.


## Файлы и явный staging/commit/push владельцем

Все затронутые файлы (79):

- `.dockerignore`
- `.env.example`
- `.gitignore`
- `AGENTS.md`
- `Dockerfile`
- `README.md`
- `alembic/versions/0003_catalog_sync_dashboard.py`
- `docker-compose.yml`
- `docs/API.md`
- `docs/DASHBOARD_AND_SYNC.md`
- `docs/DASHBOARD_GAP_ANALYSIS.md`
- `docs/INTEGRATION_1C.md`
- `docs/LLM_CONTEXT.md`
- `docs/RELEASE_V2.md`
- `docs/TECHNICAL_SPECIFICATION.md`
- `docs/architecture.md`
- `docs/operations.md`
- `renders/render_v2.png`
- `src/application/catalog.py`
- `src/application/catalog_management.py`
- `src/application/processing.py`
- `src/application/reports.py`
- `src/application/sync_ports.py`
- `src/bootstrap.py`
- `src/cli.py`
- `src/config.py`
- `src/domain/catalog_sync.py`
- `src/infrastructure/catalog_sources.py`
- `src/infrastructure/database.py`
- `src/infrastructure/integration_status.py`
- `src/infrastructure/operator_access.py`
- `src/infrastructure/repositories.py`
- `src/infrastructure/sync_repository.py`
- `src/infrastructure/telemetry.py`
- `src/main.py`
- `src/presentation/dashboard_routes.py`
- `src/presentation/dashboard_schemas.py`
- `src/presentation/middleware.py`
- `src/presentation/operator_security.py`
- `src/presentation/routes.py`
- `src/presentation/security.py`
- `src/web/static/css/blocks/dashboard.css`
- `src/web/static/css/blocks/navigation.css`
- `src/web/static/css/style.css`
- `src/web/static/icons.svg`
- `src/web/static/js/api.js`
- `src/web/static/js/app.js`
- `src/web/static/js/catalog.js`
- `src/web/static/js/dashboard-state.js`
- `src/web/static/js/dashboard.js`
- `src/web/static/js/events.js`
- `src/web/static/js/jobs.js`
- `src/web/static/js/operator.js`
- `src/web/static/js/sync-settings.js`
- `src/web/templates/base.html`
- `src/web/templates/catalog.html`
- `src/web/templates/components/icon.html`
- `src/web/templates/components/jobs_table.html`
- `src/web/templates/components/sync_panel.html`
- `src/web/templates/events.html`
- `src/web/templates/help.html`
- `src/web/templates/home.html`
- `src/web/templates/integrations.html`
- `src/web/templates/jobs.html`
- `src/web/templates/mapping.html`
- `src/web/templates/operator.html`
- `src/web/templates/settings.html`
- `src/worker.py`
- `tests/conftest.py`
- `tests/frontend/dashboard.test.js`
- `tests/test_browser.py`
- `tests/test_browser_dashboard.py`
- `tests/test_catalog_http_source.py`
- `tests/test_cli_worker.py`
- `tests/test_dashboard.py`
- `tests/test_dashboard_live.py`
- `tests/test_dashboard_migrations.py`
- `tests/test_integration_indicators.py`
- `tests/test_scheduler.py`

После проверки выполните сами:

```powershell
git add -- `
  .dockerignore .env.example .gitignore `
  AGENTS.md Dockerfile README.md `
  alembic/versions/0003_catalog_sync_dashboard.py docker-compose.yml docs/API.md `
  docs/DASHBOARD_AND_SYNC.md docs/DASHBOARD_GAP_ANALYSIS.md docs/INTEGRATION_1C.md `
  docs/LLM_CONTEXT.md docs/RELEASE_V2.md docs/TECHNICAL_SPECIFICATION.md `
  docs/architecture.md docs/operations.md renders/render_v2.png `
  src/application/catalog.py src/application/catalog_management.py src/application/processing.py `
  src/application/reports.py src/application/sync_ports.py src/bootstrap.py `
  src/cli.py src/config.py src/domain/catalog_sync.py `
  src/infrastructure/catalog_sources.py src/infrastructure/database.py src/infrastructure/integration_status.py `
  src/infrastructure/operator_access.py src/infrastructure/repositories.py src/infrastructure/sync_repository.py `
  src/infrastructure/telemetry.py src/main.py src/presentation/dashboard_routes.py `
  src/presentation/dashboard_schemas.py src/presentation/middleware.py src/presentation/operator_security.py `
  src/presentation/routes.py src/presentation/security.py src/web/static/css/blocks/dashboard.css `
  src/web/static/css/blocks/navigation.css src/web/static/css/style.css src/web/static/icons.svg `
  src/web/static/js/api.js src/web/static/js/app.js src/web/static/js/catalog.js `
  src/web/static/js/dashboard-state.js src/web/static/js/dashboard.js src/web/static/js/events.js `
  src/web/static/js/jobs.js src/web/static/js/operator.js src/web/static/js/sync-settings.js `
  src/web/templates/base.html src/web/templates/catalog.html src/web/templates/components/icon.html `
  src/web/templates/components/jobs_table.html src/web/templates/components/sync_panel.html src/web/templates/events.html `
  src/web/templates/help.html src/web/templates/home.html src/web/templates/integrations.html `
  src/web/templates/jobs.html src/web/templates/mapping.html src/web/templates/operator.html `
  src/web/templates/settings.html src/worker.py tests/conftest.py `
  tests/frontend/dashboard.test.js tests/test_browser.py tests/test_browser_dashboard.py `
  tests/test_catalog_http_source.py tests/test_cli_worker.py tests/test_dashboard.py `
  tests/test_dashboard_live.py tests/test_dashboard_migrations.py tests/test_integration_indicators.py `
  tests/test_scheduler.py
git diff --cached --check
git diff --cached --stat
git commit -m "feat: главная страница и управляемая синхронизация каталога"
git push git@github.com:Amadu-A/spec-mapper-ai.git main
git push git@github.com:neo-term-it/spec-mapper-ai.git main
```
