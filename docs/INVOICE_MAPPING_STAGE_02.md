<!-- docs/INVOICE_MAPPING_STAGE_02.md -->
# Spec Mapper AI — ТЗ этапа 02: единый API сопоставления счетов с номенклатурой 1С

**Дата:** 23.11.2026
**Репозиторий:** `Amadu-A/spec-mapper-ai` (зеркало `neo-term-it/spec-mapper-ai`)
**Базовый `main` на момент подготовки:** `b607270839ee03d75b17c7241f710dc88303f48e` — merge PR #1 `feature/unified-specification-api`.
**Upstream:** `Amadu-A/spec-extracting-ai`, `main` `9f4c911642a868cf4227ddedaec4ff6b919b6e5d` (API 8083, профиль `invoice_extraction.v1`).
**Общие правила:** `Amadu-A/shared_infrasktructure/docs`, начиная с `LLM_CONTEXT.md`; действующие `AGENTS.md`, `docs/UNIFIED_API.md`, `docs/API.md`, `docs/INTEGRATION_1C.md`, `docs/architecture.md`.
**Статус:** постановка на реализацию; не утверждение о существовании новых invoice endpoints.

## 1. Цель и границы этапа

Добавить в **существующий** `spec-mapper-ai` на **8087** второй независимый бизнес-профиль **«Сопоставление счетов»**, использующий уже работающий механизм `Caption → shared-embedding → Qdrant Top-K → deterministic rerank → selection → human review → approve → immutable JSON/Excel`. Через **единственный клиентский адрес 8087** интеграция 1С получает один внешний `job_id` и один `X-API-Key`. Mapper серверным ключом сам вызывает `spec-extracting-ai` на 8083 и обрабатывает его конечный JSON.

Рабочий сценарий:

`1С → POST 8087 /api/v1/invoices (PDF/JPEG/PNG, несколько файлов) → durable job → 8083 /api/v1/invoice-extractions → invoice_extraction.v1 → адаптер исходных строк → существующие embeddings/Qdrant/rerank → Top-5 и выбор → подтверждение в 1С → POST 8087 /approve → неизменяемый result.json → заполнение Заказа поставщику средствами 1С`.

**Нельзя:** создавать третий сервис, второй каталог, вторую векторную коллекцию или отдельную модель; реализовывать собственный алгоритм ранжирования для счетов; переписывать спецификационный unified API; напрямую создавать/проводить заказы или элементы НСИ в Mapper; автоматически менять цены, количество, НДС или финансы исходного счета.

Изменения затрагивают только `spec-mapper-ai`. Сервис 8083 уже имеет нужный API; если будет выявлено несовпадение фактического контракта, регистрировать его как блокер, а не менять Extracting в рамках данного этапа. Интеграция форм 1С — следующий отдельный этап: здесь предоставляем готовые REST-контракты для программиста 1С.

## 2. База, которую требуется переиспользовать

Последний merged PR #1 добавил:

- `src/application/specifications.py`, `src/presentation/specifications.py`, `docs/UNIFIED_API.md`;
- `POST /api/v1/specifications` и связанные `GET /rows`, `/candidates`, `PUT /selection`, `POST /approve`, `GET /draft.json`, `/result.json`, `/result.xlsx`;
- один внешний ID, серверную оркестрацию 8083 → 8087, durable приём PDF с таблицей `specification_uploads`, скрытые upstream ID, owner/TTL/idempotency;
- сохранение старых `/api/v1/mappings`, `/mappings/import-json` и действующих алгоритмов.

Переиспользовать существующие `src/application/mappings.py`, `src/application/processing.py`, `src/domain/matching.py` (`specification_text`, `rerank`, `choose`), storage/lease/outbox репозитория, `shared-embedding`, Qdrant, профильный каталог 1С, revision-CAS, immutable approval, auth, конфигурацию и журналирование. Изменять общие функции **только** если этого действительно требует общий контракт, и в таком случае сохранить прежнюю семантику и тесты спецификаций.

Критически важно: `src/application/processing.py` сейчас ожидает `data["source"]["items"]`, тогда как invoice input имеет `files[].documents[].items[]`. **Не подсовывать invoice JSON в валидатор legacy спецификации и не переименовывать его поля в хранимом source.** Сделать отдельный адаптер и оставить оригинал неизменным.

## 3. Новый вход и upstream-контракт

### 3.1 Основной единый вход на 8087

`POST /api/v1/invoices` (multipart/form-data):

- `files`: 1–10 файлов, повторяющееся поле; PDF, JPEG, PNG;
- `page_selection`: необязательная JSON-строка, например `{"0":"1-3","1":"2"}` — индексы файлов с нуля; по умолчанию все страницы каждого файла;
- обязательные заголовки `X-API-Key`, `Idempotency-Key`;
- 202 возвращается **после атомарного сохранения всего задания и всех входных файлов**, а не после получения результата VLM;
- лимиты согласованы с 8083: 50 MiB/файл, 100 MiB/пакет, не больше 100 выбранных физических страниц суммарно (фактические Settings 8083 являются источником истины). HTTP body/stream проверяется до аллокации неограниченной памяти.

Пример:

```bash
curl -X POST http://192.168.55.3:8087/api/v1/invoices \
  -H "X-API-Key: $ONEC_KEY" \
  -H "Idempotency-Key: order-draft-2026-001" \
  -F 'files=@invoice.pdf;type=application/pdf' \
  -F 'files=@scan.png;type=image/png' \
  -F 'page_selection={"0":"1-3","1":"1"}'
```

**Mapper → Extracting (сервер-сервер):** `POST /api/v1/invoice-extractions` с `files` и `page_selection`; затем `GET /api/v1/invoice-extractions/{id}` до `completed`/`completed_with_warnings`; затем однократно `GET .../{id}/result.json`. Для этих запросов используется только серверный `EXTRACTING_API_KEY` через Settings, не ключ 1С и не cookie. Upstream ID и URL никогда не выдавать в публичном статусе.

Внутренние повторные попытки и рестарты **не** создают второй invoice job в 8083: фиксировать upstream ID в durable checkpoint, использовать стабильный idempotency key для загрузки; повторить успешный этап без повторного извлечения. Нельзя использовать `snapshot.partial`/сырые SSE-дельты как окончательный результат.

### 3.2 Дополнительный прямой вход по готовому JSON

`POST /api/v1/invoices/import-json` с `Content-Type: application/json`, заголовками `X-API-Key` и `Idempotency-Key`; тело — **оригинальный полный** `invoice_extraction.v1` из 8083. Это нужно для интеграций, которые уже выполнили извлечение через 8083, и для независимой от VLM проверки Mapper. Прямой вход не вызывает 8083 и не меняет входные данные.

Загрузку файлов и импорт готового JSON реализовать как **два входных адаптера одного invoice use-case**, а не как две независимые логики сопоставления. Разные payload под тем же owner+idempotency key дают 409, повтор идентичного — тот же ID. Hash исходного JSON считается по канонической полной структуре, включает реквизиты, суммы, порядок, документы, строки и страницы.

### 3.3 Структура источника

Строго принимать:

```json
{
  "schema_version": "invoice_extraction.v1",
  "files": [
    {
      "file_index": 0,
      "source_file": "invoice.pdf",
      "source_sha256": "<64 lowercase hex>",
      "page_count": 3,
      "documents": [
        {
          "document_type": "invoice",
          "number": "000197452",
          "date": "2026-11-10",
          "supplier": {"name": "АО ЭМИС", "inn": "7729428453", "kpp": "744801001"},
          "buyer": {"name": "ООО ВЗЛЕТ-МСК", "inn": "5003097938", "kpp": "504701001"},
          "currency": "RUB",
          "items": [
            {
              "row_number": 1,
              "caption": "Датчик давления ЭМИС-БАР ...",
              "brand": null,
              "manufacturer": null,
              "article": null,
              "article_candidate": null,
              "supplier_code": null,
              "product_reference_raw": null,
              "row_kind": "product",
              "units": null,
              "quantity": "104",
              "unit_price_without_vat": "36160.00",
              "unit_price_with_vat": null,
              "line_total_without_vat": "3760640.00",
              "vat_rate": "22",
              "vat_amount": null,
              "line_total_with_vat": null,
              "source_pages": [2],
              "vat_rate_source": "document_total"
            }
          ],
          "totals": {"without_vat": "3760640.00", "vat": "827340.80", "with_vat": "4587980.80", "adjustments": []},
          "validation": {"status": "partially_checked", "checks": [], "warnings": []},
          "source_pages": [2],
          "attached_pages": [1, 3]
        }
      ]
    }
  ],
  "summary": {"file_count": 1, "document_count": 1, "item_count": 1}
}
```

Это **поясняющий укороченный пример**, а не точная schema fixture: обязательность дополнительных полей и `null` определяются актуальным DTO `InvoiceExport` из 8083. Не делать в Mapper вручную сокращённый валидатор, который отвергает реальные поля `notes`, `payment_terms`, `technical_parameters_pages`, `field_origin`, `derived_fields`, `adjustments` и пр. Для полного JSON использовать versioned DTO, контрактные тесты и явную политику расширения совместимых необязательных полей; неизвестную версию отклонять 422.

Источник сохранять без нормализации, «улучшения» сумм, удаления дублей, округления, переписывания НДС. Денежные значения — decimal-строки/`null`; никакого float и подстановок по 2026 году.

## 4. Внутренний invoice-адаптер сопоставления

Для каждой исходной строки формировать **производное** представление для существующего matching engine:

| Параметр matching engine | Источник `invoice_extraction.v1` | Правило |
|---|---|---|
| `Caption` | `item.caption` | Дословный caption, без удаления бренда/марки |
| `Units` | `item.units` | `null` → `""` только в derived DTO |
| `Quantity` | `item.quantity` | `null` → `""` только в derived DTO, на ранжирование не влияет |
| `Brand` | `item.brand` или при наличии подтверждённый `manufacturer` | Без домысливания; не смешивать неподтверждённый `article_candidate` |
| `Article` | `item.article` | Только подтверждённый артикул, не `supplier_code`, не `article_candidate` |

`specification_text()` и `rerank()` должны обрабатывать derived DTO без новой модели и второго векторного индекса. Если `article_candidate`/`supplier_code` найден, сохранять и показывать отдельно, **не** повышая ранг до точного совпадения артикула; допускается только явно объяснимый сигнал в UI. Для каждого кандидата сохранить rank, cosine, evidence, conflicts, версию каталога и исходные свойства.

**Адресация строк:** в публичном invoice API `row_index` глобально начинается с 0, стабилен для данного job и соответствует обходу `files[] → documents[] → items[]`. Обязательно хранить обратный указатель: `{file_index, document_index, row_number, source_pages, source_sha256}`. Повторный `row_number=1` в другом документе не конфликт. Нельзя сворачивать одинаковые товары или товары из разных счетов в одну строку.

**Виды строк:**
- `product` — обычное сопоставление с номенклатурой;
- `service` — использовать общий поиск, но если каталог не содержит достоверного признака «Услуга», не утверждать автоподбор без проверки; учитывать единицы;
- `advance_payment`, `final_payment` — оставить в исходном JSON, **не превращать автоматически в номенклатуру** и не запускать для них обычный товарный векторный поиск; статус `not_applicable`/`requires_review`, `selection=null`, передать в 1С как отдельную платёжную строку/реквизит для ручного решения;
- `other`, неизвестные или сомнительные — ручная проверка без автоматического назначения.

**Единицы:** `шт`, `м`, `упак` и т. п. проверяются существующим `unit_key`, но упаковка не пересчитывается автоматически. При несовместимости UOM статус review, даже если cosine высок. Нельзя подменить 2 упаковки на 200 штук без согласованного коэффициента 1С.

**GUID:** выбранный `catalog_item_guid` — внутренний UUID Mapper для команды выбора (как в PR #1); результат содержит **отдельно** `onec.onec_guid`, полученный из каталога 1С. Он не синтезируется из internal guid. Отсутствующий `onec_guid` при выбранной номенклатуре — сигнал `onec_guid_missing`, такой элемент нельзя считать автоматически готовым к заполнению Заказа поставщику. Имеющийся `GET /api/v1/catalog/items?q=...` используется для ручного поиска; подбор/выбор остаётся общим use-case.

## 5. API 8087: единый профиль «Счета»

Создать отдельный namespace **`/api/v1/invoices`**, не переписывая `/api/v1/specifications`, `/api/v1/mappings` или каталоговые API.

| Метод | Маршрут | Назначение |
|---|---|---|
| `POST` | `/api/v1/invoices` | Мультизагрузка файлов, создание durable job, 202 |
| `POST` | `/api/v1/invoices/import-json` | Вход готового `invoice_extraction.v1`, 202 |
| `GET` | `/api/v1/invoices/{job_id}` | Единый статус и ссылки, без внутренних job IDs |
| `GET` | `/api/v1/invoices/{job_id}/rows?offset=0&limit=100&status=all` | Строки и сохранённые соответствия, без повторной векторизации |
| `GET` | `/api/v1/invoices/{job_id}/candidates?row_index=0` | Top-5 выбранной строки |
| `PUT` | `/api/v1/invoices/{job_id}/rows/{row_index}/selection` | Выбор `catalog_item_guid` либо `null` и `revision` |
| `DELETE` | `/api/v1/invoices/{job_id}/rows/{row_index}/selection?revision=N` | Явное снятие выбора через существующий механизм выбора `null` |
| `POST` | `/api/v1/invoices/{job_id}/approve` | Атомарно утвердить текущие соответствия с финансовыми предупреждениями |
| `GET` | `/api/v1/invoices/{job_id}/draft.json` | Полный черновой JSON до подтверждения |
| `GET` | `/api/v1/invoices/{job_id}/source.json` | Неизменённый JSON `invoice_extraction.v1` |
| `GET` | `/api/v1/invoices/{job_id}/result.json` | Только immutable approved JSON, 409 до approve |
| `GET` | `/api/v1/invoices/{job_id}/result.xlsx` | Финансовый Excel из того же approved snapshot |

Если 1С нужен просмотр страниц через один адрес, предусмотреть `GET /api/v1/invoices/{job_id}/files/{file_index}/pages/{page}/preview` как защищённое серверное проксирование уже существующего endpoint 8083; не публиковать внешние ссылки/ключи 8083. Считать это обязательным API-контрактом для полноценного просмотра, но не создавать второй движок рендеринга.

Status: `queued`, `extracting`, `matching`, `awaiting_review`, `approved`, `failed`; `job_id`, `revision`, `created_at`, `expires_at`, `file_count`, `document_count`, `total`, `processed`, `unresolved_count`, `financial_warning_count`, `error_code`, `catalog_version`, `links`. Если upstream завершился `completed_with_warnings`, это предупреждение нужно сохранить; не считать математическую проблему технической ошибкой процесса.

Передача `selection` как в PR #1:

```json
{"catalog_item_guid":"UUID_ВНУТРЕННЕГО_КАТАЛОГА_MAPPER","revision":2}
```

или:

```json
{"catalog_item_guid":null,"revision":2}
```

`approve` — `{ "revision": 3, "acknowledged_document_warnings": ["0:0"] }` и `Idempotency-Key`. Список acknowledgment — идентификаторы документов `{file_index}:{document_index}`, где есть `validation.failed`, существенное несовпадение валюты/поставщика или иной финансовый блокер. **Добавить этот контроль только к invoice-профилю**, не изменять обычный `ensure_approvable` спецификаций. Approval не меняет `validation.failed` на `passed`, сохраняет причину, когда/кем подтверждена обработка предупреждения. Обычные неподтверждённые товарные соответствия подтверждаются общим approve, как сейчас.

Для документа с `not_checkable`/`partially_checked` показывать предупреждение и не выполнять автозаполнение финансовых реквизитов без пользовательского согласия; в `approved.json` сохранять исходный статус. Технически job может быть утверждён после явного подтверждения даже при `failed`, но это никогда не означает, что исходная арифметика исправлена. Это решение в рамках данного ТЗ: отдельное подтверждение финансового риска, не блокировка всей 1С навсегда.

## 6. Формат результата для 1С — `invoice_mapping.v1`

Корень: `schema_version`, `job_id`, `status`, `revision`/`approved_at`, `source_hash`, `catalog_version`, `algorithm_version`, `summary`, `files[]`, `approval`. `files[]/documents[]` имеют ту же структуру и порядок, что исходный invoice-export; реквизиты сторон, документы, `totals`, `validation`, `payment_terms`, `source_pages`, `attached_pages` и **каждое** финансовое поле каждого item сохраняются без изменения. Добавить к каждой позиции только:

```json
{
  "mapping": {
    "row_index": 0,
    "status": "matched",
    "confirmed": true,
    "manually_reviewed": true,
    "score": 0.91,
    "catalog_version": "<версия каталога>",
    "onec": {
      "guid": "<внутренний UUID Mapper>",
      "onec_guid": "<реальный UUID ссылки 1С>",
      "code": "<код 1С>",
      "name": "<наименование 1С>",
      "unit": "<единица 1С>"
    },
    "warnings": []
  }
}
```

Пример — **фрагмент нового объекта**, а не самостоятельный полный invoice JSON. В реализации согласовать имена вложенных полей с прежним публичным `ExportRow` (`onec`/`matching`) так, чтобы не плодить несовместимые структуры без необходимости. Предпочтение: `item.onec` и `item.matching` рядом с оригинальными полями `item`, если они уже употребляются существующим публичным контрактом. **Не дублировать `onec` одновременно внутри `mapping` и рядом.** Зафиксировать выбранный вариант в Pydantic/OpenAPI/контрактных тестах. `score` — диагностический cosine, не «вероятность правильности».

К каждому документу добавить `document_index`, а также итоговое число matching statuses и `ready_for_onec` (вычисляется из подтверждений и наличия реального `onec_guid`, не из арифметики модели). Разные поставщики и валюты остаются отдельными документами, даже если загружены вместе. `null` onec не выбрасывает исходную строку. `supplier_code`/`article_candidate` нельзя терять или превращать в GUID.

Запись в 1С остаётся ответственностью 1С: проверить соответствие реквизитов Заказа поставщику, поставщика и организации, налоговый режим, непроведённые/уже существующие строки, упаковки, обязательные поля. **8087 не отправляет HTTP-запрос создания Заказа поставщику** и не регистрирует номенклатуру. Формулировка «отправляем JSON в 1С» для первого этапа означает, что 1С получает утверждённый JSON через `GET .../result.json`; PUSH webhook — отдельное согласуемое расширение, если действительно понадобится.

## 7. Финансовые инварианты

1. Mapper не пересчитывает и не нормализует `quantity`, `unit_price_*`, `line_total_*`, `vat_rate`, `vat_amount`, `totals.*`, `adjustments` — они переносятся как строки и null. Нельзя использовать `float` для проверки и нельзя выводить НДС из года документа.
2. `validation.status` и `validation.checks` из 8083 копируются дословно вместе с предупреждениями; новый `mapping_status` не является заменой финансовой валидации.
3. Отсутствующий в документе `line_total_without_vat` не заполняется в Mapper по произведению округлённой цены на количество.
4. `invoice`/`commercial_offer`/`specification` сохраняют собственные типы, условия оплаты, номера, реквизиты. Коммерческое предложение не превращается в счёт.
5. Разный НДС по строкам, НДС старого счёта против ставки нового заказа 1С, разные UOM и валюты → явные предупреждения без молчаливого исправления. Выбор режима цены «включая/не включая НДС» делает 1С после подтверждения пользователя.
6. Повторы одинаковой позиции с разными ценами/из разных счетов не объединять автоматически.

## 8. UI 8087

Действующий интерфейс Mapper не переписывать. Добавить переключатель «Спецификации / Счета» в сопоставлении либо отдельный пункт «Сопоставление счетов» по уже существующим frontend guidelines. Для invoice-профиля переиспользовать существующую таблицу кандидатов, поиск номенклатуры, фильтры, редактирование, CAS/revision и approve.

Вид invoice-таблицы: файл, документ/номер/дата, поставщик, исходный номер строки, `caption`, артикул/код поставщика, единица, количество, цена без/с НДС, сумма строки, НДС, предложенная номенклатура 1С, top-5, статус, предупреждения. Сверху — карточки `файлов / документов / позиций / подобрано / требует проверки / финансовые расхождения`; просмотр исходного документа/страницы через 8087 preview. Для платёжных строк — отдельный бейдж «Не номенклатура». Предусмотреть подтверждение финансового предупреждения перед approve, но **не** исправление цифр через Mapper.

Web-клиент обращается к тем же invoice endpoints и owner/CSRF, что 1С (1С работает по ключу; web — по cookie), без client-side бизнес-логики. Страницы на reload возвращают сохранённые selections и текущую revision.

## 9. Структура файлов и границы ответственности

Пути уточнить по фактическому дереву актуального HEAD. Рекомендуемые **новые** файлы:

```
src/domain/invoices/__init__.py
src/domain/invoices/models.py       # DTO/валидация invoice_extraction.v1, mapping.v1
src/domain/invoices/adapter.py      # lossless исходные строки -> derived matching rows
src/application/invoices/__init__.py
src/application/invoices/service.py # бизнес use-case, status, approval guard
src/application/invoices/processing.py  # invoice-specific стадии, подключение к общей matching функции
src/infrastructure/invoices/__init__.py
src/infrastructure/invoices/extracting.py # вызов 8083 и polling
src/presentation/invoices.py        # тонкий FastAPI router/Pydantic schemas
src/application/invoice_reports.py # JSON/Excel invoice-профиля, без шаблона спецификаций
docs/INVOICE_MAPPING_STAGE_02.md
```

Если достаточно общего нового helper для `JobProcessor`, не создавать искусственных параллельных классов. **Никаких копий** `rerank`, `choose`, embeddings/Qdrant/`CatalogRepository`, worker loop, `MappingUseCases.select/approve`, аутентификации и общих ошибок. Добавлять небольшое расширение интерфейса/параметр профиля, где это реально нужно.

Разрешённые точечные правки существующих файлов: `src/bootstrap.py`, `src/main.py`, `src/application/processing.py`, `src/application/ports.py`, `src/infrastructure/http_clients.py`, `src/infrastructure/repositories.py`, `src/infrastructure/database.py`, `src/config.py`, `src/presentation/schemas.py`, существующие JS/CSS шаблоны; новые миграции Alembic **только** при реальной потребности. Для пакета из нескольких файлов текущий `specification_uploads` (один PDF на job) недостаточен: добавить безопасное совместимое временное сохранение нескольких blob по `(job_id,file_index)`, например отдельную таблицу `invoice_uploads` + миграцию 0006, не меняя и не удаляя запись старого спецификационного хранилища. Бинарные файлы не включать в JSON job и в брокер. Размеры ограничивать, принимать потоково, удалять по прежнему TTL.

Использовать единые Settings/DI/Ports & Adapters, прямую точку сборки в `bootstrap`, изоляцию PostgreSQL/Qdrant/RabbitMQ, шаблоны `.env.example`. Отдельных сервисов, docker compose или новых shared credentials кроме server-to-server настройки не создавать.

## 10. Состояния, ошибки, безопасность

- Всегда один внешний `job_id` UUID, секреты и внутренние upstream ID не раскрывать.
- В PostgreSQL хранить owner, kind/profile, hashes, immutable исходник, original file metadata, `source_job_id` только приватно, source revision, pinned `catalog_version`/`collection`/embedding identity, batched matching rows, revision-CAS, immutable approval.
- В `JobProcessor`/RabbitMQ повторная доставка безопасна: сохранённые batches не пересчитываются, lease/fence и outbox — прежние; изменение каталога не перепривязывает уже выбранные GUID.
- `queued/extracting/matching/awaiting_review/approved/failed` — публичные состояния; internal attempts/upstream stages отражать безопасно в диагностике.
- `owner` isolation: чужое 404, свой истёкший 410 до физической очистки, после — 404; `Idempotency-Key` всегда ограничен owner+payload+profile. Отдельные пространства ключей для `/specifications` и `/invoices`.
- API 1С: `X-API-Key`, браузер: HttpOnly cookie + CSRF/Origin; ключ 8083 только в серверном окружении 8087, никогда в JS/Swagger примерах в виде настоящего значения.
- Не выносить в JSON errors/logs содержимое счетов, полные наименования, ИНН/КПП, промпты, исходные байты и секреты. В логах — job ID, elapsed, counts, технический error_code.
- При `upstream_failed/cancelled/expired/timeout`, `catalog_not_ready`, `embedding_incompatible`, `qdrant_unavailable`, `invalid_invoice_source` — отдельные коды; не превращать в успешное пустое сопоставление.
- TTL для временных invoice jobs/approved snapshots — по действующей политике Mapper (1 час после завершения), для незавершённых и ожидания 8083 — действующий bounded deadline. Не сокращать время обработки больших PDF. TTL никогда не чистит постоянный каталог, векторы или согласованные версии.
- Реальная недоступность каталога не маскируется fallback на случайную номенклатуру.

## 11. Экспорт Excel

Не использовать старый спецификационный шаблон «Лист_1» для счетов. Новый отдельный invoice export с листами **«Документы», «Позиции», «Предупреждения»**; по возможности повторить колонки Excel 8083 и добавить в «Позиции» правые столбцы: `Код 1С`, `Наименование 1С`, `Реальный GUID 1С`, `Ед. 1С`, `Статус сопоставления`, `Индекс Mapper`, `Подтверждено`.

Все финансовые колонки и оригинальные строки должны совпадать с утверждённым JSON. GUID, артикулы/коды, ведущие нули, длинные числа (Excel >15 digits), пользовательский текст — безопасные текстовые поля без формульных инъекций. Числа только при безопасной сериализации Decimal. До approve Excel → 409.

## 12. Обязательные проверки и Definition of Done

**Безопасность и регрессии (offline):**

1. Старые specification endpoints, старые `/mappings`/`import-json`, PDF и Excel неизменны; текущие тесты PR #1 продолжают проходить.
2. Строгий `invoice_extraction.v1` (+ допустимые документированные дополнительные поля), `null` финансов, Decimal-строки, order/dedup, source hash; `source.json` полностью сохраняет вход.
3. Файлы 1–10, PDF/JPEG/PNG, порог 50/100 MiB, pages; неверный MIME, corrupt/empty, oversize, неверные page selections, 413/415/422; idempotency повторного POST и конфликт 409.
4. Создание, рестарт и восстановление этапа 8083 без повторного запуска; 404/410/502/504 upstream, истёкший результат, cancellation/timeout.
5. Бренд/артикул, `supplier_code` отдельно, `article_candidate` не повышает rank; единицы `упак` против `шт` не автопринимаются; Top-5/candidates/CAS/approve.
6. `final_payment`/`advance_payment` не появляются как обычная номенклатура; `service` обрабатывается корректно; `not_applicable` не ломает общие counts.
7. `document_type`, `supplier`/`buyer`/`currency`, все суммы/НДС, `validation` и страницы идентичны источнику; 1С может отобразить разные заказы по разным поставщикам.
8. Несовпадение сумм, отсутствие суммы строки или НДС, financial acknowledgment, невозможность одобрить неподтверждённый блокер без явного действия; расхождение после approve остаётся в JSON.
9. Только approved immutable JSON/Excel; никаких raw source patches; проверка GUID: `onec_guid` настоящий, без подмены internal GUID; `null` кандидата остаётся `null`.
10. Auth owner/TTL/CSRF/IDOR, no secret leakage, rate limits, browser reload и 1C API/OpenAPI contract snapshots.

**Реальная интеграция (отдельно от doubles):**

- После синтетических тестов взять **семь локальных PDF/JSON** из `examples/invoices/` Extracting (отдельный corpus, не коммитить публично): `975_2310538`, `000197452_ВЗЛЕТ-МСК ООО_10.11.2026_2`, `52523891_ООО _Взлет-МСК__12.05.2026`, `54304488_ООО _Взлет-МСК__21.09.2026`, `doc03026420260218101354`, `print(2092)`, `Спецификация_№_13_от_20_января_2026_г_pdf`;
- проверить 7 файлов, 7 документов, **161 исходную позицию**, идентичные реквизиты/денежные значения/количество/порядок/источник, отчёт о найденных кандидатов и конфликтах;
- выполнить хотя бы один реальный `POST 8087 /api/v1/invoices` с 2–3 файлами → RabbitMQ → 8083 → VLM → 8087 Qdrant → выбор позиции → approve → approved JSON и Excel, и отдельно `POST .../import-json` без VLM;
- проверить товар с подтверждённым артикулом, неоднозначный кандидат, несопоставленный товар, услугу, финальный платёж без количества и конфликт UOM;
- проверка ответа API для программиста 1С на сетевом адресе 8087, включая Swagger `/docs`/`/api/openapi.json`, auth и TTL;
- измерить throughput/latency, memory и размер PostgreSQL blob при 10 файлах; не объявлять mocks live-тестами.

**Порядок качества и выпуска:** перед изменением — сверка `git status`, `branch`, `HEAD`, `git diff`, действующих контрактов/тестов `Amadu-A/spec-mapper-ai`, `Amadu-A/spec-extracting-ai`, `Amadu-A/shared_infrasktructure/docs`; не основываться только на этом документе. Каждое изменение — типизированные Python 3.12+, Ruff/pytest/architecture/integration/regression/security/browser/E2E, `git diff --check`. Для Windows использовать существующий `ops/check-quality.ps1`; для Linux — `bash scripts/test.sh`, затем точечный `bash scripts/up.sh` **только для Mapper** после проверки совместимости миграции, без `down -v`, prune, удалений volumes или перезапуска shared stack. Коммит/push исключительно после проверки и **только пользователем**, с явными путями `git add` (никогда `git add .`).

**Приёмка:** клиент 1С через **8087** под одним ключом и одним внешним job получает после подтверждения пригодный для заполнения заказа JSON со всеми исходными финансовыми данными и выбранным реальным GUID 1С, без ошибок арифметики, без потерь/слияний строк и без изменений существующей интеграции спецификаций.

## 13. Не входит в этот этап

- UI формы и запись/проведение «Заказа поставщику» в ERP (исполнитель 1С);
- создание новых номенклатур в справочнике 1С, задачи НСИ и синхронизация после создания;
- автоматическая конвертация единиц/упаковок, самостоятельный пересчёт НДС под документ 1С;
- push результата в 1С через неизвестный webhook (сейчас **1С забирает approved.json**);
- изменения OCR/VLM/промптов Extracting 8083, правки общих shared сервисов;
- замена уже принятого PR #1 или рефакторинг legacy спецификаций без необходимости.

## 14. Требования к итоговому отчёту Codex

Дать список всех новых/изменённых файлов, архитектурные решения и границы переиспользования, версии API/JSON, результаты offline/live тестов **раздельно**, Windows PowerShell-команды Ruff/pytest/architecture/regression/browser/`git diff --check`, явные команды `git add <paths>`/`commit`/`push` для выполнения владельцем, затем Linux `pull/test/migration/rebuild/health/real-integration` команды. Приложить примеры запросов/ответов для 1С без ключей и личных данных; фиксировать непроверенные случаи, а не выдавать их за успешные.
