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
5. Показать текущие предложения и Top-5. При необходимости изменить выбор, найти
   любую разрешённую номенклатуру вне Top-5 или очистить ошибочное соответствие.
6. Утвердить общий результат одной операцией: она подтверждает все текущие выборы
   и `null`, без обязательного обхода каждой строки. Получить `approved.json` до
   `expires_at`, сохранить его в 1С и создать документ средствами 1С.

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

Это legacy v1. Рекомендуемый v2: `file_name` и `items`, каждая строка содержит
пять прежних полей плюс обязательные строковые `Brand`, `Article`, `Note`
(пустые строки разрешены). Не преобразовывать Quantity/Article в числа.
Пробелы, порядок и дубли сохраняются дословно. Source UUID остаётся заголовком.
Неверная или смешанная схема — 422.

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

Очистить соответствие можно также без тела запроса:

```http
DELETE /api/v1/mappings/{job_id}/rows/0/selection?revision=2
X-API-Key: <ключ mapper>
```

В исходной спецификации строка остаётся, Top-5 сохраняется; `selection=null`,
`status=not_found`, `confirmed=true`, `manually_reviewed=true`. Web показывает
«Соответствие не выбрано / Не найдено / Подтверждено человеком». Revision
увеличивается как при `PUT`; при устаревшей revision нужно перечитать результат.

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
Старая revision — 409. Готовый `ready_for_review` допускает общее утверждение без
предварительного подтверждения каждой строки. Вызов означает, что пользователь
одобрил **все текущие соответствия и пустые строки**. Новые кандидаты не выбираются,
`null` не заменяется Top-1. Выбранные `review` становятся `manual`, пустые `review` —
`not_found`; исходные `matched` сохраняются. Все строки получают `confirmed=true`
и `manually_reviewed=true`, а `unresolved_count` становится нулём. Запись rows,
метрик и immutable snapshot выполняется атомарно; TTL и approved revision не сдвигаются.

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
401 — ключ; 404 — чужой/отсутствующий job; 410 — истёкший; 409 — конфликт/неготовый результат;
422 — схема; 429 — лимит. `/health/ready` с 503 — зависимости/каталог ещё не готовы.
У failed задания проверять `error_code`, partial не утверждать. Сетевой retry записи
использует прежний Idempotency-Key; polling выполняется с ограниченным интервалом.

## Главная, оператор и HTTP справочник

Клиентский контракт 1С/бота import-json → status → выбор/null → approve → approved.json
не изменён. Ключ extracting должен владеть исходным upstream job. Web cookie другого
владельца не получает доступ к API-заданию даже в операторском режиме.

HTTP получение справочника — отдельное направление, реализованы полный JSON/XML,
server-only credentials, allowlist/TLS, bounded streaming и durable webhook.
Формат JSON предоставлен владельцем; completion marker, сервисная учётная запись
и безопасный транспорт ещё требуют подтверждения. До этого live — `blocked_by_external_1c`.
Не считать внутренние code UUID реальными GUID 1С.
[Поля, ограничения, план перехода GUID и права](DASHBOARD_AND_SYNC.md).

## Программисту 1С: полная сверка и изменения (V4)

Исходная база: `http://192.168.10.30/erp_update/hs/catalog`.
Уже существующие endpoint сохраняют смысл:

- GET `/export` → полный `{ "items": [...] }`, включая `deleted=true`, без групп.
- GET `/items?q=...&limit=20` — диагностический поиск; основной поиск Mapper локальный.
- POST `/items/batch`, body `{ "guids": ["uuid"] }`, ответ `{ "items": [...] }`.
  Не более 50 GUID; обязательно возвращать и помеченные на удаление карточки.

Каждая карточка: настоящий `guid` UUID; строки `code`, `name`, `full_name`, `unit`,
`article`, `brand`, `manufacturer`; `deleted` строго boolean. Пустые дополнительные
реквизиты — строки `""`. Нули в code/article сохранять. Предпочтительное отображение —
full_name; short name сохраняется отдельно. Повтор GUID — ошибка всей выгрузки;
конфликт code — все такие карточки исключаются из подбора с предупреждением.

Нужно согласовать/добавить:

1. Для полного консистентного успешного GET `/export` заголовок
   `X-Catalog-Complete: true`, `Content-Type: application/json; charset=utf-8`.
   Другой completion marker согласовать до включения расписания.
2. Отдельная read-only сервисная учётная запись с непустым паролем, HTTPS
   и доступ из Docker сети Mapper. Временная учётная запись сотрудника с пустым
   паролем по HTTP для production не подходит. TLS verification не отключать.
3. После commit карточки отправлять webhook **асинхронно** через регистр/очередь
   обмена 1С при создании, изменении, пометке удаления и физическом удалении:
   POST `http://192.168.55.3:8087/api/v1/catalog/events` (LAN; предпочтительно HTTPS/VPN),
   `Content-Type: application/json`, `X-Webhook-Key: <отдельный ключ от владельца Mapper>`.

```json
{"event_id":"5a12d947-30d3-4fd9-949a-9da4d765913a","occurred_at":"2026-11-11T13:00:00Z","changes":[{"guid":"3c660a4a-ed29-11e6-8281-305a3a813380","action":"upsert"},{"guid":"8f157ccb-ed29-11e6-8281-305a3a813380","action":"delete"}]}
```

Стабильный уникальный event_id; UTC время; 1–100 разных GUID. `upsert` — создание,
изменение, пометка удаления (`deleted` Mapper перечитает через batch).
`delete` — физическое удаление, GUID сохранить до удаления в исходящем регистре.
Карточки/URL/credentials в webhook не нужны. Не отправлять внутри транзакции записи.
Повторять тот же payload/event_id с backoff при сети/5xx/429 до 202 либо 200;
учитывать Retry-After. 409 означает ошибочное повторное использование ID с другим
payload, 401/403 — настройки доступа, 413/422 — исправить схему/размер.

Отсутствие upsert GUID в batch **не доказывает удаление**: Mapper повторяет запрос
и сохраняет прежний каталог. Полная ежедневная сверка остаётся контрольной; включить
в GUI после проверки контракта (OFF/15:00 UTC по умолчанию). Семантический поиск,
Qdrant и реранкер в 1С реализовывать не требуется. Ключ webhook не показывается в GUI.
