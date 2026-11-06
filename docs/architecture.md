<!-- docs/architecture.md -->
# Архитектура

`domain ← application ← infrastructure`; HTTP и worker — delivery. `bootstrap.py`
собирает адаптеры и use-cases. Бизнес-подбор не находится в роутерах или JS.
Порты описаны через Protocol в `application/ports.py`. SQLAlchemy разрешён только
в infrastructure, Alembic и операторских/integration tests.

## Данные и транзакции

- `mapping_jobs` — временный агрегат задания: owner, idempotency hash, request hash,
  source snapshot, отдельные rows/candidates/selection, revision, TTL, lease и outbox.
- `catalog_versions`, `catalog_entries` — постоянные неизменяемые снимки номенклатуры.
- `catalog_state` — единственный активный указатель, предыдущая версия, последняя
  успешная синхронизация и код последней ошибки.
- `catalog_vectors` — постоянный кэш векторов по hash позиции + fingerprint модели.

Границы Unit of Work представлены атомарными repository операциями: create+outbox,
claim, fenced checkpoint, ручной выбор+revision, stage и activate. Каждая операция
использует собственную короткую транзакцию; SQL не протекает в application.
Ручной выбор блокирует строку и проверяет revision повторно через CAS update.
Новый запрос со старой revision получает 409. Номера строк нулевые и стабильные.

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

Межсервисной distributed transaction нет. Поэтому **поиск всегда использует concrete
collection из PostgreSQL snapshot**, а не alias: crash между двумя операциями
не смешивает версии и не выключает старый поиск. Предыдущая коллекция сохраняется.
Смена модели/размерности/probe создаёт новый fingerprint и новый индекс.
Metadata `created` и случайные permission IDs `/models` не входят в identity.

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
