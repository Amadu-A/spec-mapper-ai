<!-- docs/operations.md -->
# Эксплуатация и приёмка

## Подготовка

Сервер должен иметь Docker/Compose 2.24+, `uv`, доступ к PyPI и image registry для
первой сборки. Shared стек предварительно запущен, существующая `ai-shared` содержит
RabbitMQ и `shared-embedding`. Скрипт ничего не устанавливает в systemd и не выбирает GPU.
RabbitMQ обнаруживается по Compose label service=rabbitmq и участию в ai-shared;
неоднозначное обнаружение останавливает запуск. Bootstrap читает существующие admin credentials RabbitMQ из Docker deployment в память
Pydantic, передаёт в Management API salted password_hash; пароль проекта не попадает
в argv/docker exec/журналы. Admin credentials в .env проекта не сохраняются.
Автоподготовка ограничена namespace
`spec_mapper`; существующий чужой пользователь/vhost или неверный пароль не перезаписываются.

Положите XML в data/ и настройте открытые параметры в .env.example. В .env храните
только реальные секреты. Upstream контейнер должен быть доступен по фактическому DNS:
значение EXTRACTING_BASE_URL в примере нужно сверить с вашим сервером.
Не требуется и не создаётся секрет shared-embedding.

XML игнорируется Git: `git pull` его не доставляет. Перед первым production-запуском
передайте файл с Windows (в отдельной PowerShell-сессии):

```powershell
ssh main@192.168.55.3 "mkdir -p ~/projects/spec-mapper-ai/data"
scp ".\data\Номенклатура_вся.xml" main@192.168.55.3:~/projects/spec-mapper-ai/data/
```

```bash
bash scripts/up.sh
```

Обычный запуск не делает down, не удаляет volumes и не останавливает shared сервисы.
Первый импорт имеет отдельный deadline 24 часа (`CATALOG_IMPORT_TIMEOUT_SECONDS`)
и сохраняет offset пачек для продолжения staging после отказа. Часовой TTL mapping
не прерывает каталог. Worker журнал остаётся
видимым. Ошибка импорта останавливает up.sh даже при ранее существовавшем каталоге.
Старый индекс при этом остаётся доступным. Недоступные shared зависимости дают not ready.

## Обновление и проверки

После того как владелец проверил и отправил свои изменения:

```bash
git pull --ff-only origin main
bash scripts/test.sh
bash scripts/up.sh
bash scripts/test.sh --live
```

`test.sh` и `ops/check-quality.ps1` показывают логи. Первый браузерный запуск скачивает
Chromium; на минимальном Linux системные библиотеки устанавливаются отдельно:

```bash
uv run playwright install --with-deps chromium
```

`--live` запускает tests image в private+ai-shared без публикации DB/Qdrant портов.
Проверяет настоящий PostgreSQL, shared embedding, staging Qdrant/alias и RabbitMQ
publisher confirm/nack/redelivery. Использует отдельные smoke ресурсы и удаляет
только их. Не подменяет live проверки тестами с fake AI/broker.

Live-проверка контракта извлечения требует явного UUID готового API-задания:

```bash
docker compose --env-file .env.example --env-file .env run --build --rm --no-deps tests /app/.venv/bin/pytest -m live --source-job-id <UUID> -q
```

Без UUID только upstream-тест помечается skipped. HTTP-цепочка onec → Top-5 →
поиск → выбор/null → approve → approved.json требует API_KEYS с onec и telegram
и готового каталога. Удаляются только собственные smoke-задания.

Для ручной повторной синхронизации, без пересчёта неизменившихся векторов:

```bash
docker compose --env-file .env.example --env-file .env exec -T api /app/.venv/bin/python -m src.cli import-catalog --wait
```

Daily sync выключен. Для включения задайте `CATALOG_DAILY_SYNC=true` в открытой
конфигурации или окружении запуска и повторите `up.sh`. Источником остаётся локальный
XML; подключение реальной 1С ещё требует её API-контракта.

## Ручная приёмка

1. Откройте `http://<сервер>:8087/`: интерфейс по концепту renders, без входа/регистрации.
   В справочнике видны реальные count, дата, fingerprint и карантин трёх кодов.
2. Укажите UUID извлечения, созданного **тем же API-ключом**, который задан серверу.
   Должны смениться этапы ожидания, загрузки и сопоставления. Итог сохраняет все
   строки и дубли, исходные Caption/Units/Quantity/Pages/UserComment1.
3. Откройте «Выбрать»: видны Top-K, код, единица, score, причины конфликтов. Найдите
   другую позицию по коду/артикулу, сохраните и перезагрузите страницу. Выбор остаётся.
4. Подтвердите «нет подходящей позиции». Проверьте фильтры, поиск, пагинацию и totals.
   В двух вкладках проверьте 409 при сохранении со старой revision.
5. Пока есть спорные строки, утверждение недоступно. Примите решения, нажмите
   «Утвердить результат»: появляются финальный JSON и время утверждения; выбор
   заблокирован и после reload. Скачайте approved.json. Также проверьте
   source.json, предварительные result.json и Excel. Оригинальная пятёрка неизменна, включая
   Quantity строкой и UserComment1. Настоящий GUID 1С пуст, внутренний UUID явно отдельный.
   При ручном поиске вне Top-K отсутствующая оценка остаётся пустой.
6. В приватном окне не должны быть видны задания первой сессии. Ключ upstream
   отсутствует в HTML и запросах браузера. Неавторизованный импорт каталога запрещён.
7. Во время обработки перезапустите **только worker этого проекта**, повторите доставку
   и убедитесь, что задача возобновляется без копий. Не перезапускайте общий RabbitMQ.
8. Через час после terminal состояние должно стать недоступным; ручной выбор не
   продлевает TTL. Каталог, XML и volumes при очистке заданий сохраняются.
9. Для приёмки качества сравните Top-K на реальных размеченных спецификациях:
   конфликтующие DN/PN/единицы не должны становиться автоматическим соответствием.

## Диагностика и откат

```bash
docker compose --env-file .env.example --env-file .env ps
docker compose --env-file .env.example --env-file .env logs --tail=100 api worker migrate
curl --fail http://127.0.0.1:8087/health/live
curl --fail http://127.0.0.1:8087/health/ready
curl --fail http://127.0.0.1:8087/api/openapi.json
```

`/health/live` — процесс; `/health/ready` проверяет реальные подключения и consumer.
Статус каталога показывает last_error отдельно от working snapshot. Повреждённый XML,
count mismatch или несовместимая embedding identity не подменяют каталог пустым.
Устраните источник ошибки и повторите импорт. При отказе DB commit alias автоматически
возвращается; при аварии между системами поиск всё равно использует concrete DB snapshot.
Не удаляйте PostgreSQL/Qdrant volumes для восстановления и не применяйте старые
миграции из examples/legacy к новой БД.

Старые версии сохраняются для ссылок заданий и отката. Worker раз в час удаляет
зарегистрированные snapshots старше часа, если это не active/previous и на них не
ссылается ни одно живое задание. Сборка версий использует тот же advisory lock.
Активный каталог, XML и embedding-кэш этим обслуживанием не удаляются.
Резервируйте PostgreSQL и Qdrant как данные этого проекта, независимо от shared stack.

## Сеть и доступ с другого компьютера

APP_BIND=0.0.0.0, APP_PORT=8087, APP_PUBLIC_BASE_URL=http://192.168.55.3:8087.
Обычные параметры — в .env.example/окружении команды; секреты — только в .env.
Web/API публикуют один порт; 8084 занят document-intake. Перед запуском:

```bash
ss -ltnp 'sport = :8087'
docker ps --format '{{.Names}} {{.Ports}}'
docker network inspect ai-shared
sudo ufw status verbose
sudo nft list ruleset
```

Firewall в ходе разработки не изменяется. Если политика требует разрешения,
оператор после проверки существующих правил может добавить LAN-правило UFW:
`sudo ufw allow from 192.168.55.0/24 to any port 8087 proto tcp`.
Docker published ports могут обходить UFW: проверить DOCKER-USER/nftables, маршрутизатор
и отсутствие доступа из недоверенных сетей. Не публиковать 5432/6333/5672 для этого
проекта, не менять чужие правила. Для интернета отдельно настроить HTTPS reverse
proxy, public URL, COOKIE_SECURE=true и политику доступа.

С Windows после успешного up.sh:

```powershell
Test-NetConnection 192.168.55.3 -Port 8087
Invoke-RestMethod http://192.168.55.3:8087/health/ready
Start-Process http://192.168.55.3:8087/
```

Секреты оператор читает локально в защищённой .env, не через команды, печатающие
credentials в общий журнал. Ключи onec/telegram передаются соответствующим
разработчикам защищённым каналом; upstream key берётся из конфигурации extracting.
Если пароль существующего RabbitMQ user не совпадает, bootstrap останавливается:
восстановить согласованное значение RABBITMQ_PASSWORD, не сбрасывать shared пароль.

## Главная и синхронизация V2

Обновление использует прежний `bash scripts/up.sh`, порт 8087 и существующие volumes.
Перед обновлением дождаться текущего импорта; additive 0003 не трогает снимки и approvals.
Новый собственный volume catalog_staging нужен для HTTP загрузок; data остаётся read-only.
Bootstrap env-флаг применяется один раз; далее расписание хранится в PostgreSQL.
Не выполнять down -v, prune или downgrade ради rollback интерфейса.
[Настройки и доступ оператора](DASHBOARD_AND_SYNC.md),
[проверка, deploy и rollback](RELEASE_V2.md).
