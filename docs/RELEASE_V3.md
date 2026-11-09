<!-- docs/RELEASE_V3.md -->
# Упрощение работы оператора и утверждения — V3

База: `bf9afa9f89b97c35f8c8434a39dca68c115dc064`, ветка `main`.
Перед изменениями локальный HEAD, Amadu-A/main и зеркало neo-term-it/main совпали;
рабочее дерево было чистым. Актуальные shared/docs сверены, reference не изменялся.

## Что изменилось

| Раньше | Теперь |
|---|---|
| Ticket требовал второй SSH-команды | Через доверенный SSH-вход есть кнопка получения кода; пользователь вводит показанный код |
| В подсказке был логин main | Логин вводится самим пользователем; после открытия доверенного входа SSH-подсказка скрыта |
| Ручной запуск был только оператору | Подтверждённый настроенный HTTP-источник обновляет любой обычный owner; общий SQL интервал 60 секунд |
| Нужно было подтвердить каждую спорную строку | Общее утверждение подтверждает все текущие выборы и null атомарно |
| Очистка только через окно перевыбора | Крестик вызывает DELETE соответствия, оставляет строку и подтверждённый null |
| Не было фавикона | Собственный SVG в стиле существующего логотипа, включая страницу API |

Операторские настройки, время UTC, включение/выключение расписания остаются закрыты.
Ручное обновление не сдвигает расписание. Локальный XML остаётся доступен оператору;
для обычного пользователя требуется проверенный HTTP-источник 1С. До получения
реального контракта 1С автоматического подключения или зелёного статуса не заявляется.

Общее утверждение сохраняет текущие предложения: нового кандидата вместо null не
выбирает. review с выбором становится manual, пустая review — not_found, matched
сохраняет автоматический статус. Все строки получают confirmed/manually_reviewed;
счётчики меняются в той же транзакции. Исходная пятёрка, порядок/дубликаты, revision,
TTL, owner isolation и неизменяемый approved.json сохранены. После approval удаление
или перевыбор запрещены. До него крестик показывает «Соответствие не выбрано»,
«Не найдено», «Подтверждено человеком».

## Доступ оператора

Публичный web/API остаётся на 8087. Код кнопкой выдаётся через новый серверный
loopback `127.0.0.1:8088` (OPERATOR_PORT). Прежний туннель на 8087 необходимо
заменить, если нужна кнопка. Один раз на своём Windows-компьютере:

```powershell
$sshLogin = Read-Host 'Ваш логин сервера'
ssh -N -L 18087:127.0.0.1:8088 "$sshLogin@192.168.55.3"
```

Оставить терминал открытым, открыть `http://localhost:18087/operator`, нажать
«Получить одноразовый код», скопировать показанный код в поле и начать сессию.
Второй терминал для operator-ticket не нужен. Код живёт 120 секунд, однократный;
роль — максимум 900 секунд. CLI остаётся резервным способом.

Host localhost сам по себе не доказывает SSH-доступ. Поэтому gateway из того же
image подключён только к отдельной project-сети operator_access с API, имеет
loopback-публикацию и подписывает 15-секундный proof на существующем SESSION_SECRET.
У него нет ai-shared, Docker socket и доступа к сетям PostgreSQL/Qdrant/RabbitMQ.
Он не выполняет shell-команду: API вызывает тот же серверный механизм выдачи кода,
что CLI. Новых секретов, пользователей и миграций нет. Сеть не internal: Docker не
публикует host-порт для контейнера, подключённого только к internal-сети.

Preflight проверяет оба порта и Docker Engine 28+ для корректной loopback-изоляции.
На сервере проверен Engine 29.8.1, порт 8088 свободен. Инфраструктура/daemon/firewall
автоматически не перенастраиваются. Подробности и первичный источник: [operations](operations.md).

## Новые контракты

Точные схемы — `/api/openapi.json`, описание — [API.md](API.md).
Добавлены POST `/api/v1/operator/tickets`, GET `/catalog/refresh-status`,
POST `/catalog/refresh`, GET `/catalog/refreshes/{job_id}` и DELETE selection?revision.
Cookie writes требуют прежний CSRF/Origin. 429 содержит Retry-After/error.retry_after.
Минутный лимит общий для owners/вкладок/replicas и не сбрасывается при reload/restart.
Повтор принятого public owner/key возвращает прежний job после изменения настроек;
новая операция со старой revision источника не enqueue. Клиент не передаёт URL,
credentials или source override. Legacy `/catalog/imports` сохранён отдельно для
административного bootstrap/recovery и не получает новый GUI cooldown.

## Проверки

- Baseline: 136 Python и 10 Node passed.
- Windows `ops/check-quality.ps1`: **163 passed**, 11 live deselected, **84%** coverage;
  Ruff/format, Node **13 passed**, diff check. Пять Chromium E2E включают прежний
  workflow, dashboard, gateway-кнопку, bulk approve/крестик/409/network и public
  refresh/cooldown/reload/другой owner. Внешние AI/1С в Windows browser tests явно fake.
- Linux Docker test/runtime build и Ruff/format успешны, offline **158 passed**.
- Реальные изолированные PostgreSQL/Qdrant/RabbitMQ, shared-embedding и HTTP API:
  **10 live passed, 1 skipped**. Пропуск — нет нового готового upstream source_job_id.
  Проверены gateway ticket/login/logout, обычный refresh через сетевой mock полного
  XML, общий cooldown/replay, отсутствие повторной векторизации, два PG enqueue
  экземпляра, миграционные и прежние mapper regression tests.
- Gateway дополнительно проверен через настоящий host `127.0.0.1:18089`, ready=200;
  в его сети во время runtime только тестовые API и gateway. Подмена Host/Forwarded
  на публичном API выдачу кода не разрешает.
- HTTP 1С проверен против явного mock-сервера, не реальной 1С. Новая полная
  векторизация production-справочника не выполнялась. Рабочий каталог не сбрасывался.
- ID/StartedAt production mapper/PG/Qdrant и shared RabbitMQ/embedding не изменились.
  Временные тестовые контейнеры/сети удалены, persistent volumes не тронуты.
  Повторная проверка 09.11.2026: production ready=200, каталог 62 052,
  версия d548a881-17a4-4d9a-9811-25bc6647a2a4 и чистый production HEAD bf9afa9 сохранены.

Логи внутренних прогонов: `.v3-baseline.log`, `.v3-quality-final.log`,
`.v3-live-final.log`, `.v3-gateway-network.log`, `.v3-remote-build.log`, `.v3-cleanup.log`.
Скриншоты: `test-results/browser-operator-gateway.png`, `browser-bulk-approval.png`,
`browser-bulk-approval-mobile.png`, `browser-favicon.png`.

## Проверка владельцем на Windows

```powershell
Set-Location C:\Users\i.mein\PycharmProjects\spec-mapper-ai
.\ops\check-quality.ps1
git diff --check
git status --short
```

Отдельные группы при необходимости:

```powershell
uv run --no-sync ruff check src tests alembic
uv run --no-sync ruff format --check src tests alembic
uv run --no-sync pytest -m 'not live and not e2e' -q
uv run --no-sync pytest -m e2e -q
node --test tests/frontend/*.test.js
```

## Linux: обновление владельцем

Убедиться, что checkout чист и текущий импорт завершён (проверка в operations).
При dirty checkout остановиться; не делать reset. Выполнять следующую команду
только после успеха предыдущей:

```bash
cd ~/projects/spec-mapper-ai
git status --short
PREVIOUS_HEAD=$(git rev-parse HEAD)
git pull --ff-only origin main
docker compose --env-file .env.example --env-file .env config --quiet
bash scripts/up.sh
curl --fail --show-error http://127.0.0.1:8087/health/ready
curl --fail --show-error http://127.0.0.1:8088/health/live
docker compose --env-file .env.example --env-file .env logs --tail=100 api worker operator-gateway
bash scripts/test.sh
```

up.sh проверяет и поднимает только свои сервисы, включая gateway, сохраняет volumes,
секреты и staging. SQL схема остаётся 0003. Для live regression доступна прежняя
`bash scripts/test.sh --live`; глобальные V3 management тесты намеренно пропускаются
вне специально изолированного mapper-v2-test-v3-* окружения.

## Ручная проверка

1. Войти через новый туннель: SSH-подсказки больше нет, код получается кнопкой и
   вводится; после входа поле/выданный код очищены. На публичном URL кнопки кода нет.
2. В обычном режиме расписание и время не редактируются. При настроенном HTTP 1С
   «Обновить сейчас» доступно; после запуска минутный отсчёт и disabled сохраняются
   после reload и во второй сессии. Без контракта кнопка честно недоступна.
3. Импортировать JSON со спорными строками: общее утверждение активно после подбора.
   До него крестик очищает только выбранную номенклатуру; источник остаётся в таблице.
   Статус — «Не найдено / Подтверждено человеком». Перевыбор и Top-5 работают.
4. Нажать «Утвердить результат» без открытия всех строк: все текущие выборы/null
   подтверждены, approved.json скачивается; исходные пять полей и Excel сохранены.
   После утверждения изменения закрыты. Проверить вкладочный фавикон и мобильный вид.

## Откат

Сначала выключить расписание, если возможно, дождаться активного импорта и сохранить
диагностику. Остановить только новый собственный gateway перед возвратом к bf9afa9:

```bash
docker compose --env-file .env.example --env-file .env stop operator-gateway
git switch --detach "$PREVIOUS_HEAD"
bash scripts/up.sh
```

Миграционного downgrade, удаления volumes, Qdrant reset и down -v не требуется.
После возврата к V3 сохранённые настройки снова используются. Код изменён только в Windows. Staging/commit, push и production deployment агент
самостоятельно не выполняет.

## Файлы, staging, commit и push владельцем

Всего 48 файлов:

- `.env.example`
- `AGENTS.md`
- `README.md`
- `docker-compose.yml`
- `docs/API.md`
- `docs/DASHBOARD_AND_SYNC.md`
- `docs/INTEGRATION_1C.md`
- `docs/LLM_CONTEXT.md`
- `docs/RELEASE_V3.md`
- `docs/TECHNICAL_SPECIFICATION.md`
- `docs/architecture.md`
- `docs/operations.md`
- `scripts/up.sh`
- `src/application/catalog_management.py`
- `src/application/mappings.py`
- `src/application/sync_ports.py`
- `src/cli.py`
- `src/config.py`
- `src/domain/models.py`
- `src/infrastructure/operator_access.py`
- `src/infrastructure/repositories.py`
- `src/infrastructure/sync_repository.py`
- `src/main.py`
- `src/operator_gateway.py`
- `src/presentation/dashboard_routes.py`
- `src/presentation/dashboard_schemas.py`
- `src/presentation/gateway_security.py`
- `src/presentation/routes.py`
- `src/web/static/css/blocks/table.css`
- `src/web/static/favicon.svg`
- `src/web/static/js/api.js`
- `src/web/static/js/mapping.js`
- `src/web/static/js/operator.js`
- `src/web/static/js/state.js`
- `src/web/static/js/sync-settings.js`
- `src/web/templates/api_docs.html`
- `src/web/templates/base.html`
- `src/web/templates/components/sync_panel.html`
- `src/web/templates/mapping.html`
- `src/web/templates/operator.html`
- `tests/frontend/approval.test.js`
- `tests/test_approval.py`
- `tests/test_browser_bulk_approval.py`
- `tests/test_browser_public_refresh.py`
- `tests/test_bulk_approval.py`
- `tests/test_operator_gateway.py`
- `tests/test_public_refresh.py`
- `tests/test_v3_live.py`

После успешных проверок выполните сами:

```powershell
git add -- `
  .env.example AGENTS.md README.md `
  docker-compose.yml docs/API.md docs/DASHBOARD_AND_SYNC.md `
  docs/INTEGRATION_1C.md docs/LLM_CONTEXT.md docs/RELEASE_V3.md `
  docs/TECHNICAL_SPECIFICATION.md docs/architecture.md docs/operations.md `
  scripts/up.sh src/application/catalog_management.py src/application/mappings.py `
  src/application/sync_ports.py src/cli.py src/config.py `
  src/domain/models.py src/infrastructure/operator_access.py src/infrastructure/repositories.py `
  src/infrastructure/sync_repository.py src/main.py src/operator_gateway.py `
  src/presentation/dashboard_routes.py src/presentation/dashboard_schemas.py src/presentation/gateway_security.py `
  src/presentation/routes.py src/web/static/css/blocks/table.css src/web/static/favicon.svg `
  src/web/static/js/api.js src/web/static/js/mapping.js src/web/static/js/operator.js `
  src/web/static/js/state.js src/web/static/js/sync-settings.js src/web/templates/api_docs.html `
  src/web/templates/base.html src/web/templates/components/sync_panel.html src/web/templates/mapping.html `
  src/web/templates/operator.html tests/frontend/approval.test.js tests/test_approval.py `
  tests/test_browser_bulk_approval.py tests/test_browser_public_refresh.py tests/test_bulk_approval.py `
  tests/test_operator_gateway.py tests/test_public_refresh.py tests/test_v3_live.py
git diff --cached --check
git diff --cached --stat
git commit -m "feat: упростить вход оператора и общее утверждение спецификаций"
git push git@github.com:Amadu-A/spec-mapper-ai.git main
git push git@github.com:neo-term-it/spec-mapper-ai.git main
```
