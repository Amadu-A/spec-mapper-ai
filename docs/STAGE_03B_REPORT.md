<!-- docs/STAGE_03B_REPORT.md -->
# Stage 03B: изменения, проверки и выпуск

Дата проверки: 25.11.2026. Разработка выполнена в Windows checkout Mapper.
Commit/push/merge, production deployment, firewall и изменение shared stack не выполнялись.

## Проверенная основа и gap analysis

| Компонент | Проверенное состояние перед изменениями |
|---|---|
| Mapper local/GitHub mirror/server feature | `54b5dd98d2f1cce3baa00f491aed7abf612cde62`, `feature/invoice_mapping` |
| Mapper GitHub main | `b607270839ee03d75b17c7241f710dc88303f48e` |
| Extracting accepted main/server | `de7c948c14d147724c41e57d0b12054d12663809` |
| Shared main/reference docs | `dee0a2a17319a24d565d5ba1cabd4b7d477c1d74` |

Начальное дерево Mapper чистое; branch не переключалась. Изучены AGENTS, LLM_CONTEXT,
Engineering/Frontend/Infrastructure/services.yaml, профильные контракты, код и тесты.
Локальные незакоммиченные изменения соседнего Extracting не менялись. Их контрактные
файлы сверены с принятым de7c948; владелец подтвердил приёмку и развёртывание Stage 03A.
Read-only проверка реального OpenAPI 8083 подтвердила snapshot/events/cancel обоих профилей.
Правильный readiness upstream: `/api/v1/health/ready`, у Mapper — `/health/ready`.

| Пробел исходного кода | Изменение |
|---|---|
| Request busy освобождал форму после 202 | Общий logical submit state, стабильный key, явное новое задание, защита от позднего POST |
| Разные ключи создавали одинаковый active вход | Атомарный fingerprint и durable aliases, owner/profile isolation |
| Неограниченный admission | Настраиваемые active/queued/owner/temporary budgets, 429/Retry-After |
| Вкладка теряла активный job; ссылки списка только mappings | PostgreSQL history/restore, profile-safe UI/API/export links |
| 0/0 и потерянный per-file прогресс | Allowlist 8083 metadata, компактная карточка, неизвестные totals отдельно |
| Нет общей отмены и журнала | Durable cancel, snapshot/SSE replay с отдельным cursor, bounded fallback |
| Published queued повторялся каждые 30 секунд | Transport reservation/generation/receipt/confirm отделены от business state |
| Ранний ACK дубля мог лишить job последнего сообщения | Durable receipt и recovery истёкшей lease независимо от ACK |

Архитектура сохранена: общие domain/application lifecycle/delivery, SQL и broker в
infrastructure, тонкие API adapters, Jinja/BEM/vanilla JS. Matching/rerank, Top-5,
ручной выбор/null, финансовые и specification exports, каталог/webhook/scheduler,
порты 8087/8088 и TTL не переписаны. Новых секретов нет; `.env` не изменялся.

## Результаты

Подробные внутренние журналы — в игнорируемом `.cache/stage03b/`, успешные прогоны
не засоряют ответ. Пользовательские команды ниже выводят логи обычным образом.

| Проверка | Результат |
|---|---|
| Baseline 54b5dd9, Windows check-quality | 316 pytest, 12 live deselected, 18 Node, 87% coverage |
| Windows окончательный check-quality.ps1 | 337 pytest (включая 17 browser E2E), 12 live deselected, 23 Node, 87% coverage; Ruff/format/diff check pass |
| Дополнительный browser regression после очистки TTL/смены job | 16 E2E pass |
| Новый SSE EOF/fallback + private DOM expiry и прочие lifecycle E2E | 4 pass; browser HTTP/SQL, внешние AI/broker doubles |
| Linux окончательный test.sh | 336 passed, 1 skipped (приватный corpus отсутствует в образе), 12 live deselected; 23 Node; Ruff/format/diff pass, coverage 87% |
| PostgreSQL 0005→0007 в отдельной QA schema | Исторические job/PDF/approval сохранены |
| PostgreSQL, 10 независимых SqlJobs instances | 1 active job, 1 reservation; owner admission 429; реальное matching/AMQP |
| Реальная спецификация 8087→8083→8087 | 5 позиций, VLM/embedding/Qdrant, approve/JSON/Excel и terminal SSE |
| Реальный пакет двух счетов | 7 позиций, VLM/embedding/Qdrant, review/null/approve/exports |
| Все 7 JSON эталонов | 161 строка; исходные значения/дубли/финансы сохранены |
| Managed cancel | Реальный 8083 подтвердил cancelled; TTL сохранён |
| SIGKILL QA worker после принятия upstream | Естественное истечение lease, 1 recovery, тот же upstream, approved 7 позиций |
| Пересоздание QA API/worker | История и 10 approved результатов сохранились, Excel доступны после recreate |

Первый полный прогон выявил пропущенный HTML-фильтр профиля (две регрессии главной).
DOM исправлен; соответствующие тесты главной, public refresh и lifecycle повторно прошли.
Ошибки не объявлялись успешными результатами.

### Доказательство устранения накопления

Оба сценария используют реальные PostgreSQL и RabbitMQ, собственную schema/queue,
5 уникальных технических заданий, 2 удерживаемых Unacked/lease и 3 ожидающих задания.
Исходный сценарий не выполняет VLM или каталог; это transport fixture.

| Код | Длительность | Начальный Ready | Конечный Ready | Positive confirms | Уникальные jobs |
|---|---:|---:|---:|---:|---:|
| До исправления 54b5dd9 | 660,1 с | 3 | 66 | 68 | 5 |
| После исправления | 660,1 с | 3 | 3 | 5 | 5 |

Не менее 20 прежних интервалов по 30 секунд проверены live и отдельно с управляемыми
часами offline. Concurrency/prefetch=2 сохранены. Это не сравнение двух пустых очередей.
Журналы: `baseline-live-delivery.log`, `after-live-delivery.log`.

### Отказы и границы достоверности

| Сценарий | Фактическая проверка |
|---|---|
| Потерянный confirm | Реальный broker ACK, затем fault injection до SQL confirm commit; receipt не стёрт |
| Mandatory return | Настоящая публикация в отсутствующий маршрут собственного namespace; retry восстановлен |
| Broker NACK | Одноразовая QA queue с reject-publish, без изменения production queue/shared policy |
| Потеря соединения | Закрыто собственное AMQP соединение; durable сообщение сохранилось после reconnect |
| Publisher timeout | Клиентская fault injection с реальным PostgreSQL; pending/backoff, job не потерян |
| Смерть до confirm/reservation timeout/stale CAS | Offline контролируемые часы и конкурирующие reservations |
| SIGKILL после receipt, ранний ACK redelivery | Реальный отдельный процесс/AMQP/PG; в узком fixture expiry lease внедрён только в QA SQL |
| SIGKILL настоящего QA worker при VLM | Естественные 120 секунд lease и recovery того же upstream; doubles не использовались |
| SQL checkpoint до consumer ACK | Реальные checkpoint, reject/requeue и terminal redelivery noop |
| Старое UUID-only сообщение | Реальный AMQP, terminal duplicate noop |
| Cancel во время POST/неизвестный ответ/approve race | Контрактные offline tests, late ID и immutable snapshot |
| SSE обрыв/replay/лимиты/TTL | Настоящий Chromium EOF/fallback и API tests; это не тест сети production |

Потеря confirm моделировалась на клиентской границе, а не удалением конкретного TCP
кадра. Shared broker не перезапускался. Тесты с upstream doubles не названы реальной
интеграцией. Журналы `live-profiles.log`, `live-cancel.log`, `live-corpus.log`,
`live-worker-restart.log`, `live-replicas.log`, `live-faults-3.log` разделены.

Синтетические screenshots (без содержимого реальных счетов):
`.cache/stage03b/screenshots/invoice-queued-desktop.png`, `invoice-queued-mobile.png`.
Desktop 1366×768: cancel, карточка трёх файлов, шесть KPI и начало таблицы доступны;
mobile 390 px не создаёт горизонтальный overflow страницы. Финансовая таблица имеет
собственную горизонтальную прокрутку. Browser screenshots просмотрены визуально.

Реальные 7 PDF + 7 JSON передавались с отдельного разрешения владельца только в
`/tmp/spec-mapper-invoice-qa-e3702f308d894853/corpus`, mode 0600, read-only bind.
Рабочие checkout сервера не менялись; каталог использовался через read-only pool.
QA schema/queue/контейнеры/образ и временный root с corpus/private.env удалены.
Отдельно проверено отсутствие root/QA контейнеров. Production HEAD остался 54b5dd9,
Extracting — de7c948, оба ready. После cleanup PostgreSQL/Qdrant сохранили версию
`b74fe58c-c425-4f1f-a45d-8adb16740fd5` и по 62 048 записей/точек.
Локальный временный архив реального corpus удалён; оригиналы не изменялись.
Также удалены 18 локальных basetemp каталогов с временными тестовыми БД.
Сохранены журналы и синтетические screenshots.

## Ручной выпуск Linux

Выполняет владелец после ручного commit/push текущей feature-ветки. Сначала проверить
принятый Stage 03A на 8083 и согласовать короткое окно остановки собственных API.
Никаких действий с firewall/shared services, queue purge/get production, volumes или
ручного UPDATE чужих jobs. Старые и новые worker одновременно не запускаются.

```bash
cd ~/projects/spec-mapper-ai
git status --short
git branch --show-current
git log -1 --oneline
git pull --ff-only origin feature/invoice_mapping
bash scripts/test.sh
compose() { docker compose --env-file .env.example --env-file .env "$@"; }
compose build api

# Закрыть приём; worker пока завершает уже принятые задания.
compose stop api operator-gateway
compose logs --tail=100 worker
```

Перед остановкой worker проверить только технические metadata старой схемы:

```bash
compose exec -T worker /app/.venv/bin/python - <<'PY'
import asyncio, json
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from src.config import Settings
async def check():
    engine=create_async_engine(Settings().database_url)
    try:
        async with engine.connect() as connection:
            rows=await connection.execute(text("""SELECT id,kind,state,stage,expires_at,
              lease_until,dispatched_at FROM mapping_jobs
              WHERE expires_at>extract(epoch FROM now()) ORDER BY created_at LIMIT 1000"""))
            print(json.dumps([dict(r) for r in rows.mappings()],ensure_ascii=False))
    finally:
        await engine.dispose()
asyncio.run(check())
PY
```

Если есть активная обработка или sync, дать ей завершиться; не объявлять сообщения
Ready уникальными jobs. Не отменять лишние задания без решения их владельца.
После drain/проверки можно продолжить:

```bash
compose stop worker
mkdir -p "$HOME/backups/spec-mapper-ai"
backup="$HOME/backups/spec-mapper-ai/pre-0007-$(date -u +%Y%m%dT%H%M%SZ).dump"
compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup"
test -s "$backup"
compose run --rm --no-deps migrate
bash scripts/up.sh
compose ps
compose logs --tail=100 api worker migrate
curl --fail http://127.0.0.1:8087/health/ready
curl --fail http://127.0.0.1:8087/api/openapi.json
compose exec -T api /app/.venv/bin/python -m src.cli delivery-status
bash scripts/test.sh --live
```

Продолжать только после успешного результата каждой команды. Offline test.sh до up
тестирует собственный образ с `--network none`, поэтому не требует поднятого Mapper.
up.sh остаётся единственной командой запуска/пересборки; live выполняется после up.
Команды намеренно не скрывают логи.

Если обнаружены `legacy_unknown`, сначала проверить drain/отсутствие сообщения и
действующей lease. Только затем для **конкретного UUID**, не раньше пяти минут:

```bash
compose exec -T api /app/.venv/bin/python -m src.cli delivery-reconcile --job-id '<проверенный UUID>'
```

Результат сообщает scheduled/unchanged. Не запускать команду по таймеру или на всех jobs.
Если bootstrap каталога завершается ошибкой HTTP 1С, сохранить guard, источник, staging,
PostgreSQL/Qdrant и диагностировать этот импорт отдельно. Не сбрасывать каталог и
не отменять shrink/transport protections ради readiness.

При проблеме нового кода остановить только свои API/worker, сохранить backup/очередь
и исправлять вперёд. Downgrade 0007 или запуск старого worker после cancel/aliases
не являются безопасным автоматическим rollback.

## Ручная проверка UI

1. На `/invoices` загрузить 2–3 разрешённых PDF с разными физическими страницами.
   Повторные клики не создают пакет; видны queue/transfer/upstream queue/extract/match.
   До получения totals нет 0/0. Cancel расположен в карточке, KPI компактны.
2. Перейти в спецификации и обратно, нажать F5: открыть тот же job без нового POST.
   «Новое задание» начинает независимый пакет; старый остаётся в истории.
3. Проверить журнал, краткий обрыв связи и восстановление. Уход со страницы не
   останавливает worker. Для внешнего UUID отменяется только Mapper.
4. На собственном тестовом job проверить queued/running cancel; до подтверждения
   8083 статус cancelling. Approved отменить/изменить нельзя.
5. Проверить Top-5, поиск, перевыбор/null, preview, финансы и acknowledgment, approve,
   JSON/Excel обоих профилей. `/jobs` показывает профиль и правильные адреса exports.
6. Проверить 404 чужого job и TTL; после истечения таблица/dialog/preview очищаются.
   Главная, расписание/ручной refresh и доступ оператора продолжают работать.

## Файлы и Windows публикация

Перечень и точный staging приведены ниже; commands предназначены только владельцу.

```powershell
Set-Location C:\Users\i.mein\PycharmProjects\spec-mapper-ai
.\ops\check-quality.ps1
git status --short
git branch --show-current
git diff --check
$paths = @(
    '.env.example'
    'README.md'
    'alembic/versions/0007_job_lifecycle_delivery.py'
    'docker-compose.yml'
    'docs/API.md'
    'docs/JOB_LIFECYCLE.md'
    'docs/LLM_CONTEXT.md'
    'docs/STAGE_03B_JOB_LIFECYCLE_UI.md'
    'docs/STAGE_03B_REPORT.md'
    'docs/operations.md'
    'src/application/delivery.py'
    'src/application/invoices/ports.py'
    'src/application/invoices/service.py'
    'src/application/lifecycle.py'
    'src/application/mappings.py'
    'src/application/ports.py'
    'src/application/processing.py'
    'src/application/specifications.py'
    'src/bootstrap.py'
    'src/cli.py'
    'src/config.py'
    'src/domain/lifecycle.py'
    'src/domain/models.py'
    'src/infrastructure/broker.py'
    'src/infrastructure/database.py'
    'src/infrastructure/http_clients.py'
    'src/infrastructure/invoices/extracting.py'
    'src/infrastructure/job_delivery.py'
    'src/infrastructure/repositories.py'
    'src/infrastructure/sync_repository.py'
    'src/main.py'
    'src/observability.py'
    'src/presentation/dashboard_routes.py'
    'src/presentation/dashboard_schemas.py'
    'src/presentation/invoice_schemas.py'
    'src/presentation/lifecycle.py'
    'src/presentation/lifecycle_schemas.py'
    'src/presentation/schemas.py'
    'src/presentation/specifications.py'
    'src/web/static/css/blocks/job-lifecycle.css'
    'src/web/static/css/blocks/page.css'
    'src/web/static/css/style.css'
    'src/web/static/js/app.js'
    'src/web/static/js/dashboard-state.js'
    'src/web/static/js/invoice-state.js'
    'src/web/static/js/invoices.js'
    'src/web/static/js/job-state.js'
    'src/web/static/js/job-workspace.js'
    'src/web/static/js/jobs.js'
    'src/web/static/js/mapping.js'
    'src/web/static/js/selection.js'
    'src/web/static/js/state.js'
    'src/web/templates/base.html'
    'src/web/templates/components/job_lifecycle.html'
    'src/web/templates/components/jobs_table.html'
    'src/web/templates/invoices.html'
    'src/web/templates/mapping.html'
    'src/worker.py'
    'tests/frontend/job-lifecycle.test.js'
    'tests/live_invoice_runtime.py'
    'tests/live_job_delivery.py'
    'tests/live_job_lifecycle.py'
    'tests/live_job_replicas.py'
    'tests/test_api.py'
    'tests/test_browser_job_lifecycle.py'
    'tests/test_catalog_events.py'
    'tests/test_catalog_resume.py'
    'tests/test_cli_worker.py'
    'tests/test_dashboard_migrations.py'
    'tests/test_invoice_contracts.py'
    'tests/test_job_delivery.py'
    'tests/test_job_lifecycle.py'
    'tests/test_job_lifecycle_migration.py'
    'tests/test_specification_v4.py'
)
git add -- $paths
git diff --cached --stat
git diff --cached --check
git commit -m "feat: add durable job lifecycle, progress and recovery"
git push origin HEAD:feature/invoice_mapping
```

Staging содержит только файлы Stage 03B. Не добавлять `.env`, каталог, examples,
.cache, test-results или реальные документы. Main не переключать и PR не сливать
без отдельного решения владельца.
