<!-- docs/INVOICE_STAGE_02_REPORT.md -->
# Этап 02: реализация и проверка профиля счетов

Проверки: 23–24.11.2026. Разработка выполнена в Windows checkout Mapper; commit,
push и production deployment не выполнялись. На сервер передавалась только временная
копия кода и отдельно разрешённые владельцем 14 файлов корпуса. Extracting и shared
инфраструктура не изменялись. [ТЗ](INVOICE_MAPPING_STAGE_02.md),
[исходный gap analysis](INVOICE_STAGE_02_GAP_ANALYSIS.md), [контракт 1С](INVOICE_API.md).

## База и изменения

| Источник | Проверенный main/HEAD |
|---|---|
| Mapper, включая origin mirror | `b607270839ee03d75b17c7241f710dc88303f48e` |
| Extracting | `9f4c911642a868cf4227ddedaec4ff6b919b6e5d` |
| shared-infrastructure | `dee0a2a17319a24d565d5ba1cabd4b7d477c1d74` |

Добавлен независимый `/api/v1/invoices` и экран `/invoices` на прежнем 8087.
Multipart 1–10 файлов и прямой JSON — два входа одного процесса. Пакет blobs/job/outbox
сохраняется атомарно до 202; повторный вызов и восстановление после сбоя используют
сохранённые checkpoints и стабильный upstream ключ. Получается только конечный JSON 8083.

Общий алгоритм embeddings/Top-5/rerank/choose вынесен из существующего worker в одну
функцию и используется обоими профилями. Общие хранилище, lease, outbox, CAS, ручной
выбор/null и массовое immutable approval переиспользованы с прежними defaults для
спецификаций. Платежи не ищутся; услуги и неподтверждённые виды требуют review.
Исходные финансы, реквизиты, страницы и повторы не пересчитываются и не объединяются.
Article-кандидат и код поставщика сохраняются отдельно от подтверждённого артикула.

Результат `invoice_mapping.v1` дополняет исходный `invoice_extraction.v1` полями
`onec`/`matching` и метаданными документов. Реальный GUID 1С отделён от internal UUID.
Финансовое согласие фиксируется по каждому документу, сохраняя исходный validation.
JSON/Excel доступны только после approve. Invoice Excel содержит три листа;
шаблон и все прежние Excel/JSON маршруты спецификаций сохранены.

Миграция 0006 только добавляет `invoice_uploads`. Каталог, Qdrant, scheduler,
именованные ключи, owner/CSRF/TTL, публикация 8087 и loopback 8088 не заменяются.
Новые `INVOICE_*` — обычные лимиты Settings, новых секретов нет; `.env` не изменялся.
Swagger UI 5.36.0 хранится локально с лицензиями и SHA-256 manifest; CDN, внешний
validator и сохранение авторизации отключены. Существующий просмотрщик OpenAPI сохранён.

## Автоматические проверки без реальных внешних моделей

| Проверка | Результат |
|---|---|
| `ops/check-quality.ps1`, Windows Python 3.12 | 316 pytest passed, 12 live deselected |
| `bash scripts/test.sh`, Linux Docker `--network none` | 315 passed, 1 skipped, 12 live deselected |
| Ruff / format | Успешно, 117 Python-файлов |
| Покрытие Python | 87% на Windows и Linux |
| Frontend Node | 18 passed на обеих системах |
| Новые browser E2E | 3 Chromium-сценария, входят в оба gate |
| Runtime build/smoke | Успешно, UID 10001, сеть отключена, PDF/PNG native libraries, OpenAPI и Excel |

Один пропуск Linux — локальный приватный corpus test: `.dockerignore` намеренно
не включает реальные счета в образ. В Windows этот тест прошёл; семь JSON также
проверены отдельно на реальных сервисах. Существующие 12 live pytest не выдаются за
пройденные: они исключены offline gate. Реальные проверки ниже — отдельные сценарии.

Покрыты unit, functional/API, regression, architecture, security и integration:
lossless расширения/пустые значения/дубли, строгие типы/даты/счётчики, финансовый guard,
истинный GUID, Top-5/ручной поиск/null, CAS, owner/IDOR/CSRF/TTL, изоляция профилей,
идемпотентность, повреждённые PDF/изображения/MIME, настоящий порог 50/100 MiB и
100 выбранных страниц без частично сохранённых blobs. Проверены большие книги
на 5000 строк и безопасность формул/длинных decimal-строк, upstream 404/410/502/504,
failed/cancelled, повтор после потерянного ответа и восстановление без нового job 8083.
Browser: загрузка/JSON, выбор/поиск/null, reload, consent/approve, Excel, mobile,
TTL и локальный Swagger под CSP. Внешние AI/HTTP в этих browser-тестах подменены явно.

## Реальная интеграция, отдельно от mocks

Реальный каталог: версия `b74fe58c-c425-4f1f-a45d-8adb16740fd5`, 62 048 записей
PostgreSQL и точек Qdrant. Сверена совместимость embedding identity: 4096 измерений,
revision `a44a46e9b820ccc355dd70819d4f7f4c4980c9ba34ff213b65c8acf172cd464a`.
Каталог использовался только через read-only SQL pool; индексация и коллекции не менялись.

Проверка выполнена во временном Mapper API с внутренним loopback 8087 и worker,
отдельными SQL schema и очередью проекта. Контейнеры использовали network namespace
рабочего API без новых опубликованных портов и без изменения состава shared networks.
Production checkout/API оставались на `b607270`; новый invoice namespace станет
доступен по LAN только после ручного выпуска владельцем.

| Реальный сценарий | Результат |
|---|---|
| PostgreSQL 0005→0006 | Старый synthetic job, PDF и approval сохранены |
| Готовые JSON → настоящий embedding/Qdrant → review/approve/JSON/Excel | 7 файлов, 7 документов, 161 позиция |
| Два PDF → Mapper → RabbitMQ → настоящий 8083/VLM → embedding/Qdrant → review/approve/JSON/Excel | 2 документа, 7 позиций, 74,94 с |
| API/OpenAPI/Swagger assets/preview 8083 proxy | Успешно |
| Неверный ключ / чужая web-сессия / собственный истёкший job | 401 / 404 / 410, включая экспорты |

Для всех семи исходных JSON сравнивались полные hashes source и вложенные структуры
после удаления только добавленных matching-полей. Проверены исходные реквизиты,
суммы, порядок, дубли, число строк Excel; результаты повторно читались из immutable approval.
Ручной QA-выбор использовал реальные кандидаты, поиск по всему справочнику и явный null.

Идентификаторы корпуса ниже — локальные порядковые номера без раскрытия реквизитов:

| Случай | Исходных позиций | Строк с кандидатами | Кандидатов Top-5 | Выборов с real GUID после QA review |
|---|---:|---:|---:|---:|
| 0 | 1 | 1 | 5 | 1 |
| 1 | 59 | 59 | 295 | 53 |
| 2 | 44 | 44 | 220 | 24 |
| 3 | 6 | 6 | 30 | 1 |
| 4 | 1 | 0 | 0 | 0 |
| 5 | 45 | 45 | 225 | 29 |
| 6 | 5 | 5 | 25 | 1 |
| Всего | 161 | 160 | 800 | 109 |

109 выборов — технический результат QA с ручными правками, не метрика точности модели.
Среди 800 кандидатов: 166 конфликтов артикула, 188 единиц измерения,
63 бренда/производителя, 2 технического напора. Один кандидат может иметь несколько
конфликтов; это не число ошибочных исходных строк. Конфликтный кандидат не становится
автоматическим соответствием.
Платёж, услуга, неизвестный вид, отсутствие количества/НДС и missing GUID дополнительно
проверены отдельными синтетическими тестами; не все такие виды есть в реальном корпусе.

Замер admission десяти реальных PDF при остановленном **только QA worker**:
3 083 667 байт приняты и сохранены в PostgreSQL, сумма `pg_column_size(content)` —
3 083 667 байт. Время POST до 202 — 0,120 с, 83,04 файла/с; RSS API
159 728 → 166 880 KiB, HWM после запроса 166 880 KiB. Это приём/проверка/сохранение
пакета, **не** throughput VLM и не пиковая память при максимально допустимых 100 MiB.
Реальное полное извлечение измерено отдельным двухфайловым прогоном выше.

Временные QA контейнеры, очередь, SQL schema, образы и каталог `/tmp` с 14 файлами
и private.env удалены. Проверены отсутствие схемы/контейнеров/каталога, чистый
server checkout на прежнем HEAD, production health ready и неизменные 62 048 записей/точек.
Тестовые jobs в Extracting удаляются его штатным TTL; его БД вручную не менялась.

## Файлы и ручная публикация Windows

Ниже точный набор файлов этого изменения. Команды `git add` одновременно служат
перечнем изменённых/добавленных файлов. Пользовательский исходный
`docs/TZ_spec_mapper_ai_invoice_mapping_stage_02_v1.md`, реальные examples, .env,
логи, caches и test-results в staging не входят.

```powershell
Set-Location C:\Users\i.mein\PycharmProjects\spec-mapper-ai
git status --short
git branch --show-current
git log -1 --oneline
.\ops\check-quality.ps1

git add -- .gitattributes .env.example README.md docker-compose.yml pyproject.toml uv.lock
git add -- alembic/versions/0006_invoice_uploads.py
git add -- docs/API.md docs/UNIFIED_API.md docs/architecture.md docs/operations.md
git add -- docs/INVOICE_MAPPING_STAGE_02.md docs/INVOICE_API.md docs/INVOICE_STAGE_02_GAP_ANALYSIS.md docs/INVOICE_STAGE_02_REPORT.md
git add -- src/application/mappings.py src/application/ports.py src/application/processing.py src/application/invoice_reports.py
git add -- src/application/invoices/__init__.py src/application/invoices/ports.py src/application/invoices/service.py
git add -- src/domain/invoices/__init__.py src/domain/invoices/adapter.py src/domain/invoices/models.py src/domain/invoices/warnings.py
git add -- src/bootstrap.py src/config.py src/main.py
git add -- src/infrastructure/database.py src/infrastructure/http_clients.py src/infrastructure/repositories.py
git add -- src/infrastructure/invoices/__init__.py src/infrastructure/invoices/extracting.py src/infrastructure/invoices/uploads.py
git add -- src/presentation/invoice_schemas.py src/presentation/invoices.py src/presentation/middleware.py src/presentation/routes.py
git add -- src/resources/swagger_ui/LICENSE src/resources/swagger_ui/NOTICE.txt src/resources/swagger_ui/README.md src/resources/swagger_ui/manifest.json src/resources/swagger_ui/swagger-ui-bundle.js src/resources/swagger_ui/swagger-ui.css
git add -- src/web/static/css/blocks/api-docs.css src/web/static/css/blocks/invoices.css src/web/static/css/style.css
git add -- src/web/static/js/api.js src/web/static/js/app.js src/web/static/js/selection.js src/web/static/js/swagger.js
git add -- src/web/static/js/invoice-preview.js src/web/static/js/invoice-state.js src/web/static/js/invoice-table.js src/web/static/js/invoices.js
git add -- src/web/templates/api_docs.html src/web/templates/base.html src/web/templates/invoices.html
git add -- tests/test_architecture.py tests/test_invoices.py tests/test_invoice_contracts.py tests/test_browser_invoices.py tests/frontend/invoices.test.js
git add -- tests/live_invoice_runtime.py tests/live_invoice_environment.py tests/live_invoice_cases.py

git diff --cached --check
git diff --cached --stat
git diff --cached
git commit -m "feat: add independent invoice mapping profile"
git remote get-url --push --all origin
git push origin main
```

Выполнять последующие команды только после успешной предыдущей. На момент проверки
origin имеет два push URL (Amadu-A и neo-term-it), поэтому `git push origin main`
отправляет в оба согласно существующей конфигурации. Если main успел измениться,
сначала сверить новые изменения; force push и reset не нужны.

## Linux: pull, проверка, миграция и пересборка

После ручной публикации, на подготовленном сервере и при чистом checkout:

```bash
cd ~/projects/spec-mapper-ai
git status --short
git pull --ff-only
bash scripts/test.sh
bash scripts/up.sh
bash scripts/test.sh --live
docker compose --env-file .env.example --env-file .env ps
docker compose --env-file .env.example --env-file .env logs --tail=100 api worker migrate
curl --fail http://127.0.0.1:8087/health/live
curl --fail http://127.0.0.1:8087/health/ready
curl --fail http://127.0.0.1:8087/api/openapi.json
```

`up.sh` выполняет миграцию 0006 и rebuild только проекта Mapper. Прежние .env,
каталог, volumes и shared сервисы сохраняются. Не выполнять down -v/prune/downgrade.
Логи остаются видимыми. Для реальной проверки invoice используйте `/invoices`
и примеры [контракта](INVOICE_API.md); общий `--live` сам не загружает приватные счета.

Ручная приёмка:

1. Открыть `http://192.168.55.3:8087/invoices` и `/docs`; отдельная навигация,
   шесть показателей из API, Swagger содержит namespace invoices и старые specification routes.
2. Загрузить два PDF, задать страницы каждому, дождаться результата; проверить файлы,
   поставщиков, исходные деньги/НДС и предупреждения, открыть preview без прямого ключа 8083.
3. Посмотреть Top-5, найти позицию по справочнику, выбрать её; очистить другую строку
   крестиком. После reload выборы сохраняются, исходные строки остаются на месте.
4. У платежа нет товарного выбора. При финансовых предупреждениях подтвердить каждый
   документ, затем одним нажатием утвердить весь результат. Суммы не меняются.
5. До approve итоговые JSON/Excel недоступны; после — immutable JSON и три листа Excel,
   real GUID/коды/нули/повторы сохранены. Финансовые failed checks по-прежнему видны.
6. Проверить импорт готового JSON, узкий экран, отдельную сессию и TTL. Повторить
   прежний процесс спецификаций, каталог/ручной refresh и operator-вход через 8088.

## Границы проверки

Production invoices по LAN и обновление production 0006 остаются ручным выпуском;
это не выдаётся за выполненный deploy. На LAN проверена доступность прежнего API,
новые контракты — на временном loopback 8087 в том же network namespace.
Не выполнялись создание/проведение ERP документа, изменение 1С, реальные webhook-события
1С, изменение shared сервисов или firewall. Определение бизнес-корректности всех 161
соответствий требует эксперта; этот прогон доказывает сохранность данных и рабочий процесс.
