<!-- docs/INTEGRATION_1C.md -->
# Интеграция 1С и Telegram

1С-кнопку, модальное окно и создание документа реализует программист 1С.
Mapper предоставляет вычисления и REST-контракты. Web остаётся самостоятельным
клиентом с отдельной cookie-сессией.

## Адреса и ключи

Mapper: `http://192.168.55.3:8087`; `/docs`, `/api/openapi.json`.
Извлечение: `http://192.168.55.3:8083`; его `/openapi.json` — отдельная схема.
LAN не означает публикацию в интернет: для интернета нужны HTTPS reverse proxy
и сетевая политика. PostgreSQL/Qdrant наружу не публикуются.

Bootstrap создаёт в `.env` `API_KEYS={"onec":"…","telegram":"…"}`.
Передавайте свой ключ серверным HTTP-клиентом в `X-API-Key`. Имя определяет owner
`api:onec` или `api:telegram`; чужой job недоступен (404). Ротация значения при
сохранении имени сохраняет доступ. Прежний `API_KEY` поддерживает свой прежний owner;
его ротация меняет этот доступ. Ключи mapper и extracting различаются.
`CATALOG_ADMIN_KEY` не выдавать клиентам. Пользователи/JWT не создаются.

## Последовательность

1. `POST extracting/api/v1/documents`: multipart `file`; ответ содержит `id`, `page_count`.
   Пользователь выбирает физические PDF-страницы с 1.
2. `POST extracting/api/v1/extractions`: multipart `document_id`, `pages` (например
   `1-3,8`), `project_code`; собственные `X-API-Key`, `Idempotency-Key`.
3. Опросить `GET extracting/api/v1/extractions/{job_id}` тем же owner до
   `completed`/`completed_with_warnings`; получить `.../{job_id}/result.json` до TTL.
   Не передавать partial snapshot как итог.
4. Передать документ в mapper, опросить статус до `complete`/`ready_for_review`.
5. Показать Top-5, явно сохранить выбор или `null` для спорных строк. Доступен поиск
   любой разрешённой номенклатуры вне Top-5.
6. Утвердить общий результат, получить `approved.json` до `expires_at`, сохранить
   его в 1С и создать документ средствами 1С.

Ключ извлечения должен владеть upstream заданием. Cookie-задание другого пользователя
не становится доступным по серверному ключу. Web mapper также не получает доступ
к API-заданиям автоматически: owner изолированы.

## Прямой JSON — основной контракт

```http
POST /api/v1/mappings/import-json
X-API-Key: <ключ mapper onec>
Idempotency-Key: <стабильный UUID операции>
X-Source-Job-ID: <UUID извлечения, необязательно>
Content-Type: application/json

{"items":[{"Caption":"Клапан DN50 PN16","Units":"шт","Quantity":" 2,50 ","UserComment1":"ПР-01","Pages":"4–5"}]}
```

Только `items` и пять указанных **строковых** полей. Не преобразовывать Quantity
в число. Пробелы, порядок и дубли сохраняются дословно. Source UUID — заголовок,
не шестое поле исходного документа. Неверная схема — 422.

Ответ 202 содержит `job_id`, `status`, `revision`, `expires_at`, `links`.
Повтор того же owner/Idempotency-Key/документа/source UUID возвращает прежний job;
другой payload под тем же ключом — 409 `idempotency_conflict`. После потери ответа
повторять тот же ключ; независимая операция использует новый.

Альтернатива — `POST /api/v1/mappings` с `{"source_job_id":"UUID"}` и теми же
заголовками. Mapper сам опрашивает upstream посредством `EXTRACTING_API_KEY` из
своего `.env`; отсутствие ключа даёт `upstream_key_missing`.

## Ревью и поиск

```http
GET /api/v1/mappings/{job_id}
GET /api/v1/mappings/{job_id}/result?offset=0&limit=25&status=all
GET /api/v1/mappings/{job_id}/candidates?row_index=0
GET /api/v1/catalog/items?q=00-00001&limit=20
PUT /api/v1/mappings/{job_id}/rows/0/selection
Content-Type: application/json

{"catalog_item_guid":"<внутренний guid кандидата или поиска>","revision":1}
```

Для отказа: `{"catalog_item_guid":null,"revision":2}`. Все вызовы требуют
`X-API-Key`. Сохранение увеличивает revision; следующая запись использует последнее
значение из ответа. При 409 `revision_conflict` перечитать результат и повторить
решение пользователя, не затирать другую вкладку. Выбирать можно только текущую
активную позицию каталога.

`score` — cosine, `rank` — правила, это не вероятность. Top-5 — максимум; при малом
справочнике кандидатов меньше. Несовпадающие DN/PN, диапазоны, температура, единицы
и неподтверждённые модель/артикул требуют проверки даже при высоком cosine.

## Общее утверждение

```http
POST /api/v1/mappings/{job_id}/approve
X-API-Key: <ключ mapper>
Idempotency-Key: <стабильный UUID утверждения>
Content-Type: application/json

{"revision":3}
```

200: `status=approved`, `approved_at`, `approved_revision`, прежний `expires_at`.
Повтор той же утверждённой revision идемпотентен, включая потерю HTTP-ответа.
Старая revision — 409. При `review` или неподтверждённом `not_found` — 409
`unresolved_rows`. Автоматический `matched` утверждается общей операцией;
явный ручной `null` допустим. Ручное подтверждение строки и утверждение задания
учитываются отдельно.

После утверждения изменения запрещены (`result_approved`, 409).
`GET /api/v1/mappings/{job_id}/approved.json` до утверждения — 409 `not_approved`.
`source.json` — оригинал, `result.json`/`result.xlsx` — предварительные экспорты.
Для создания документа получать `approved.json`.

Сокращённый пример финального результата:

```json
{
  "job_id": "UUID",
  "source_job_id": "UUID или null",
  "source_hash": "sha256",
  "catalog_version": "UUID",
  "algorithm_version": "technical-baseline-v2",
  "status": "approved",
  "approved_at": 1793940000.0,
  "approved_revision": 3,
  "approval": {
    "approved_at": 1793940000.0,
    "approved_revision": 3,
    "owner": "api:onec",
    "catalog_version": "UUID"
  },
  "items": [{
    "Caption": "Клапан DN50 PN16",
    "Units": "шт",
    "Quantity": " 2,50 ",
    "UserComment1": "ПР-01",
    "Pages": "4–5",
    "index": 0,
    "onec": {
      "guid": "внутренний UUID mapper",
      "onec_guid": null,
      "code": "00000123",
      "name": "Клапан DN50 PN16",
      "unit": "шт"
    },
    "matching": {
      "status": "manual",
      "confirmed": true,
      "manually_reviewed": true,
      "approved": true,
      "score": null
    }
  }]
}
```

`onec` дополнительно содержит атрибуты каталога и версию ручного выбора. При отказе
**весь `onec` равен null**. Код — строка с ведущими нулями. `guid` — внутренний UUID,
его нельзя выдавать за ссылку 1С. Настоящий `onec_guid` берётся только из XML,
в текущем файле отсутствует. Программист 1С разрешает ссылку по однозначному `code`.
Коды `00000079127`, `00-00003589`, `ЦБ-00000920` целиком исключены из подбора/поиска.

## Сроки и ошибки

Mapping живёт час после технического завершения; незавершённый черновик — час
с создания. Review/approve TTL не продлевают. После срока — 410, после очистки — 404.
Финал необходимо забрать и хранить в 1С/боте. Каталог постоянный; первоначальный
импорт имеет отдельный 24-часовой предел и checkpoints.

Ошибки: `{"error":{"code":"revision_conflict","message":"…"}}`.
401 — ключ; 404 — чужой/отсутствующий job; 410 — истёкший; 409 — конфликт/ревью;
422 — схема; 429 — лимит. `/health/ready` с 503 — зависимости/каталог ещё не готовы.
У failed задания проверять `error_code`, partial не утверждать. Сетевой retry записи
использует прежний Idempotency-Key; polling выполняется с ограниченным интервалом.

## Главная, оператор и будущий HTTP справочник

Клиентский контракт 1С/бота import-json → status → выбор/null → approve → approved.json
не изменён. Ключ extracting должен владеть исходным upstream job. Web cookie другого
владельца не получает доступ к API-заданию даже в операторском режиме.

HTTP получение справочника из 1С — отдельное направление: сейчас контракт неизвестен.
Поддержана архитектура GET/full/XML, server-only none/basic/bearer credentials,
allowlist/TLS, ограниченное потоковое скачивание и проверка completion marker.
Согласовать URL/метод, XML пример/XSD, GUID/deleted, признак полного среза/версию,
лимиты и CA/сеть до включения. Не считать внутренние code UUID реальными GUID 1С.
[Поля, ограничения, план перехода GUID и права](DASHBOARD_AND_SYNC.md).
