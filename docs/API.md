<!-- docs/API.md -->
# API v1

Машиночитаемая схема: `/api/openapi.json`. Времена в ответах — Unix seconds UTC.
Все UUID передаются полными строками. Индексы строк начинаются с нуля.

API-клиент передаёт `X-API-Key` из `API_KEYS` (`onec`, `telegram`): имя определяет
стабильный owner, значение можно ротировать. Legacy `API_KEY` сохраняет прежний owner.
Web использует cookie `spec_mapper_session`; для writes нужны
точный `Origin` и `X-CSRF-Token` из `GET /api/v1/session`. Ключ upstream — другой,
`EXTRACTING_API_KEY`, и используется только сервером. CORS для чужих origin не включён.

| Метод и путь | Назначение |
|---|---|
| `POST /api/v1/mappings` | `{ "source_job_id": "UUID" }`, обязательный `Idempotency-Key`, 202 |
| `POST /api/v1/mappings/import-json` | Основной вход: пятиколоночный JSON, `Idempotency-Key`, необязательный UUID `X-Source-Job-ID`, 202 |
| `GET /api/v1/mappings` | Последние 50 неистёкших заданий текущего owner |
| `GET /api/v1/mappings/{id}` | Статус, этап, counts, processed, TTL, revision и links |
| `GET /api/v1/mappings/{id}/result` | Страница готовых rows; до готовности 409 |
| `GET /api/v1/mappings/{id}/candidates?row_index=0` | Top-K конкретной строки |
| `PUT /api/v1/mappings/{id}/rows/{index}/selection` | `{ "catalog_item_guid": "UUID или null", "revision": 1 }` |
| `DELETE /api/v1/mappings/{id}/rows/{index}/selection?revision=1` | Удалить только соответствие, сохранить исходную строку и подтверждённый `null` |
| `GET /api/v1/mappings/{id}/source.json` | Оригинальный документ без добавленных полей |
| `GET /api/v1/mappings/{id}/result.json` | Полный результат со всеми строками |
| `GET /api/v1/mappings/{id}/result.xlsx` | Полный Excel, исходные значения — literal text |
| `POST /api/v1/mappings/{id}/approve` | `{ "revision": 3 }`, `Idempotency-Key`; immutable snapshot, 200 |
| `GET /api/v1/mappings/{id}/approved.json` | Только утверждённый снимок для 1С; до approval 409 |
| `GET /api/v1/catalog/status` | count, version, fingerprint, warnings, last_successful_sync |
| `GET /api/v1/catalog/items?q=...&limit=20` | Поиск по имени/коду/артикулу, limit 1–50 |
| `POST /api/v1/catalog/imports` | Импорт настроенного data/, отдельный `X-Catalog-Key`, `Idempotency-Key` |
| `GET /api/v1/catalog/imports/{id}` | Статус импорта с тем же `X-Catalog-Key` |

Параметры `/result`: `q`, `status=all|matched|review|not_found|manual`, `offset>=0`,
`limit=1..100`. `total/counts` относятся ко всему заданию, `filtered_total` — к фильтру.
Каждая запись страницы: `index`, неизменный `source`, `selection`, `candidates`,
`status`, `confirmed`, `manually_reviewed`. Cosine и rank правил раздельны;
`conflicts/evidence` объясняют ограничения. Автоподбор всегда `confirmed=false`.

`selection.guid` — внутренний устойчивый UUID; `selection.onec_guid` — настоящая
ссылка 1С либо null. В текущем XML ссылок 1С нет. Ручной выбор принимает внутренний
`guid` любой позиции **активного** каталога. `null` подтверждает отсутствие подходящей.
Ручной выбор не продлевает срок хранения задания. Устаревшая revision даёт 409.
`DELETE selection` использует ту же проверку owner, CSRF и revision, что и `PUT`.
После очистки `selection=null`, `status=not_found`, `confirmed=true`,
`manually_reviewed=true`; исходная строка и список Top-K сохраняются. Повтор запроса
со старой revision возвращает 409, после утверждения очистка запрещена.

JSON-экспорт содержит исходную пятёрку, отдельный объект `onec` и `matching`;
оригинал доступен через `source.json`. При выборе через общий поиск cosine может
быть null — новый выбор не получает оценку другого кандидата.

Ошибки имеют форму `{ "error": { "code": "...", "message": "..." } }`.
Основные коды: unauthorized/csrf/forbidden, not_found/expired, validation/invalid_source,
idempotency_conflict/revision_conflict, upstream_not_found/upstream_expired/
upstream_forbidden/upstream_failed/upstream_cancelled/upstream_timeout,
embedding_unavailable/embedding_incompatible, catalog_not_ready, qdrant_unavailable.
После физической очистки истёкший UUID возвращает 404. `ready_for_review` — технически
завершённое задание, доступное для экспорта; незавершённые partial rows не выдаются.

Статус содержит `unresolved_count`, `approved_at`, `approved_revision`, `links.status`
и `links.approved`. Для `complete`/`ready_for_review` общее утверждение с актуальной
revision является явным подтверждением **всех текущих выборов и пустых строк**.
Предварительно заходить в каждую спорную строку не требуется. Операция не выбирает
нового кандидата вместо `null`: она фиксирует ровно текущее предложение пользователя.
Выбранная `review` становится `manual`, пустая `review` — `not_found`; ранее
автоматическая `matched` сохраняет этот статус. Все строки получают `confirmed=true`
и `manually_reviewed=true`, `unresolved_count` становится нулём. Метрики различают
сохранённые автоматические и подтверждённые человеком спорные соответствия.
Повтор утверждённой revision идемпотентен. Старая revision и незавершённый подбор
по-прежнему дают 409; после approval изменения дают `result_approved` (409).
Approval не продлевает TTL. Предварительные JSON/Excel сохраняются, финал — `approved.json`.
Его `approval` содержит owner, время, revision и версию каталога; строки содержат
исходную пятёрку, стабильный `index`, `onec` (либо null), признаки `matching`.
Полный пример и последовательность интеграции: [INTEGRATION_1C.md](INTEGRATION_1C.md).

## Дополнение V2: dashboard и управление синхронизацией

Прежние mapping/approval/import-json контракты сохранены. Новые GET/PUT sync-settings,
connection-test, import-history/report, dashboard summary/jobs и operator session/audit/jobs
описаны в [контрактах управления](DASHBOARD_AND_SYNC.md#api-управления-и-отчёты)
и актуальном OpenAPI. Cookie управления — OperatorSession, CSRF — X-Operator-CSRF;
серверный CatalogKey совместим. Обычный IntegrationKey не повышает права.
`POST catalog/imports` без тела по-прежнему импортирует локальный XML;
необязательное тело `{"source_type":"local_xml"}` либо http_onec выбирает GUI source.
Новые management настройки revision-CAS; 409 требует перечитать конфигурацию.

## Упрощённый вход и ручное обновление (V3)

| Метод и путь `/api/v1` | Контракт |
|---|---|
| GET `/operator/session` | Дополнительно `can_issue_ticket`, `operator_port`, `ssh_host`; административные ключи не выдаются |
| POST `/operator/tickets` | 201 `{ticket, expires_in}`; только web Owner + CSRF/Origin через доверенный SSH gateway |
| GET `/catalog/refresh-status` | `{available, reason, retry_after, interval_seconds:60, source_type, active}` без URL/секретов |
| POST `/catalog/refresh` | 202 CatalogImportStatus; пустое тело `{}`, обязательный Idempotency-Key; источник только из БД |
| GET `/catalog/refreshes/{job_id}` | CatalogImportStatus только своего запуска, с прежними owner/TTL проверками |

Обычный пользователь может запустить только настроенный подтверждённый HTTP-источник
1С. Оператор через ту же кнопку может обновить и локальный XML. Настройки подключения,
включение/выключение расписания и время UTC по-прежнему меняет только оператор.
Общий интервал между принятыми запросами кнопки — 60 секунд в PostgreSQL, для всех
owners/вкладок/реплик. Ответ 429 содержит `Retry-After` и `error.retry_after`.
409 `catalog_busy` означает активный импорт, `revision_conflict` — смену настроек
перед enqueue. Повтор принятого запроса с тем же owner/key возвращает прежний job,
не расходует интервал и работает после последующего изменения настроек.

Legacy `/catalog/imports` остаётся отдельным административным/CLI контрактом без
нового cooldown для bootstrap и восстановления. В UI обоих режимов используется
только `/catalog/refresh`. Ручное обновление не сдвигает ежедневный слот.

Код кнопкой выдаётся через `127.0.0.1:8088` сервера (OPERATOR_PORT), куда направлен
SSH-туннель. Gateway использует существующий SESSION_SECRET и подписывает короткое
межсерверное доказательство запроса; оно не отправляется браузеру. Подмена Host,
Origin или Forwarded на публичном 8087 не даёт права получить код. Получение кода
эквивалентно CLI operator-ticket и не запускает shell/Docker из API.
Новый маршрут не изменяет правила обычных owner cookie или API_KEYS.
