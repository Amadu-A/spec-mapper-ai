<!-- docs/API.md -->
# API v1

Машиночитаемая схема: `/api/openapi.json`. Времена в ответах — Unix seconds UTC.
Все UUID передаются полными строками. Индексы строк начинаются с нуля.

API-клиент передаёт `X-API-Key: <API_KEY>`. Этот ключ принадлежит одной интеграции и
определяет её owner. Web использует cookie `spec_mapper_session`; для writes нужны
точный `Origin` и `X-CSRF-Token` из `GET /api/v1/session`. Ключ upstream — другой,
`EXTRACTING_API_KEY`, и используется только сервером. CORS для чужих origin не включён.

| Метод и путь | Назначение |
|---|---|
| `POST /api/v1/mappings` | `{ "source_job_id": "UUID" }`, обязательный `Idempotency-Key`, 202 |
| `POST /api/v1/mappings/import-json` | Готовый пятиколоночный документ, `Idempotency-Key`, 202 |
| `GET /api/v1/mappings` | Последние 50 неистёкших заданий текущего owner |
| `GET /api/v1/mappings/{id}` | Статус, этап, counts, processed, TTL, revision и links |
| `GET /api/v1/mappings/{id}/result` | Страница готовых rows; до готовности 409 |
| `GET /api/v1/mappings/{id}/candidates?row_index=0` | Top-K конкретной строки |
| `PUT /api/v1/mappings/{id}/rows/{index}/selection` | `{ "catalog_item_guid": "UUID или null", "revision": 1 }` |
| `GET /api/v1/mappings/{id}/source.json` | Оригинальный документ без добавленных полей |
| `GET /api/v1/mappings/{id}/result.json` | Полный результат со всеми строками |
| `GET /api/v1/mappings/{id}/result.xlsx` | Полный Excel, исходные значения — literal text |
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
