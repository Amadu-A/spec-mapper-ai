<!-- AGENTS.md -->
# Spec Mapper AI — обязательные правила разработки

## 0. Назначение и приоритет

Репозиторий: `Amadu-A/spec-mapper-ai`. Создаётся законченный локально развёртываемый сервис сопоставления позиций спецификаций с номенклатурой 1С. Источник спецификаций — `Amadu-A/spec-extracting-ai`, источник справочника на первом этапе — XML из `data/`. В будущем — ежедневная синхронизация с 1С через отдельный адаптер. Никакой регистрации, обучения модели, пользовательских профилей, админ-панели или облачных AI API.

Приоритет: **явное решение владельца → фактический текущий код/контракты spec-mapper-ai → актуальный `Amadu-A/shared_infrasktructure/docs` → заимствуемые алгоритмы `Amadu-A/fsnb-fastapi-app` → defaults библиотек**. Приложенный исторический `AGENTS.md` относится к `document-intake-ai`: применять переносимые правила (безопасность, lifecycle, тесты, инфраструктура), но не переносить схемы писем/OCR/разделение страниц.

Перед изменениями проверить текущие локальные `git status`, branch/HEAD/diff и remote; прочитать полные затрагиваемые файлы, тесты, договорённости и документацию. Читать `docs/LLM_CONTEXT.md`, а по профилю задачи: `ENGINEERING_GUIDELINES.md`, `FRONTEND_GUIDELINES.md` полностью для UI, `INFRASTRUCTURE_INSTRUCTIONS.md`, `services.yaml` для shared. Не выполнять commit/push, force push, destructive reset или изменения shared ресурсов без отдельной команды владельца. Не использовать `git add .`.

## 1. Вход и взаимодействие с извлечением

1. Входной идентификатор — `spec-extracting-ai` **`job_id` (UUID)**, а не cookie-сессия другой системы. Серверный HTTP-клиент работает с API-ключом из собственного `.env`, `X-API-Key`; этот ключ не отправляется в браузер. Правило owner/ключа upstream сохраняется.
2. Опрашивать `GET /api/v1/extractions/{job_id}`. `queued`/`running` — продолжать ограниченный polling; `completed`/`completed_with_warnings` — однократно получить `GET /api/v1/extractions/{job_id}/result.json`; `failed`, `cancelled`, 404, 410, timeout и ошибки соединения — явные диагностируемые состояния. Загрузка JSON должна завершиться ДО TTL upstream; не считать snapshot/partial или SSE `text_delta` готовым экспортом.
3. Поддерживать legacy v1 с `items` и пятью строками Caption/Units/Quantity/UserComment1/Pages и v2 с обязательным `file_name` и всеми восемью строковыми полями (добавлены Brand/Article/Note). Сохранить порядок, дубликаты и значения **дословно**. Не дописывать новые поля в сохранённый legacy source; defaults допустимы только в derived представлениях и новых экспортах. Старые approved snapshots неизменны.
4. Сопоставления хранить отдельно, связав с `source_job_id`, владельцем, индексом позиции, revision/hash исходного JSON и версией справочника. Повторный submit с тем же идемпотентным ключом не создаёт копий; изменённый payload под тем же ключом даёт конфликт.
5. Основной интеграционный поток — готовый JSON от 1С/бота через `/mappings/import-json`, `X-API-Key`, `Idempotency-Key` и необязательный `X-Source-Job-ID`. Polling по UUID остаётся самостоятельным поддерживаемым сценарием. Это уточнение владельца из ТЗ завершения проекта.

## 2. Каталог 1С, XML и ежедневная синхронизация

1. До появления реального XML нельзя придумывать структуру выгрузки 1С. Контракт адаптера `CatalogSource` и полевая модель должны учитывать GUID/ссылку 1С, код, полное наименование, единицу измерения, артикул/марку/производителя (если присутствуют), принадлежность группе и активность/пометку удаления. Возможности конкретного формата подтвердить на реальном файле и XSD, если предоставлена.
2. Первый адаптер — `XmlCatalogSource` для `data/*.xml`. Потоковый безопасный парсинг (без XXE/внешних сущностей), ограничение объёма, проверка типов, кодировки, ссылок и дублирующихся GUID; ошибки не скрывать и не заменять пустым успешным справочником.
3. Номенклатуру и её текущую версию хранить в **собственной PostgreSQL**. `GUID` 1С — устойчивый внешний идентификатор, а не автогенерируемый номер строк при каждом импорте. Нельзя переприсваивать выбранные соответствия при обновлении справочника.
4. Второй будущий адаптер — `OneCCatalogSource` с тем же портом; включение только после утверждения API/авторизации/схемы изменений 1С. `SynchronizeCatalogUseCase` общий для XML и 1С. Предусмотреть ежедневный запуск с флагом отключения по умолчанию, журналом, `last_successful_sync`, защитой от параллельных запусков, retry и восстановлением после неудачи. Не добавлять n8n/отдельный orchestrator без необходимости.
5. Изменённые позиции переиндексируются, удалённые и неактивные исключаются из новых выборов. Индексация согласуется с обновлением PostgreSQL; индекс никогда не исчезает в ходе успешного пользовательского поиска. При полной перестройке — staging collection, проверка counts и атомарный alias switch, rollback на предыдущий индекс.
6. Файлы в `data/` — локальные входные справочники, не временные задания; при удалении результата через TTL **не удалять** каталог, persistent PostgreSQL/Qdrant и их volumes. Реальные выгрузки номенклатуры 1С по умолчанию не коммитить.

## 3. Embedding, Qdrant, reranking

1. Использовать существующий `shared-embedding`: `http://shared-embedding:8000/v1`, модель `shared-embedding`, внешняя Docker сеть `ai-shared`. Не загружать локально Giga/SentenceTransformer в контейнер приложения и не создавать новый shared model runtime.
2. Model alias не гарантирует неизменность векторов. Проверять реальный endpoint, размерность, revision/identity модели и preprocessing. Хранить fingerprint индекса; при несовместимости создавать новую коллекцию, не смешивать старые и новые векторы.
3. Qdrant по `services.yaml` — **project-specific** по умолчанию, с собственными volume и закрытым network. Если предполагается использовать общий чужой Qdrant, требуется отдельное согласование владельца и проверка изоляции.
4. Поиск двухэтапный: (а) нормализация исходного `Caption` без потери характеристик; поиск Top-K в Qdrant; (б) повторное ранжирование с учётом единиц, марки/производителя, DN/PN, мощности, диапазонов, артикулов и других извлечённых подтверждённых признаков. Не объявлять произвольный cosine-score вероятностью соответствия.
5. Исходный `fsnb-fastapi-app` содержит vector Top-K, но не подтверждённый отдельный cross-encoder reranker; его нельзя представлять готовой моделью reranking. Статистические/детерминированные правила — реальный baseline. Новый ML-reranker — только при отдельном утверждении модели, endpoint и измеренного выигрыша качества, без обучения.
6. Автоматическое соответствие не должно маскировать несовместимую единицу или противоречащие технические параметры. Такие строки переводить в «требует проверки»/«не найдено». Пороги калибровать по тестовому корпусу и фиксировать версию алгоритма.
7. Батчить embeddings и поиск, избегать N+1 запросов к PostgreSQL, ограничивать concurrency/retry; использовать асинхронные адаптеры либо ограниченный thread offload.

## 4. Таблица и ручной выбор

1. Светлый инженерный интерфейс: статус извлечения и индекса, карточки количества позиций/сопоставлений/неопределённых/не найденных; главная часть — таблица с исходным `Caption`, `Units`, `Quantity`, `Pages`, исходным шифром проекта `UserComment1`, выбранным названием/кодом/GUID 1С, оценкой и статусом.
2. Для каждой строки — кнопка перевыбора: показать Top-K кандидатов с кодом, названием, единицей и честным score; поиск по коду/названию/артикулу (PostgreSQL), выбор любой разрешённой позиции из действующего справочника, вариант «нет подходящего». Никаких полей `gold`, `negative`, `training_run` или прав редактора из старого `train`.
3. Изменение выбора сохраняется в собственной сессии/БД атомарно и отражается при reload/reconnect. Сохранение выбора не обучает модель, не меняет каталог, не перезапускает индексацию.
4. Фильтры «все / подобрано / проверка / не найдено / вручную», поиск, пагинация, итоговые totals и выгрузка итогового XLSX/JSON с отдельными 1С-полями и исходной пятёркой. Оригинальные пять строк должны оставаться доступными без изменений.
5. Web и API используют одинаковые application use-cases. Jinja2 + BEM CSS + JS modules: semantic HTML, один CSS entrypoint, блоковые CSS, `data-*` JS hooks, no inline `onclick`, safe textContent, keyboard/focus/accessibility, frontend tests.
6. Автоматический маппинг завершается даже при низкой оценке или пустых кандидатах; ручная проверка не требуется для технического завершения задания, но неподтверждённые соответствия маркируются честно. Не объявлять «проверено человеком» без фактического подтверждения.

## 5. Состояние, TTL и безопасность

1. API и worker — FastAPI-монолит с PostgreSQL, RabbitMQ shared (`rabbitmq:5672`) через отдельный user/vhost/queue, private Qdrant и собственную сеть. Shared RabbitMQ/embedding НЕ перезапускать/не изменять. Запуск из существующего Docker хоста через `ai-shared`.
2. Задания durable, с состояниями polling → fetching → matching → ready_for_review/complete или явной failure. Обязательны idempotency, at-least-once deduplication, bounded retries, lease/fencing, recover после restart, проверяемые `health/live` и `health/ready`.
3. Исходный JSON, ревью, утверждённый результат и отчёты живут 1 час после технического завершения; незавершённое mapping-задание — до часа с создания. Approval не продлевает TTL. Импорт каталога имеет отдельный bounded deadline `CATALOG_IMPORT_TIMEOUT_SECONDS` (24 часа по умолчанию) и checkpoints staging. API сразу запрещает доступ после TTL; очистка идемпотентна. XML-каталоги, активный справочник и Qdrant-индекс TTL заданий не затрагивает.
4. Web без регистрации и пароля, но с изолированной HttpOnly/SameSite cookie-сессией, CSRF+Origin для cookie-writes. Отдельные API-ключи интеграций, owner isolation; не отдавать чужой job или данные. Не хранить секрет upstream API в браузере. Ограничить LAN/VPN firewall, загрузку, XML payload, размеры результатов, rate limits.
5. HTTP-адрес web/API — отдельный редактируемый порт после проверки занятости. Не занимать 8083 (`spec-extracting-ai`) и не перехватывать порт другого проекта. Внешне открывать только web/API; DB/Qdrant/RabbitMQ не публиковать.
6. Публичный JSON и логи не содержат секреты, чужие документы, credential, полные confidential captions; логировать job IDs, timings, counts, errors безопасно. Ротация логов и retention обязательны.

## 6. Архитектура и конфигурация

Логические границы: `domain` (чистые модели/правила) ← `application` (use-cases, ports, UoW) ← `infrastructure` (HTTP source, XML, embedding, Qdrant, PostgreSQL, broker), `presentation/http` и `web` — delivery, `bootstrap.py` — composition root. SQLAlchemy в infrastructure/data access; бизнес-логика не в роутерах и не в JS. Малые Ports & Adapters, DI, PEP8/type hints, русские docstrings, относительный путь файла первым допустимым комментарием.

Единые Pydantic Settings. `.env.example` содержит каталог безопасных настроек и placeholders, `.env` только реальные секреты/частные overrides; приоритет явно проверять в Python и Compose (environment > .env > defaults). `uv`, закреплённый `uv.lock`, Alembic, Ruff и pytest. Стек создаёт только собственные ресурсы. `bash scripts/up.sh` после clone безопасно подготавливает project secrets, изолированного пользователя/vhost RabbitMQ (без изменения чужих credentials), применяет миграции, запускает только собственные API/worker/PostgreSQL/Qdrant, проверяет real health и показывает безопасные адреса. Повторный запуск идемпотентен, не удаляет volumes. Недоступный shared endpoint — not ready с диагностикой, не silent fallback.

## 7. API-контракты

Основные реализованные внешние маршруты:

- `POST /api/v1/mappings` — `source_job_id` UUID (обязательно), `Idempotency-Key` и API-ключ. Создать задачу ожидания upstream.
- `GET /api/v1/mappings/{job_id}` — статус, counts, stage, error_code, TTL и links.
- `GET /api/v1/mappings/{job_id}/result` — итоговые соответствия; только готовое/явно маркированное состояние.
- `GET /api/v1/mappings/{job_id}/candidates?row_index=N` — Top-K для строки.
- `GET /api/v1/catalog/items?q=...&limit=...` — поиск номенклатуры по коду, названию и артикулу.
- `PUT /api/v1/mappings/{job_id}/rows/{row_index}/selection` — ручное переопределение `catalog_item_guid` либо `null`.
- `DELETE /api/v1/mappings/{job_id}/rows/{row_index}/selection?revision=N` — очистить только соответствие с сохранением исходной строки и Top-K.
- `POST /api/v1/mappings/{job_id}/approve` — явно подтвердить все текущие выборы/null после завершённого подбора одной атомарной операцией.
- `GET /api/v1/mappings/{job_id}/result.xlsx` — итоговый Excel.
- `POST /api/v1/catalog/imports` — инициировать проверенную переиндексацию XML из разрешённого `data/` (только привилегированный project credential).
- `GET /api/v1/catalog/status` — число записей, fingerprint, состояние индекса и дата последней синхронизации.
- `POST /api/v1/catalog/refresh` — пользовательское обновление сохранённого подтверждённого HTTP источника (local XML только оператору), с общим минутным SQL cooldown.

Точные схемы OpenAPI и совместимость закреплять контрактными тестами. Не объявлять
неизвестную live-интеграцию HTTP 1С настроенной по одному URL или наличию ключа.

## 8. Тесты и завершение работы

Отдельные проверяемые группы: offline unit/architecture; API/security; XML integration; PostgreSQL/Qdrant live; shared embedding live; spec extraction contract live; RabbitMQ/retry/TTL; браузерные E2E. Проверять: XML с отсутствующими ссылками/удалениями/дубликатами; GUID стабильность; несовместимые единицы; дубли спецификации; батч Top-K; поиск по коду/названию; manual select; повторный polling; чужой owner; upstream TTL/404; reindex rollback; Qdrant alias swap; восстановление после restart; Excel с сохранением пяти оригинальных полей. Не выдавать mocks за live-pass.

Quality gates: Windows `ops/check-quality.ps1` (Ruff, pytest, architecture, JS/E2E если применимо, `git diff --check`), Linux `bash scripts/test.sh`, `bash scripts/up.sh` и реальные runtime проверки. В отчёте: файлы, состояния, тесты, ограничения, точные команды проверки, явный список `git add <paths>`, команды commit/push только для исполнения владельцем. Не изменять shared-stack и не делать destructive actions.

Definition of Done: после clone и одного `bash scripts/up.sh` сервис запускается на уже подготовленном сервере; импортирует реальный XML в PostgreSQL/Qdrant через shared embedding; по `source_job_id` получает готовый JSON; возвращает честное Top-K сопоставление всех строк, предоставляет поиск/перевыбор/сохранение и корректные экспорты; задания устойчивы к restart и соблюдают TTL; соседи по shared-инфраструктуре не затронуты.

## 9. Главная и управление каталогом (дополнение V2)

Главная следует предоставленному `renders/render_v2.png`; прежняя таблица — `/mapping`,
старые deep links сохраняют owner. Оператор без регистрации получает короткую сессию
через одноразовый ticket в доверенном SSH-входе или резервный CLI-код; API-ключи в браузер не выдаются.
Оператор видит только общую мета-статистику чужих jobs. После bootstrap расписание
живёт в PostgreSQL: OFF/15:00 UTC по умолчанию, один слот дата+источник, безопасный catch-up.
Локальный XML и HTTP full XML используют один pipeline; неизвестный контракт 1С
не разрешает HTTP publication. Новые миграции additive, staging/volumes не сбрасываются.
Точный workflow, безопасность, метрики и API: [DASHBOARD_AND_SYNC](docs/DASHBOARD_AND_SYNC.md).

## 10. Упрощённый операторский вход и утверждение V3

Кнопка выдачи ticket работает только через loopback сервера 127.0.0.1:8088
(`OPERATOR_PORT`) и gateway в отдельной сети operator_access без ai-shared/Docker socket. Подписанный
15-секундный proof использует существующий SESSION_SECRET; нужны обычный web owner
и CSRF. Host=localhost сам по себе не является авторизацией. Публичный 8087 сохранён.
В SSH-инструкции пользователь вводит собственный логин; после доверенного входа
инструкция скрывается. Старый туннель на 8087 поддерживает CLI-ticket fallback.

Только оператор меняет auto-update, время и источник. GUI в обоих режимах использует
пользовательский refresh с глобальным 60-секундным SQL cooldown, replay и revision
CAS; обычному owner разрешён только сохранённый проверенный HTTP источник, оператору
также local XML. Настройки URL/секреты не принимаются из public refresh payload.
Ручной запуск не сдвигает scheduler. Административный imports/CLI сохранён без нового
пользовательского лимита для bootstrap/совместимости. Новые секреты и миграции не нужны.

Готовое сопоставление утверждается целиком одним явным действием пользователя:
текущие review+selection становятся manual, review+null — not_found, matched
сохраняются; все строки confirmed/manually_reviewed. Rows, метрики и immutable
snapshot фиксируются атомарно, source/TTL не меняются. Не подбирать новый Top-1
вместо null. Крестик удаляет только selection через DELETE с owner/CSRF/revision,
не исходную строку/кандидатов; после approved изменения запрещены. SVG favicon
продолжает существующий логотип. Offline/mocked и live результаты разделять.

## 11. JSON 1С, webhook и миграция GUID (V4)

HTTP JSON GET/export и POST/items/batch используют общий защищённый transport и
ParsedCatalog pipeline наряду с XML. Полный JSON требует completion marker,
строгих типов, bounded streaming и shrink guard. Реальный адрес из ТЗ не означает
авторизованный production транспорт: HTTP Basic и пустой пароль не разрешены.
ONEC_CATALOG_CONTRACT_CONFIRMED остаётся false до реального подтверждения.

Webhook POST /api/v1/catalog/events имеет отдельный server-only ключ,
durable PG inbox/dedup и существующий Rabbit job outbox/lease/fence. 202 только
после commit, changed payload того же event_id — 409. Batch coalesces за 20 секунд;
full sync и события используют общий lock/staging/alias. Applied и active SQL pointer
фиксируются одним commit. Missing upsert не считается delete. Physical delete
сохраняет tombstone; full сверка остаётся источником истины. Не клонировать индекс
отдельно на каждый GUID. Порты 8087/8088, owner/TTL/approved и shared stack сохраняются.

Реальный GUID хранится отдельно в onec_guid и уникальном registry. Однозначный
legacy code сохраняет internal UUID; новые карточки используют стабильный реальный
UUID как internal ID. Смена кода не создаёт новую карточку. Semantic fingerprint
зависит от model/preprocessing identity и реального input, не от GUID/кода/active.
Переиспользовать V3 cache только при совпавшем input. Explicit Brand/Article учитываются
в подборе/конфликтах; Note и file_name сейчас не являются embedding признаками.
Миграция 0004 additive; старые approvals и staging не сбрасывать.
