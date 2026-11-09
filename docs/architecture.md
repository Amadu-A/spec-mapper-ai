<!-- docs/architecture.md -->
# Архитектура

`domain ← application ← infrastructure`; HTTP и worker — delivery. `bootstrap.py`
собирает адаптеры и use-cases. Бизнес-подбор не находится в роутерах или JS.
Порты описаны через Protocol в `application/ports.py`. SQLAlchemy разрешён только
в infrastructure, Alembic и операторских/integration tests.

## Данные и транзакции

- `mapping_jobs` — временный агрегат задания: owner, idempotency hash, request hash,
  source snapshot, отдельные rows/candidates/selection, revision, TTL, lease и outbox.
- `mapping_approvals` — неизменяемый финальный snapshot, owner, revision, время и hash
  ключа утверждения. Удаляется вместе с временным заданием; TTL не продлевает.
- `catalog_versions`, `catalog_entries` — постоянные неизменяемые снимки номенклатуры.
- `catalog_state` — единственный активный указатель, предыдущая версия, последняя
  успешная синхронизация и код последней ошибки.
- `catalog_vectors` — постоянный кэш векторов по hash позиции + fingerprint модели.

Границы Unit of Work представлены атомарными repository операциями: create+outbox,
claim, fenced checkpoint, ручной выбор+revision, stage и activate. Каждая операция
использует собственную короткую транзакцию; SQL не протекает в application.
Ручной выбор блокирует строку и проверяет revision повторно через CAS update.
Новый запрос со старой revision получает 409. Номера строк нулевые и стабильные.
Approval блокирует ту же строку задания, повторно проверяет revision/готовность и
одним явным действием подтверждает все текущие selection/null. Выбранная review
становится manual, пустая — not_found, matched сохраняет автоматический статус;
все строки получают confirmed/manually_reviewed. Rows, метрики и immutable snapshot
фиксируются атомарно, job переходит в approved. Новые кандидаты не подбираются,
source/TTL/revision сохраняются. Последующие selection запрещены. CAS ручного
изменения также проверяет допустимое состояние, защищая конкурентный approve/clear.

DELETE selection использует тот же use-case сохранения явного null с owner,
CSRF и revision. Исходная строка и кандидаты не удаляются; изменение выбора
увеличивает revision. Метрики отражают подтверждённую пустую строку без unresolved.

Cookie owner и API owner изолированы; неверный API-ключ не переключается молча на cookie.
Чужой job возвращает 404; собственный истёкший — 410 до очистки, затем 404.

## Долговечный worker

Create фиксирует outbox в PostgreSQL без зависимости от доступности брокера.
Dispatcher публикует только UUID с publisher confirm; после confirm отмечает dispatch.
Неотмеченная публикация повторяется; CAS claim не позволяет второму consumer начать
ту же работу. Lease продлевается heartbeat; истёкший fence не принимает checkpoint.
Повторный consumer возобновляет JSON snapshot и завершённые пачки.
Зависимости повторяются ограниченно; permanent upstream errors заканчиваются failed.
При restart невзятый outbox и истёкшие lease восстанавливаются автоматически.

`source` никогда не обогащается: rows хранятся отдельно, привязаны к `source_hash`,
`source_job_id`, `catalog_version` и `algorithm_version`. Ручной выбор хранит snapshot
позиции и версию каталога, из которой выбран. Обновление каталога его не переприсваивает.

## Согласование PostgreSQL и Qdrant

1. Advisory lock сериализует импорты.
2. XML валидируется полностью; вычисляется hash и compatibility fingerprint.
3. При прежней identity готовый Qdrant индекс копируется через snapshot/recover
   в новую коллекцию; upsert выполняется только для новых/изменённых позиций,
   удалённые UUID исключаются. Неизменённые точки не переиндексируются. При смене
   identity строится новый индекс, embedding-кэш используется по fingerprint.
4. Проверяется точное число точек, staging metadata сохраняется в PostgreSQL.
5. Qdrant alias переключается одним запросом, затем транзакцией меняется DB pointer.
   При ошибке commit alias откатывается.

Staging хранит `initialized`, `resume_offset`, base version, counts, source hash и
fingerprint. После сбоя переиспользуется совместимый unpublished снимок и vector cache;
offset фиксируется после durable upsert пачки. Для каталога отдельный deadline 24 часа,
вместо часового TTL mapping. Живой импорт защищает staging/base от фоновой сборки.
Опубликованный неизменённый источник проверяется по реальному существованию/count индекса.

Межсервисной distributed transaction нет. Поэтому **поиск всегда использует concrete
collection из PostgreSQL snapshot**, а не alias: crash между двумя операциями
не смешивает версии и не выключает старый поиск. Предыдущая коллекция сохраняется.
Смена модели/размерности/probe создаёт новый fingerprint и новый индекс.
Metadata `created` и случайные permission IDs `/models` не входят в identity.
Контрольный вектор хранится с metadata identity. Численный шум GPU проверяется по
cosine (не ниже `1−1e−6`) и максимальному отклонению (`1e−4` с учётом масштаба),
а не по точному равенству хешей float. Metadata/revision/размерность проверяются строго.
Fingerprint индекса закреплён за совместимым reference: шум не меняет vector cache.
Старый формат получает reference только после точного совпадения прежнего fingerprint;
неизвестная identity не принимается молча.

Каждая новая версия публикует новый Qdrant snapshot. Нормальный incremental import
переносит готовый индекс [через восстановление snapshot](https://api.qdrant.tech/v-1-16-x/api-reference/snapshots/recover-from-snapshot)
и применяет только delta. Это требует временного места для копии, но не полной
переиндексации неизменённых позиций. Промежуточный snapshot удаляется после restore.

REST запросы закреплены контрактными тестами по [Qdrant 1.16 batch API](https://api.qdrant.tech/v-1-16-x/api-reference/search/query-batch-points)
и [атомарным alias operations](https://api.qdrant.tech/api-reference/aliases/update-aliases).
Образ PostgreSQL закреплён на [16.15](https://www.postgresql.org/docs/release/16.15/).
Автоматическая сборка obsolete версий сохраняет active/previous и все snapshots,
на которые ссылаются живые задания. Короткий advisory guard защищает pin/publish/prune
от гонок отдельно от долгого lock синхронизации, поэтому embedding нового каталога
не блокирует поиск по старому.

## Эксплуатация

API/worker используют общие только RabbitMQ и embedding; PostgreSQL/Qdrant находятся
в private сети без host ports. Модельные веса, GPU и vLLM в приложении отсутствуют.
Daily sync — встроенное расписание worker без отдельного orchestrator. Адаптер
реальной 1С добавляется через `CatalogSource`; до утверждения API не имитируется.

`/health/live` проверяет процесс. `/health/ready` — схему БД, реального consumer
RabbitMQ, embedding identity, Qdrant count и готовый каталог. Браузерные индикаторы
используют этот ответ. Логи JSON с безопасным allow-list; timing применён к ключевым
use-cases. Docker local driver хранит не более пяти файлов по 10 МБ на контейнер.

## Развитие V2

`domain/catalog_sync.py` — чистое UTC расписание/метрики; application CatalogManagement
зависит от SyncRepository/CatalogSources. Infrastructure реализует SQL transaction lock,
HTTP source, operator tickets и технические probes; bootstrap собирает зависимости.
Worker использует прежний outbox/processor; source override входит в тот же каталоговый
pipeline. Миграция 0003 добавляет отдельные настройки/историю/счётчики и не меняет 0001/0002.
Browser модули разбиты по реальным страницам, источник истины — backend.
[Точная семантика и ограничения](DASHBOARD_AND_SYNC.md).

## Операторский вход и действия V3

Публичный API остаётся на 8087. Для выдачи короткого operator-ticket кнопкой
добавлен транспортный `operator-gateway` на loopback сервера 127.0.0.1:8088
(`OPERATOR_PORT`). Он использует тот же image, не содержит бизнес-логики,
подключён только к отдельной сети operator_access с API и не имеет ai-shared/Docker socket.
SSH-туннель ведёт на этот listener. Gateway подписывает короткий 15-секундный
proof существующим SESSION_SECRET и удаляет входящие служебные заголовки;
сам Host=localhost не повышает роль. API проверяет proof, обычный web owner
и CSRF до вызова прежнего ticket use-case. Код отображается пользователю, затем
обменивается на прежнюю короткую HttpOnly operator cookie. CLI ticket сохранён.

Пользовательский refresh берёт только неизменяемую конфигурацию сохранённого
подтверждённого HTTP источника из SQL; local XML требует роли оператора. Под
существующим advisory transaction lock выполняются replay, revision CAS,
проверка общего 60-секундного cooldown, проверка active job и create+outbox+run.
Cooldown хранится в service_activity и переживает смену browser owner/restart.
Второй request в интервале получает 429/Retry-After; replay прежней операции
не создаёт новый импорт. Scheduler и ручное действие используют прежний pipeline,
ручной запуск не меняет next_run_at. Доверенный административный imports/CLI
оставлен отдельным совместимым путём bootstrap без пользовательского cooldown.

Новая миграция и новые секреты не требуются. Frontend добавляет общее подтверждение,
очистку выбранного соответствия и локальный SVG favicon; browser не содержит
правил ранжирования, credentials и общего administrative key.
