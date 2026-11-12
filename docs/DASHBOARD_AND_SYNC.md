<!-- docs/DASHBOARD_AND_SYNC.md -->
# Главная и управляемая синхронизация

Развитие контрактов `677f1d2` и главной `bf9afa9`. Эталон — предоставленный владельцем
`renders/render_v2.png`. Старые mapping API, пять исходных строковых полей,
Top-5, revision, manual/null, approve и approved.json сохранены.

## Страницы и полномочия

`/` — главная, `/mapping` — прежняя таблица, `/jobs` — задания,
`/catalog` — поиск/карантин, `/settings` — источник/расписание,
`/integrations` — документация, `/events` — история, `/help` — справка.
Прежние `/?job=UUID` и `/?source_job_id=UUID` перенаправляются на `/mapping`
с теми же параметрами и owner cookie. Навигация — обычные ссылки, reload и
back/forward не теряют маршрут. Документы по-прежнему исчезают по TTL через час.

Обычная web-сессия видит свои задания и KPI, активный каталог, read-only расписание
и состояния интеграций. Named API owners изолированы. Оператор видит общие
агрегаты и метаданные заданий; это **не** даёт доступа к чужим source/result,
экспортам, выбору или approve. В общем обзоре такие действия отсутствуют.
Секреты API и 1С не передаются в HTML/JS/localStorage.

Для оператора нет регистрации/профилей. Откройте SSH-туннель к отдельному loopback
входу сервера; замените `ВАШ_ЛОГИН` собственным SSH-пользователем:

```powershell
ssh -N -L 18087:127.0.0.1:8088 ВАШ_ЛОГИН@192.168.55.3
```

На `http://localhost:18087/operator` нажмите **«Получить одноразовый код»**, затем
введите показанный код и начните сессию. Команда SSH скрывается, когда сервер
подтвердил вход через доверенный gateway; повторно выполнять её не требуется.
Код действует 120 секунд и используется один раз. Он не является CATALOG_ADMIN_KEY.
Роль живёт максимум 900 секунд, имеет
HttpOnly/SameSite=Strict cookie, отдельный CSRF/Origin, серверный отзыв и аудит.
Вход через обычный HTTP LAN запрещён. HTTPS за reverse proxy требует корректного
`APP_PUBLIC_BASE_URL`, TLS на доверенной границе и закрытого обходного доступа;
Forwarded/X-Forwarded-* не доверяются. Серверные клиенты по-прежнему используют
`X-Catalog-Key`, без cookie. `X-API-Key` не даёт операторских полномочий.

Gateway публикуется только на `127.0.0.1:${OPERATOR_PORT:-8088}` сервера, находится
в отдельной сети operator_access только с API и проксирует запросы. У него нет ai-shared, Docker socket и
командного shell endpoint. Выдача кода вызывает тот же серверный use-case, что CLI,
через `POST /api/v1/operator/tickets`: нужны обычный web owner, CSRF/Origin и
15-секундный подписанный gateway proof на существующем `SESSION_SECRET`.
Gateway формирует proof сам и удаляет присланные клиентом служебные заголовки.
Имя `localhost` или подставленный Host не являются разрешением на выдачу кода.
Публичный web/API остаётся на 8087; новый loopback-порт не открывается в LAN/firewall.
Новых секретов для gateway нет.

Прежний туннель на серверный 8087 сохраняет ручной CLI-вход: создайте код в другой
SSH-сессии и введите его. Кнопка выдачи кода там недоступна:

```bash
docker compose --env-file .env.example --env-file .env exec -T api /app/.venv/bin/python -m src.cli operator-ticket
```

## Ручное обновление и общее подтверждение

Переключатель автообновления, время и поля источника редактирует только оператор.
Кнопка **«Обновить сейчас»** использует `POST /api/v1/catalog/refresh` в обоих режимах.
Обычный owner может запустить только сохранённый в PostgreSQL HTTP-источник 1С с
подтверждённым контрактом/probe; URL и credentials не принимаются от клиента.
Локальный XML через эту кнопку доступен оператору. Неизвестный контракт остаётся
блокировкой, даже если введён URL или присутствует API-ключ.

Успешно принятый ручной запуск включает общий SQL cooldown на 60 секунд для всех
пользователей и API replicas. Повтор с новым ключом в этот период получает 429 и
`Retry-After`; UI показывает остаток времени и блокирует кнопку. Повтор принятого
запроса со своим прежним `Idempotency-Key` возвращает тот же job без нового импорта
и без продления cooldown. Изменение конфигурации между проверкой и enqueue
защищено revision CAS. Активный импорт также блокирует второй запуск. Ручное
обновление не сдвигает календарное расписание.

Административный `POST /catalog/imports` с CatalogKey/операторской ролью и CLI импорт
сохранены для bootstrap и совместимости; минутный лимит нового пользовательского
действия на этот доверенный административный путь не распространяется.

После `complete`/`ready_for_review` кнопка **«Утвердить результат»** подтверждает
все текущие выборы и пустые строки одной атомарной операцией. Отдельно открывать
каждую спорную строку не нужно. Новый кандидат вместо `null` не подбирается:
выбранная `review` становится `manual`, пустая — `not_found`, исходная `matched`
сохраняет автоматический статус. Все строки получают `confirmed=true` и
`manually_reviewed=true`; rows, метрики и immutable approved.json фиксируются вместе.
Source, TTL и утверждаемая revision не меняются; последующие правки запрещены.

Крестик у выбранной номенклатуры вызывает
`DELETE /mappings/{job_id}/rows/{index}/selection?revision=N`: удаляется только
соответствие, не исходная строка и не Top-K. Результат — `null`, «Соответствие не
выбрано», «Не найдено / Подтверждено человеком». Owner, CSRF и revision проверяются
сервером. Приложение использует локальный SVG-фавикон в стиле существующего логотипа.

## Что означают показатели

API/PG/Qdrant/embedding проверяются реально через существующий readiness,
Worker — по SQL heartbeat с TTL 45 секунд. API может быть degraded при работающем
процессе. Это не заменяет `/health/live` и `/health/ready`.

Каталог — разрешённые активные позиции текущего снимка. Дата успеха отличается от
последней попытки; неудача следующего импорта сохраняет предыдущую рабочую версию.
«Доступно» не означает, что неизвестный HTTP endpoint 1С подключён.

Задания считаются по дню **создания в UTC**. Знаменатель процентов — позиции jobs,
достигших complete/ready_for_review/approved, созданных в эти UTC-сутки.
Автоматические matched и manual — разные числа. Требуют проверки review и
неподтверждённые not_found; явный подтверждённый null сюда не входит. Failed jobs
учитываются отдельно, не превращаются в процент ревью. При нулевом знаменателе —
«Нет данных». Дельта — процентные пункты двух предыдущих полных недель, только
после 14 дней наблюдения и ненулевых данных обеих недель.

В SQL сохраняются только счётчики, день и hash owner, без Caption/JSON/кандидатов.
Дневные/собственные агрегаты хранятся 90 дней; общий накопленный счётчик без owner
сохраняется постоянно. Начало наблюдения показано явно. Истёкшие документы не
восстанавливаются из KPI. Bootstrap учитывает только ещё доступные старые jobs,
не придумывает историю до миграции.

## Настройки и scheduler

Additive миграция `0003` добавляет `catalog_sync_settings`, `catalog_sync_runs`,
`dashboard_daily_metrics`, `service_activity`, `operator_tickets`, `catalog_audit`.
Таблицы 0001/0002 и snapshots не меняются. Первый runtime bootstrap один раз читает
`CATALOG_DAILY_SYNC` и `CATALOG_SYNC_TIME_UTC` (default OFF, 15:00 UTC). Затем
PostgreSQL — единственный источник GUI настроек; изменение env их не перезаписывает.
Старый `CATALOG_SYNC_SECONDS` сохранён для совместимости Settings, но не управляет
календарным расписанием. Для существующего ON первый слот — ближайший будущий
HH:MM UTC после bootstrap, без немедленного импорта при migration/save.

Только оператор сохраняет включение/время/источник/открытые HTTP поля с revision CAS. Сохранение
само не запускает импорт. Следующий слот фиксирован по UTC, независимо от времени
последнего успеха. Worker проверяет его каждые 3 секунды. PG advisory transaction
lock и unique date+source claim исключают дубли у replicas/restart. После простоя —
один последний catch-up, без очереди за все пропущенные дни. Ручной импорт в тот же
момент объединяется с плановым, если источник совпадает; другой активный источник
приводит к безопасному пропуску слота с аудитом. Второй ручной импорт получает 409.
Ручной запуск не сдвигает расписание. Выключение не отменяет уже начатый job.
Ошибки scheduler не блокируют outbox остальных mapping jobs.

Используется прежний durable job/pipeline: lease/fence, bounded retry, PG staging,
vector cache, Qdrant count/alias/rollback/resume и отдельный deadline до 24 часов.
Рестарт не удаляет staging и не запускает второй конфликтующий импорт.
GUI восстанавливает прогресс из SQL; фоновый polling не перезаписывает введённые
несохранённые поля. Для нового source сначала сохранить OFF, проверить, затем
включить. HTTP probe привязан к config revision и HMAC identity credentials.
Для включения требуется probe не старше часа. Последующие ежедневные загрузки
разрешены для ранее подтверждённого неизменного источника и вновь проверяют весь
XML/JSON; успех обновляет timestamp probe. При ротации credentials нужен новый probe.
Ручной пользовательский HTTP-запуск использует такое же подтверждение неизменного
источника, как scheduler. Для V3 новая миграция не нужна: cooldown
хранится в существующей `service_activity`, остальные таблицы 0001–0003 сохраняются.

## Источник HTTP: готовая архитектура, реальный контракт ожидается

Локальный `data/*.xml` полностью поддерживается. HTTP адаптер использует тот же
ParsedCatalog и SynchronizeCatalogUseCase. V4 добавляет GET **полного JSON**
наряду с XML. Нет заявлений о поддержке OData, SMB, delta/cursor,
пагинации, ZIP, 304/ETag. Без утверждённого контракта
`ONEC_CATALOG_CONTRACT_CONFIRMED=false`: «Ожидается контракт», публикация запрещена.

Открытые поля GUI: source_type, url, GET, auth_type none/basic/bearer,
credential_ref=onec_catalog, expected_format=json|xml, export_mode=full, timeout_seconds,
max_bytes, completion_header/value. Параметры заголовка завершённости берутся **из
согласованного контракта**. Предложен `X-Catalog-Complete: true`, подтверждение 1С ожидается.
Полная выгрузка должна иметь достоверный признак завершённости; корректный XML без
такого подтверждения не даёт права удалять отсутствующие позиции.

Server-only Pydantic Settings: `ONEC_CATALOG_ALLOWED_HOSTS`,
`ONEC_CATALOG_ALLOWED_NETWORKS`, `ONEC_CATALOG_ALLOWED_PORTS`, TLS по умолчанию,
`ONEC_CATALOG_ALLOW_HTTP=false`. Для private IP нужны явные сети/IP. Проверяются
все DNS-ответы, соединение закреплено за проверенным IP с исходным Host/TLS SNI.
Loopback/link-local/metadata/multicast/unspecified запрещены. Redirect не следует.
URL не допускает userinfo/query/fragment. Basic/Bearer через HTTP запрещены.
Не отключать проверку TLS: доверенный CA настраивается инфраструктурой.

Реальные username/password/token — только `ONEC_CATALOG_USERNAME`,
`ONEC_CATALOG_PASSWORD`, `ONEC_CATALOG_TOKEN` в защищённом `.env`/server environment;
GUI принимает только reference и отвечает secret_configured. Значения не входят
в аудит, report или ответы. `.env.example` — каталог defaults/placeholders;
`.env` — реальные секреты и необходимые частные overrides, с правами 600.

Файл скачивается потоково в собственный `catalog_staging` volume (/app/staging,
0700/0600), не в read-only data. Проверяются таймаут, размер, Content-Length,
HTTP 200, content-type, отсутствие partial/range/compression, completion marker,
схема XML/XXE либо bounded JSON/типы/дубли. Неудача удаляет только временную загрузку, сохраняет рабочий
каталог/alias. Старая незавершённая PG/Qdrant staging-версия не удаляется этим cleanup.

**Для live 1С ожидаются:** completion marker, read-only сервисная учётная запись,
безопасный транспорт и авторизованный сетевой доступ из api/worker. URL/JSON уже
предоставлены владельцем. До подтверждения — `blocked_by_external_1c`, mock зелёным
live не объявляется. `ONEC_CATALOG_CONTRACT_CONFIRMED=false` сохраняется.

V4 добавляет registry реальный GUID → внутренний UUID. Однозначный прежний код
сохраняет внутренний UUID, внешняя ссылка попадает только в `onec_guid`.
Новая позиция получает внутренний UUID, равный её настоящему GUID; registry
сохраняет его при смене кода. Исторические code conflicts не используются для
миграции по коду. Старые jobs/approved snapshots не меняются. Embedding cache
зависит от нормализованного input и model identity, а не от GUID/кода/active.

## События 1С (V4)

На главной и в настройках — реальные received/pending/applied/failed и последнее
применение. Оператор дополнительно видит последние 50 технических доставок.
Адрес webhook доступен для копирования, server-only ключ не отображается.
Формат JSON/XML, расписание и источник редактирует только оператор.

`catalog_events` — durable inbox и dedup, независимый от часового TTL документов.
После SQL commit API отвечает 202 без ожидания внешних систем. Worker объединяет
события за 20 секунд (15–30 настраивается), создаёт один catalog_events job/outbox,
читает карточки через batch по 50 GUID, повторяет чтение до публикации. Delete
с актуальной доступной карточкой считается устаревшим: используется текущая карточка.
Missing upsert требует retry/full reconcile, а не удаления. Physical delete хранит tombstone.
Применение событий и SQL active pointer фиксируются одной транзакцией после alias
switch; неудачный commit откатывает alias, applied не выставляется. Crash после
успешной активации повторно завершает job без повторного применения.

Full sync и webhook используют общий lock. Во время full события накапливаются;
после него batch читает текущие данные. Due daily full ожидает активный event job,
не засчитывая его вместо полной выгрузки. Burst/backlog включает контрольную полную
сверку; заполненный inbox даёт 503/Retry-After. Ошибки сохраняют прежний каталог,
повторяются bounded попытками job и последующим backoff до восстановления.
Applied event IDs хранятся постоянно для дедупликации; карточки в inbox не копируются.
При снятии code conflict полная сверка восстанавливает ранее карантинированные
карточки; delta не угадывает отсутствующие метаданные исключённых карточек.

## API управления и отчёты

Точная схема входа находится в `/api/openapi.json`. Настройки, probe, общая история
и аудит требуют OperatorSession либо прежний CatalogKey; operator cookie writes —
X-Operator-CSRF + Origin. Пользовательский refresh требует обычный Owner и web CSRF
для cookie; выдача ticket дополнительно требует доверенный gateway proof.

| Метод и путь `/api/v1` | Назначение |
|---|---|
| GET/POST/DELETE `/operator/session` | Роль / одноразовый вход / отзыв |
| POST `/operator/tickets` | Одноразовый код только через доверенный loopback gateway |
| GET/PUT `/catalog/sync-settings` | Открытые настройки, revision, next_run, probe |
| POST `/catalog/connection-test` | Настоящая проверка без публикации |
| GET `/catalog/refresh-status` | Разрешение запуска, cooldown и безопасный общий прогресс |
| POST `/catalog/refresh` | Сохранённый разрешённый источник, общий интервал 60 секунд |
| GET `/catalog/refreshes/{job_id}` | Статус своего пользовательского запуска |
| POST `/catalog/imports` | Административный импорт; без тела прежний local XML |
| GET `/catalog/imports/{job_id}` | Прежний статус владельца + download/metrics |
| GET `/catalog/import-history` | offset/limit/state/source_type |
| GET `/catalog/import-history/{run_id}/report.json` | JSON отчёт, application/json |
| GET `/catalog/import-history/{run_id}/report.xlsx` | Excel, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet |
| GET `/operator/audit` | Безопасные административные события, пагинация |
| GET `/operator/jobs` | Общие метаданные, без чужих документов |
| GET `/dashboard/jobs` | Свои задания, offset/limit/state/order |
| GET `/dashboard/summary` | Область/KPI/health/catalog/read-only schedule |
| GET `/integrations/status` | extracting probe, stored 1С probe, Telegram inbound |

Report содержит run/job ID, source_type/initiator, UTC timestamps, stage/state,
hash/version, added/changed/removed/unchanged/quarantine, calculated/reused vectors,
processed/total, ошибки и фактические события. Не содержит XML/Caption/credentials.
`counts_complete=false` означает восстановление прежнего staging без полного набора
новых счётчиков: UI показывает «Частичный отчёт», не объявляет их точными.
История и аудит живут 90 дней (`CATALOG_AUDIT_RETENTION_DAYS`), независимо от TTL job.
Формула Excel не исполняется: текстовые поля записываются literal.

extracting проверяется по действительному OpenAPI и защищённому несуществующему
UUID без чужих документов, с cache 15 секунд. Различаются недоступность, отсутствие
ключа и отклонённый ключ. Telegram — **входящий клиент**: наличие named key и время
последнего успешного авторизованного 2xx запроса; само наличие ключа не зелёное.
Статические CSS/JS не расходуют API rate limit; API и страницы лимитируются как раньше.
