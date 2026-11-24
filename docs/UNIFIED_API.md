<!-- docs/UNIFIED_API.md -->
# Единый API спецификации из PDF

Этот документ описывает неизменённый профиль спецификаций. Независимый процесс
для счетов на том же адресе 8087 описан в [INVOICE_API.md](INVOICE_API.md).

Mapper предоставляет законченный процесс для 1С: один адрес, один `X-API-Key`,
один `job_id`. Приём PDF, ожидание Extracting и передача извлечённых строк
в подбор выполняются внутри сервиса. Прежние `/mappings` и `/mappings/import-json`
сохраняют свои контракты для существующих клиентов.

## Контракт

| Операция | Запрос |
|---|---|
| Запустить обработку | `POST /api/v1/specifications` |
| Проверить состояние | `GET /api/v1/specifications/{job_id}` |
| Получить страницу строк | `GET /api/v1/specifications/{job_id}/rows?offset=0&limit=100&status=all` |
| Получить кандидатов | `GET /api/v1/specifications/{job_id}/candidates?row_index=0` |
| Найти позицию каталога | `GET /api/v1/catalog/items?q=DN50&limit=20` |
| Выбрать соответствие или null | `PUT /api/v1/specifications/{job_id}/rows/{row_index}/selection` |
| Утвердить все текущие выборы | `POST /api/v1/specifications/{job_id}/approve` |
| Просмотреть полный черновик | `GET /api/v1/specifications/{job_id}/draft.json` |
| Получить утверждённый JSON | `GET /api/v1/specifications/{job_id}/result.json` |
| Получить утверждённый Excel | `GET /api/v1/specifications/{job_id}/result.xlsx` |

POST принимает `multipart/form-data`: `file` (PDF), `pages` (например `1-5,8`),
`project_code` (подтверждённый шифр до 200 символов). Обязательны `X-API-Key`
и `Idempotency-Key`. Ответ 202 означает, что PDF и job уже сохранены атомарно
в PostgreSQL. LLM не вызывается в HTTP-запросе. Максимальный размер PDF задаёт
`MAX_PDF_BYTES` (по умолчанию 50 MiB); HTTP multipart ограничен этим размером + 64 KiB.
Проверка структуры PDF и номеров страниц выполняется асинхронно в Extracting.

Состояния: `queued`, `extracting`, `matching`, `awaiting_review`, `approved`, `failed`.
В статусе есть `job_id`, `revision`, сроки, счётчики и ссылки только на единый API.
При ошибке есть `error_code`; внутренние ID и адреса Extracting не возвращаются.
`embedding_incompatible` требует проверки модели/индекса оператором; перезапуск
задания не заменяет переиндексацию. При повторе сетевого запроса сохраняйте ключ;
новая операция требует нового ключа. Изменение PDF/параметров под прежним ключом — 409.

Selection: JSON `{"catalog_item_guid":"UUID-каталога-Mapper","revision":1}`
либо `{"catalog_item_guid":null,"revision":1}`. Индексы с нуля; после записи
сохраняйте новую revision. Approve: `{"revision":2}` и `Idempotency-Key`.
При 409 перечитайте результат и повторно проверьте выбор. Утверждение фиксирует
все текущие selection/null атомарно; после него редактирование запрещено.
Итоговые JSON/Excel до утверждения возвращают 409 `not_approved`.
Исходные строковые значения, дубликаты, порядок, Brand/Article/Note сохраняются.
`onec.guid` — внутренний ID каталога; `onec.onec_guid` — реальная ссылка 1С.

Owner/TTL действуют для всех маршрутов: чужой job — 404, собственный истёкший —
410 до очистки, затем 404. Старые jobs `/mappings` не становятся спецификациями.
