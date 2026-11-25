<!-- docs/JOB_LIFECYCLE.md -->
# Жизненный цикл заданий Stage 03B

Оба рабочих пространства (`/mapping`, `/invoices`) используют PostgreSQL как источник
истории. Переходы и F5 не создают задания. Приоритет: явный `?job=UUID`, последнее
активное задание, последний результат для проверки, новая форма. `?new=1` явно
открывает новую форму; прежние задания продолжают работу. Все задания доступны на `/jobs`.

Форма блокируется синхронно до чтения файлов/первого HTTP запроса. После 202 ключ
не меняется. При неизвестном исходе отправки можно повторить запрос с прежним ключом
или явно начать новое задание. Поздний ответ прежней формы не переключает новую.
Документы, финансовые JSON и ключи не записываются в browser storage.

## API

Для `family = mappings | specifications | invoices` добавлены:

| Запрос | Результат |
|---|---|
| `GET /api/v1/{family}/{job_id}/snapshot` | Безопасные metadata и cursor журнала |
| `GET /api/v1/{family}/{job_id}/events?after=0` | SSE из журнала Mapper, без отдельного polling 8083 |
| `POST /api/v1/{family}/{job_id}/cancel` | Durable отмена; повтор идемпотентен |
| `GET /api/v1/specifications?state=active&limit=20&offset=0` | Свои спецификации, включая legacy JSON/UUID |
| `GET /api/v1/invoices?state=awaiting_review&limit=20&offset=0` | Свои счета |

История принимает пустой state, `active`, `awaiting_review` или конкретное состояние.
Ответ: `total`, `active_total`, `offset`, `items`; сортировка по created_at убывает.
`limit` — 1–100. Старый `GET /mappings` сохранён. Dashboard/operator jobs дополнены
фильтром `profile=specification|invoice`, profile-safe ссылками и признаками известных totals.
Операторский обзор не открывает чужие документы, отмену, filenames и exports.

Snapshot содержит profile/api_root/status/stage/revision, timestamps/TTL, elapsed/wait,
текущие файл/физическую страницу, выбранные страницы и их состояния, processed/total
страниц и позиций, counts, количество документов/предупреждений, cancel_requested,
error_code, last_event_id/journal_start_id и ссылки. Неизвестные totals — `null`.
Для импорта готового JSON `extracting_applicable=false`.
В нём нет raw/partial VLM, исходных позиций, сумм, upstream UUID, URL, lease или секретов.
Старые status DTO дополнены `progress` и `reused`; JSON/Excel exports не менялись.

Нормализованные состояния: `queued`, `extracting`, `matching`, `awaiting_review`,
`approved`, `cancelling`, `cancelled`, `failed`. `stage=upstream_queued` отличает очередь
8083; `transferring` — отправку. Перечень этапов и проценты строятся по фактам,
без 0/0 и предполагаемого ETA. Ошибки файлов и неизвестный исход отправки видны отдельно.

SSE кадр: `id: N`, `event: lifecycle`, `data: {id, at, kind, data}`.
`Last-Event-ID` имеет приоритет над `after`, cursor исключающий. Cursor не является
revision ручного выбора. Журнал ограничен 200 событиями; утраченный/будущий cursor —
409, клиент заново читает snapshot и начинает с `journal_start_id - 1`.
Heartbeat — 15 секунд, длительность подключения — 120 секунд, затем `event: reconnect`.
Terminal закрывает stream. 404/410/истечение доступа очищают временный UI.
Лимиты на процесс API: 4 stream/owner, 64 всего; 429 с `Retry-After: 15`.
При живом SSE нет второго постоянного polling. При обрыве — bounded backoff,
snapshot и повторное подключение; соблюдается Retry-After.

Все маршруты сохраняют API-key/cookie owner isolation, CSRF+Origin для cookie writes,
404 чужого/неверного профиля, 410 после TTL. Отмена approved запрещена (409).
Managed `/specifications` требует свой профиль; legacy `/mappings` сохраняет совместимость.

## Идемпотентность и ограничение нагрузки

Атомарный active fingerprint: owner, профиль, вид входа, упорядоченные SHA-256 файлов,
параметры/канонические страницы, project_code, полный JSON либо внешний UUID.
Текст, порядок и дубли исходного документа не нормализуются. Разные ключи одинакового
active входа получают тот же job и `reused=true`. Алиасы ключей сохраняются после
завершения: другой payload с тем же ключом — 409. Другой owner/профиль независим.
После завершения явно новый ключ может создать новое задание.

Короткая PostgreSQL-транзакция с advisory lock защищает admission/алиасы/создание
job+blobs+outbox. Сетевые операции выполняются вне транзакции.
По умолчанию: 32 активных задания, 24 queued, 8 активных/owner, 2 GiB логических
временных JSON/blobs/approvals и резервов журнала. Превышение — 429, Retry-After 30.
Настройки `JOB_*` находятся в Settings/.env.example/Compose; новых секретов нет.
Это ограничение нагрузки для прежних двух worker slots, а не обещание времени ожидания.
Прежние файловые лимиты, concurrency/prefetch=2 и административные импорты сохранены.

24 queued ограничивают очередь двенадцатью волнами по два слота; active=32 также
ограничивает retry/cancelling, не попавшие в queued. Owner=8 — четверть общего cap,
чтобы один клиент не занял весь admission. 2 GiB ограничивают логические временные
данные независимо от count: даже 32 максимально допустимых пакета не принимаются
только потому, что count ещё свободен. Это защитные настраиваемые defaults, не оценка
свободной RAM/диска хоста. Для оценки производительности использовать реальные метрики.
Четыре SSE/owner допускают по две вкладки двух профилей; общий cap и конечная
длительность не дают подпискам накапливаться бесконечно.

## Отмена

Queued без отправки в 8083 закрывается локально без VLM. При matching отмена делает
старую lease недействительной и проверяется перед новыми внешними batch-вызовами. Отказ от draft
разрешён, approved snapshot неизменен. TTL отменой не продлевается.

Managed job с известным upstream UUID вызывает профильный cancel 8083 серверным ключом.
Пока 8083 отвечает running/недоступен, Mapper остаётся `cancelling` и повторяет отмену
с backoff. `cancelled` означает доказанный terminal upstream, включая гонку с его
успешным завершением; outcome сохраняется. Внешний legacy UUID отменяет только Mapper.

До upstream POST фиксируется intent. Поздний ответ сохраняет binding даже после отмены.
Если процесс погиб с неизвестным исходом POST и UUID не получен, остаётся
`cancelling / cancel_unknown / upstream_submission_unknown` до разрешения или TTL.
Новое извлечение ради получения UUID для отмены не запускается. Контракт 8083 не
предоставляет owner-safe read-only lookup по Idempotency-Key; такую операцию можно
добавить отдельной согласованной задачей Extracting. Изменений 8083 в Stage 03B нет.

## Доставка и восстановление

0007 отделяет transport от business state: `pending → publishing → published → received`,
terminal → `closed`. Reservation имеет token/generation/lease; confirm условен.
Publisher mandatory, positive confirms и `on_return_raises=True`; NACK/return/timeout
оставляют pending с backoff/jitter. Сетевой publish ограничен 8 секундами,
reservation — 20 секундами. Небольшие batch не меняют concurrency/prefetch.

Подтверждённый published waiter больше не публикуется каждые 30 секунд. Получение
сообщения фиксирует receipt и lease до ACK. Даже ранний ACK дубля после смерти
worker оставляет durable receipt: истечение lease создаёт новую generation.
Старый worker/confirm не переписывает новую generation. UUID-only сообщения совместимы.
Повтор после неопределённого confirm допустим; бизнес-обработка остаётся идемпотентной.
Exactly-once RabbitMQ не заявляется: publisher confirms и consumer ACK независимы.
См. [RabbitMQ confirms](https://www.rabbitmq.com/docs/confirms) и
[aio-pika publisher confirms](https://docs.aio-pika.com/rabbitmq-tutorial/7-publisher-confirms.html).

Transport общий для mapping, managed specification (kind=mapping), invoice, catalog,
catalog_events. Прежние scheduler, webhook inbox, checkpoints/staging и TTL сохранены.
Политики RabbitMQ, очередь production и shared stack не изменяются.

CLI `delivery-status` даёт ограниченные технические metadata, generation, attempts,
recoveries и duplicate_noops. `delivery-reconcile --job-id UUID` принимает 1–10 явных
различных UUID; только published/legacy_unknown, без действующей lease, старше 5 минут.
Применять после доказанной утраты очереди или drain, не циклом и не ко всем заданиям.
Он не отменяет jobs и не читает сообщения RabbitMQ.

## Переход с 0006

0007 additive: старые UUID, checkpoints, leases, PDF, approvals и сообщения сохраняются.
Старые terminal → closed, lease → received, dispatched без receipt → legacy_unknown,
остальные → pending. Неопределённые старые публикации автоматически не размножаются.
Нужны контролируемый drain и проверка состояния перед явным reconciliation.
Порядок обновления и проверки — [отчёт Stage 03B](STAGE_03B_REPORT.md).

Старый worker не понимает отмену/новые generation/алиасы. После использования этих
функций откат только бинарника на 54b5dd9 небезопасен. При отказе остановить admission
и собственные API/worker, сохранить БД/очередь и исправлять вперёд; downgrade не выполнять.
