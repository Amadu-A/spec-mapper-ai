<!-- README.md -->
# Spec Mapper AI

Вторая ступень после `spec-extracting-ai`: принимает готовый JSON от 1С/бота или
получает его по UUID извлечения, подбирает **Top-5** через shared-embedding и Qdrant.
Web позволяет искать по всему справочнику, перевыбирать позицию, явно оставлять её
без соответствия и утверждать неизменяемый итог для 1С. Доступны JSON/Excel.
Регистрации, обучения моделей и облачных AI API нет.

## Структура

```text
src/domain/          исходные строки и правила ранжирования
src/application/     use-cases API/web/worker, порты
src/infrastructure/  PostgreSQL, XML, HTTP, Qdrant, RabbitMQ
src/presentation/    REST, авторизация, cookie/CSRF
src/web/             Jinja2, BEM CSS, JS modules
alembic/             миграции собственной БД
data/*.xml           актуальная выгрузка 1С; не коммитится
renders/             концепт интерфейса
tests/               unit, API, regression, architecture, browser, live
```

## Настройки

Обычные настройки — в `.env.example` или окружении команды. Pydantic и Compose:
**окружение → `.env` → `.env.example` → defaults**. `.env` содержит только секреты.
Первый запуск атомарно создаёт отсутствующие `DATABASE_PASSWORD`, `RABBITMQ_PASSWORD`,
`SESSION_SECRET`, `CATALOG_ADMIN_KEY` и `API_KEYS` с отдельными ключами `onec`/`telegram`.
Существующие значения, включая прежний `API_KEY`, сохраняются; права Linux 0600.
Пользователи web не создаются: автоматически готовятся DB/RabbitMQ credentials и cookie-сессии.

Для polling добавьте в `.env` **существующий** ключ извлечения:

```dotenv
EXTRACTING_API_KEY=<ключ spec-extracting-ai, владеющий исходным заданием>
```

Для прямого JSON upstream-ключ не нужен. Ключи mapper и extracting различаются;
секрет shared-embedding не требуется. Не помещайте API-ключи в браузерный JavaScript.

## Развёртывание

Linux: Docker Engine/Compose **2.24+**, `uv`, доступ к Docker и работающие
RabbitMQ/shared-embedding в `ai-shared`. Скопируйте XML в `data/`, затем:

```bash
bash scripts/up.sh
```

Команда проверяет окружение/порт/XML, готовит секреты и выделенные RabbitMQ user/vhost,
собирает контейнеры, применяет миграции, импортирует каталог с прогрессом и ждёт
реальной готовности. Логи видны. Повторный запуск сохраняет данные и volumes.
Адрес: **http://192.168.55.3:8087/**; API-документация: **/docs** без внешнего CDN.
Параметры `APP_BIND`, `APP_PORT`, `APP_PUBLIC_BASE_URL` — в открытой конфигурации.
DB/Qdrant наружу не публикуются. Доступ — LAN/VPN; публикация в интернет требует
отдельного HTTPS reverse proxy и сетевой политики.

В текущем XML — **62 052** разрешённые активные позиции. Конфликтующие коды целиком
карантинируются. GUID 1С отсутствует: внутренний UUID и `onec_guid=null` различаются.
Первый импорт имеет отдельный предел 24 часа и возобновляемые checkpoints;
неизменённые позиции повторно не векторизуются. Ежедневный XML sync выключен по умолчанию.
Прямой адаптер справочника из 1С подключается после согласования её API.

## Проверки

```powershell
.\ops\check-quality.ps1
```

```bash
bash scripts/test.sh          # Ruff, Python, Node, браузер
bash scripts/test.sh --live   # после up.sh, дополнительно реальные зависимости/API
```

Первый запуск устанавливает Chromium; на минимальном Linux заранее:
`uv run playwright install --with-deps chromium`. Браузерные offline-тесты явно
подменяют AI/broker; live-тесты используют настоящие сервисы.

Программисту 1С: [контракты и последовательность вызовов](docs/INTEGRATION_1C.md).
Также: [API](docs/API.md), [эксплуатация](docs/operations.md),
[архитектура](docs/architecture.md), [ТЗ](docs/TECHNICAL_SPECIFICATION.md),
[отчёт проверок](docs/IMPLEMENTATION_REPORT.md).
