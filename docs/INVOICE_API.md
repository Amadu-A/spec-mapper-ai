<!-- docs/INVOICE_API.md -->
# Сопоставление счетов: контракт для 1С и web

Один клиентский адрес `http://192.168.55.3:8087`, один ключ Mapper и один внешний
`job_id`. Префикс `/api/v1/invoices` независим от `/api/v1/specifications` и legacy
`/mappings`. OpenAPI: `/api/openapi.json`, интерактивный локальный Swagger: `/docs`.
Документы в 1С создаёт клиентская интеграция после получения утверждённого результата.

## Доступ и срок хранения

Интеграция передаёт `X-API-Key` из существующего `API_KEYS` Mapper. Для создающих
операций и approve нужен `Idempotency-Key` длиной 1–128 символов. Повтор идентичного
запроса возвращает тот же job; изменённое содержимое с тем же ключом — 409.
Ключи и пространства идемпотентности разных owner/профилей изолированы.

Web работает через существующую HttpOnly cookie; для записи нужны Origin и CSRF.
Чужой job или job другого профиля — 404. Просроченный собственный job — 410,
после физической очистки — 404. Действует прежний TTL: незавершённый job — час
с создания, завершённый — час с технического завершения. Review/approve TTL не продлевают.
Исходники, blobs, выборы и approval удаляются вместе; каталог остаётся постоянным.

## Создание

`POST /api/v1/invoices`, multipart:

- `files`: 1–10 PDF/JPEG/PNG; до 50 MiB каждый и 100 MiB суммарно.
- `page_selection`: JSON-строка вида `{"0":"1-3","1":"2,4"}`. Индексы файлов с 0,
  физические страницы с 1; пропущенный файл обрабатывается целиком. До 100 выбранных
  страниц на пакет. Проверяется реальная структура PDF/изображения, не только расширение.

202 означает, что **весь** пакет, метаданные и outbox уже атомарно сохранены в PostgreSQL.
Ответ содержит `job_id`, `status`, `revision`, `expires_at`, counts и ссылки только на 8087.
Worker серверным `EXTRACTING_API_KEY` отправляет пакет в
`POST 8083 /api/v1/invoice-extractions`, сохраняет upstream ID и получает только конечный
`result.json`. Повтор отправки использует стабильный ключ. Partial/snapshot не является
результатом. Upstream ID, ключ и внутренние URL в клиентский status не выдаются.

Пример Bash; ключ задаётся локально через переменную, а не в URL:

```bash
curl --fail-with-body -H "X-API-Key: $MAPPER_API_KEY" \
  -H 'Idempotency-Key: invoice-upload-2026-001' \
  -F 'files=@invoice.pdf;type=application/pdf' \
  -F 'page_selection={"0":"1-3"}' \
  http://192.168.55.3:8087/api/v1/invoices
```

Альтернатива: `POST /api/v1/invoices/import-json`, `Content-Type: application/json`,
полный `invoice_extraction.v1` из Extracting. Это тот же invoice pipeline без VLM.
Обязательны `schema_version`, `files[]`, `summary`; вложенные файлы, документы, позиции,
реквизиты, финансы, проверки, SHA и страницы сохраняются дословно, включая отсутствующие
поля, null, дубли и порядок. Денежные значения — decimal-строки, не JSON float.
Совместимые дополнительные поля сохраняются; новая версия schema требует явной поддержки.
Историческое `notes` строкой принимается наряду со списком. Зарезервированные
`onec`, `matching` и служебные поля результата во входном JSON запрещены.

## Примеры ответов

Сокращённый синтетический status (это фрагмент, полный DTO описан в OpenAPI):

```json
{
  "job_id": "00000000-0000-4000-8000-000000000002",
  "status": "awaiting_review",
  "revision": 1,
  "file_count": 1,
  "document_count": 1,
  "total": 2,
  "processed": 2,
  "counts": {"review": 1, "not_applicable": 1},
  "financial_warning_count": 1,
  "preview_available": true,
  "links": {
    "rows": "/api/v1/invoices/00000000-0000-4000-8000-000000000002/rows"
  }
}
```

Фрагмент позиции утверждённого результата без соответствия:

```json
{
  "row_number": 1,
  "caption": "Тестовая позиция",
  "article": "000123",
  "quantity": "2.000",
  "line_total_with_vat": "1200.00",
  "onec": null,
  "matching": {
    "row_index": 0,
    "status": "not_found",
    "confirmed": true,
    "manually_reviewed": true,
    "approved": true,
    "catalog_version": "version-uuid",
    "score": null,
    "warnings": []
  }
}
```

## Операции с заданием

В таблице `J` — внешний Mapper UUID, `R` — глобальный индекс строки с 0.

| Метод | Путь после `/api/v1/invoices` | Назначение |
|---|---|---|
| GET | `/{J}` | Статус, revision, TTL, counts и links |
| GET | `/{J}/rows?q=&status=all&offset=0&limit=100` | Исходные строки, реквизиты, финансы, selection и Top-5 |
| GET | `/{J}/candidates?row_index=0` | Сохранённые кандидаты без повторного поиска |
| PUT | `/{J}/rows/{R}/selection` | `{"catalog_item_guid":"internal-uuid-or-null","revision":N}` |
| DELETE | `/{J}/rows/{R}/selection?revision=N` | Явный null, исходная строка и кандидаты остаются |
| POST | `/{J}/approve` | Revision, финансовое согласие и массовое утверждение |
| GET | `/{J}/source.json` | Точный исходный `invoice_extraction.v1` |
| GET | `/{J}/draft.json` | Предварительный `invoice_mapping.v1` |
| GET | `/{J}/result.json` | Только утверждённый immutable snapshot |
| GET | `/{J}/result.xlsx` | Тот же snapshot, три финансовых листа |
| GET | `/{J}/files/{file_index}/pages/{page}/preview?width=1200` | Защищённый PNG proxy 8083 |

В PUT нужно передавать JSON `null`, а не строку `"null"`. Ручной поиск по всему
справочнику — существующий `GET /api/v1/catalog/items?q=...&limit=20`.
Из поиска/кандидата в команду выбора передаётся внутренний `guid` Mapper.
В документ 1С передаётся **только отдельный настоящий `onec.onec_guid`**.
Отсутствующий настоящий GUID отмечается `onec_guid_missing` и не подменяется UUID Mapper.

Статусы задания: `queued → extracting → matching → awaiting_review → approved` либо
`failed` с безопасным `error_code`. Для прямого JSON шаг extracting отсутствует.
`completed_with_warnings` upstream сохраняется как `upstream_warning=true`.
Статусы строк: `matched`, `review`, `manual`, `not_found`, `not_applicable`.
Товар и услуга проходят общий matcher; услуга требует проверки. Платёжные строки
`final_payment`/`advance_payment` не ищутся в Qdrant и остаются `not_applicable` с null.
Неизвестный вид требует отдельного решения; API не позволяет назначать ему номенклатуру.

Глобальная нумерация следует `files → documents → items`. Каждая строка имеет
`reference={file_index, document_index, row_number, source_pages, source_sha256}`.
Одинаковые row_number и товары из разных документов не объединяются.

Preview доступен для загруженных файлов до upstream TTL; для импорта JSON
`preview_available=false`, запрос preview — 409 `preview_unavailable`.
Исходные файлы не рендерятся повторно Mapper, ключ 8083 не передаётся в браузер.

## Финансовая проверка и результат

`rows.financial_warnings[]` содержит `document_id` вида `"0:0"` и коды рисков.
Если предупреждения есть, нужно явно передать согласие по каждому документу:

```json
{
  "revision": 3,
  "acknowledged_document_warnings": ["0:0", "1:0"]
}
```

При отсутствии согласия approve возвращает 409 `financial_acknowledgment_required`.
Неверные/повторные document_id отклоняются. Revision защищает от конкурентных правок.
Утверждение одним действием подтверждает все текущие selection/null; открывать каждую
строку отдельно не требуется. Согласие, owner, время и версия каталога фиксируются
вместе с immutable snapshot. После approve менять выбор нельзя.

Результат `invoice_mapping.v1` сохраняет все исходные уровни. К каждой позиции
добавлены ровно один `onec` (snapshot каталога либо null) и `matching` с row_index,
status, confirmed, score, warnings и версией каталога. К документу добавлены
`document_index`, `matching_summary`, `ready_for_onec`. Последний флаг означает наличие
подтверждений и настоящих GUID у всех строк; это не подтверждение арифметики, НДС
или проведения документа в 1С. Null/платёж требуют отдельной обработки в форме 1С.
Исходный `validation.failed` и суммы после согласия не исправляются.

Excel: листы `Документы`, `Позиции`, `Предупреждения`. Финансовые строки, коды,
артикулы и пользовательский текст записываются текстом, включая ведущие нули,
числа длиннее 15 цифр и строки, начинающиеся `=`. Последние семь столбцов позиций:
`Код 1С`, `Наименование 1С`, `Реальный GUID 1С`, `Ед. 1С`, `Статус сопоставления`,
`Индекс Mapper`, `Подтверждено`. Прежний Excel спецификаций не изменён.

## Ошибки и конфигурация

Ошибки имеют общий вид `{"error":{"code":"...","message":"..."}}` без входных
реквизитов и секретов. 401 — ключ/сессия, 403 — CSRF, 404 — нет доступа/профиля,
409 — не готов/не утверждён/CAS/идемпотентность/financial guard, 410 — TTL,
413 — размер/страницы, 415 — MIME, 422 — файл/схема/страницы, 429 — rate limit.
Для 429 соблюдайте `Retry-After`. Ошибки upstream 404/410/502/504, cancelled/failed
и timeout дают явный failed job либо ошибку preview, без ложного success.

Используются прежние `EXTRACTING_BASE_URL`, `EXTRACTING_API_KEY`, `API_KEYS` и TTL.
Новые обычные настройки: `INVOICE_MAX_FILES=10`, `INVOICE_MAX_FILE_BYTES=52428800`,
`INVOICE_MAX_BATCH_BYTES=104857600`, `INVOICE_MAX_SELECTED_PAGES=100`,
`INVOICE_MAX_ITEMS=20000`, `INVOICE_UPSTREAM_TIMEOUT_SECONDS=3300`.
Все читаются Pydantic Settings и передаются Compose. `.env`/секреты не генерируются заново.
