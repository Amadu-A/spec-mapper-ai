<!-- docs/IMPLEMENTATION_REPORT.md -->
# Отчёт завершения, 06.11.2026

Исходный HEAD: `85ac0b68a2e65bbca01faad0cd5b6739fac1e59f`. Изменения сделаны только
в Windows-проекте. Commit/push не выполнялись. Серверный checkout остался чистым
на прежнем HEAD; сервер использовался для тестов образа из локальных исходников.

## Результат

- Публикация web/API на `0.0.0.0:8087`, LAN URL `http://192.168.55.3:8087`.
  `/docs` работает с локальными ресурсами, OpenAPI содержит типизированные ответы
  результата, кандидатов, поиска, предварительного и утверждённого JSON.
- Основной вход: неизменённый JSON от 1С/бота; `X-Source-Job-ID` отдельно от пятёрки.
  Polling extracting по UUID также поддерживается и проверен на настоящем извлечении.
- `API_KEYS` разделяет onec/telegram, имя owner сохраняется при ротации значения.
  Прежний API_KEY сохраняет прежний owner. Пользовательской регистрации/JWT нет.
- Top-5, поиск всего каталога, ручной выбор/null, revision, сохранение и reload.
  Расширены запреты автоподбора при несовместимых температуре, модели и артикуле.
- Атомарное общее approve, миграция `0002_approvals`, неизменяемый approved.json.
  Спорные строки требуют явного решения; после утверждения правки запрещены.
  Source сохраняется дословно, ведущие нули кода не теряются, UUID/GUID различаются.
- Review/approval не продлевают часовой TTL. Каталог имеет отдельный deadline 24 часа,
  staging checkpoints и продолжение после restart. Повторный запуск восстанавливает
  failed import и проверяет существование реального индекса.
- `.env` записывается атомарно, на Linux 0600, с сохранением прежних секретов.
  Создаются отдельные onec/telegram ключи; upstream ключ не придумывается.
  Bootstrap RabbitMQ создаёт только выделенные user/vhost через password_hash,
  не передаёт пароль в argv, не заменяет существующий пароль и не меняет чужие права.
- Исправлена обнаруженная live-прогоном чувствительность identity к точному хешу float:
  metadata проверяется строго, контрольный вектор — с численным допуском. Закреплённый
  fingerprint/vector cache не меняется из-за небольшого GPU-шума.

## Подтверждённые проверки

| Проверка | Результат |
|---|---|
| Windows `ops/check-quality.ps1` | Ruff/format, 85 Python-тестов, 6 Node-тестов; покрытие 80% |
| Linux offline в отдельном контейнере | 84 passed, без внешней сети |
| Linux live, PostgreSQL/Qdrant/RabbitMQ/shared-embedding/extracting | 7 passed, без skipped |
| Настоящий XML | 65 493 записи, 3 436 пометок удаления, 6 строк карантина, 62 052 активные разрешённые |
| Полная векторизация | 62 052 metadata в PostgreSQL и 62 052 точки Qdrant, размерность 4096 |
| Restart во время полного импорта | Продолжен тот же staging, новая попытка после lease, без потери готовых пачек |
| Повторный запуск окончательного кода | Та же версия и коллекция, без повторной индексации позиций |
| Incremental sync с настоящими адаптерами | Неизменная позиция переиспользована, изменённая/новая векторизованы, удалённая исключена |
| Извлечение синтетического PDF | Настоящий extracting вернул две строки; mapper polling → review/null → approve → JSON прошёл |
| Прямой API-сценарий 1С | import/idempotency/Top-5/поиск/selection/null/revision/approve/export/owner isolation прошли |
| TTL в реальной PostgreSQL | На собственном тестовом job выставлена прошедшая дата: 410 для результатов, каталог сохранён |
| Chromium с Windows через LAN | Реальный worker и полный каталог: выбор/null, approval, reload, JSON, desktop/mobile, /docs |
| Offline Chromium | Дополнительно ошибка сети, конфликт двух вкладок, фильтры, поиск и Excel |
| Независимый read-only review | Существенных дефектов не найдено |
| Bash-скрипты | Синтаксис up.sh/test.sh проверен Linux bash; файлы используют LF |

Статистика удалений и карантина пересекается: одна конфликтующая запись уже помечена
удалённой. Технических дословных повторов в предоставленном файле — 0.
Исключены все записи кодов `00000079127`, `00-00003589`, `ЦБ-00000920`.
GUID 1С в файле отсутствует. SHA256 XML:
`1c4aae81231a4f975e90d67036b2b1a93ae8a08da10e7ffceafa54f88d569807`.

Небольшой synthetic справочник incremental-теста использовал отдельные SQL-схему
и Qdrant alias. Это реальные SQL/HTTP/embedding адаптеры, а не fake векторы.
Browser offline-тесты, напротив, явно подменяют AI/broker; эти результаты не смешиваются.

Подробные внутренние логи игнорируются Git: `.quality-final.log`, `.remote-import.log`,
`.remote-restart.log`, `.remote-repeat.log`, `.remote-final-tests.log`,
`.remote-polling.log`, `.lan-browser.log`, `.remote-cleanup.log`.

## Состояние сервера после тестов

Временные контейнеры с XML/PostgreSQL/Qdrant, временная сеть и тестовые RabbitMQ
user/vhost удалены. Порт 8087 снова свободен. Shared RabbitMQ/embedding и extracting
остались healthy с прежними датами запуска. Общие сервисы не перезапускались.
Production-стек mapper не развёртывался: это остаётся операторским запуском после push.
Синтетический PDF upstream очищается по штатному TTL извлечения.

Системные firewall-правила не изменялись. Их чтение через sudo недоступно без
интерактивной авторизации; точные диагностические команды приведены в operations.md.
Доступ с Windows к временному API на LAN 8087 фактически проверен.
Публикация в интернет/HTTPS reverse proxy в текущий LAN-прогон не входила.

## Действия владельца

1. Выполнить Windows quality, просмотреть diff, сделать commit и push в оба репозитория.
2. Передать XML в серверный `data/`: Git его не доставляет. Настроить существующий
   upstream ключ в серверном `.env`, если нужен polling. Обычные параметры уже описаны
   в `.env.example`; project secrets и named API keys создаёт up.sh.
3. На сервере: pull → `bash scripts/up.sh` → `bash scripts/test.sh --live`.
   Системные библиотеки Chromium при необходимости подготовить по operations.md.
4. Открыть LAN web, проверить выбор/null, reload, approval и финальный JSON; проверить
   собственные реальные спецификации на качество Top-5 и технические конфликты.

Продакшн credentials namespace `spec_mapper` не изменялись и не проверялись тестовым
namespace. Несогласованный старый пароль должен дать явную ошибку, не автоматический
сброс. Точность на размеченном промышленном корпусе не заявляется измеренной.
1С-кнопка/модальное окно/создание документа реализуются программистом 1С;
адаптер ежедневного получения справочника непосредственно из 1С ждёт её API-контракта.
Сейчас ежедневный источник — локальный XML, расписание выключено по умолчанию.

Контракты и примеры для интегратора: [INTEGRATION_1C.md](INTEGRATION_1C.md).
Команды эксплуатации/проверок и ручная приёмка: [operations.md](operations.md).

## Изменённые файлы

- `.env.example`
- `AGENTS.md`
- `README.md`
- `docker-compose.yml`
- `docs/API.md`
- `docs/TECHNICAL_SPECIFICATION.md`
- `docs/architecture.md`
- `docs/operations.md`
- `scripts/test.sh`
- `scripts/up.sh`
- `src/application/catalog.py`
- `src/application/mappings.py`
- `src/application/ports.py`
- `src/application/processing.py`
- `src/application/reports.py`
- `src/bootstrap.py`
- `src/cli.py`
- `src/config.py`
- `src/domain/matching.py`
- `src/domain/models.py`
- `src/infrastructure/broker.py`
- `src/infrastructure/database.py`
- `src/infrastructure/http_clients.py`
- `src/infrastructure/qdrant.py`
- `src/infrastructure/repositories.py`
- `src/infrastructure/xml_catalog.py`
- `src/main.py`
- `src/presentation/routes.py`
- `src/presentation/schemas.py`
- `src/presentation/security.py`
- `src/web/static/css/style.css`
- `src/web/static/js/mapping.js`
- `src/web/static/js/selection.js`
- `src/web/static/js/state.js`
- `src/web/templates/base.html`
- `src/web/templates/mapping.html`
- `tests/conftest.py`
- `tests/test_api.py`
- `tests/test_browser.py`
- `tests/test_domain.py`
- `tests/test_http_adapters.py`
- `tests/test_live.py`
- `tests/test_recovery.py`
- `tests/test_settings_logging.py`
- `tests/test_workflow.py`
- `tests/test_xml.py`
- `alembic/versions/0002_approvals.py`
- `docs/GAP_ANALYSIS.md`
- `docs/IMPLEMENTATION_REPORT.md`
- `docs/INTEGRATION_1C.md`
- `src/domain/embedding.py`
- `src/infrastructure/rabbitmq_bootstrap.py`
- `src/infrastructure/secrets.py`
- `src/web/static/css/blocks/api-docs.css`
- `src/web/static/js/docs.js`
- `src/web/templates/api_docs.html`
- `tests/test_approval.py`
- `tests/test_bootstrap_security.py`
- `tests/test_catalog_resume.py`
- `tests/test_embedding_identity.py`
