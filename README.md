<!-- README.md -->
# Spec Mapper AI

Локальный сервис сопоставляет позиции проектных и рабочих спецификаций с номенклатурой 1С.
Получает готовый JSON из `spec-extracting-ai` по UUID задания и серверному API-ключу,
ищет Top-K в Qdrant через существующий `shared-embedding`, учитывает технические
параметры и единицы. В web доступны поиск, ручной выбор, сохранение и экспорт JSON/Excel.
Регистрации, обучения и облачных AI API нет.

## Структура

```text
src/domain/          модели, контракт исходной пятёрки, правила ранжирования
src/application/     use-cases и порты; общие для API, web и worker
src/infrastructure/  PostgreSQL, XML, HTTP, Qdrant, RabbitMQ
src/presentation/    HTTP, cookie, Origin/CSRF и ограничения запросов
src/web/             Jinja2, BEM CSS, JS modules
src/bootstrap.py     сборка зависимостей
alembic/             миграции собственной БД
data/*.xml           локальная выгрузка; в Git не включается
renders/             исходный концепт интерфейса
tests/               unit, API, архитектура, regression, live и browser E2E
examples/legacy/     сохранённая старая заготовка; игнорируется Git
```

## Настройки

`.env.example` — полный каталог обычных настроек и пустых секретов.
Pydantic и скрипты Compose используют одинаковый приоритет:
**окружение команды → `.env` → `.env.example` → defaults**.
Обычные параметры меняйте в `.env.example` или окружении команды.

При первом `up.sh` автоматически создаются только отсутствующие проектные секреты:
`DATABASE_PASSWORD`, `RABBITMQ_PASSWORD`, `SESSION_SECRET`, `API_KEY`, `CATALOG_ADMIN_KEY`.
Существующие пароли не заменяются. В `.env` дополнительно запишите:

```dotenv
# Серверный интеграционный ключ spec-extracting-ai; в браузер не передаётся.
EXTRACTING_API_KEY=<существующий ключ API извлечения>
```

Проверьте `EXTRACTING_BASE_URL` в `.env.example`: нужен фактический Docker DNS и
внутренний порт извлечения в `ai-shared`. Ключ должен владеть upstream заданием:
cookie-задание другого пользователя не становится доступным по чужому API-ключу.
У `shared-embedding` отдельного API-ключа нет.
Default `http://spec-extracting-ai-api-1:8083` соответствует опубликованному Compose
извлечения с project name `spec-extracting-ai`; runtime адрес проверяется на сервере.

## Развёртывание

Требуются Linux, Docker Engine/Compose **2.24+**, `uv`, доступ к Docker, уже работающие
`rabbitmq:5672` и `shared-embedding:8000` в существующей сети `ai-shared`.
Положите реальный XML в `data/`, затем:

```bash
bash scripts/up.sh
```

Команда проверяет порт, готовит выделенные user/vhost `spec_mapper`, собирает приложение,
ждёт PostgreSQL, применяет миграции, запускает API/worker, импортирует XML и ждёт `/health/ready`.
Показывает журналы. Повторный запуск сохраняет данные и volumes.
Общие RabbitMQ/embedding не перезапускаются. Внешне открывается только web/API:
`http://<адрес-сервера>:8084/`. Порт редактируется через `APP_PORT`; 8083 запрещён.
Доступ к HTTP ограничьте существующим LAN/VPN firewall.

В текущем XML нет GUID 1С: внутренний UUID детерминирован по коду и явно отделён от
`onec_guid=null`. Все записи с конфликтующими кодами исключаются с предупреждением.
В предоставленном файле парсер получил **62 052** активные однозначные позиции.
Векторизация выполняется на сервере при первом импорте; неизменные позиции затем
используют кэш. Полные обновления строятся в staging и публикуются атомарным alias switch.

## Проверки

Windows, Ruff + Python/архитектура/API + frontend + браузерный E2E:

```powershell
.\ops\check-quality.ps1
```

Linux — одна команда, включая те же проверки:

```bash
bash scripts/test.sh
```

При первом запуске устанавливается Chromium. На минимальном Linux заранее выполните
`uv run playwright install --with-deps chromium` для системных библиотек браузера.
Браузерный тест использует настоящий FastAPI/SQLite и **подменённые внешние AI/broker порты**.
Реальные интеграции проверяются отдельно на поднятом сервере:

```bash
bash scripts/test.sh --live
```

Команды по умолчанию не прячут вывод. Внутренние прогоны можно перенаправлять в `.log`.
Код и запуск не означают подтверждённую точность подбора на ваших спецификациях:
порогам требуется приёмка на реальном размеченном корпусе.

Подробности: [ТЗ](docs/TECHNICAL_SPECIFICATION.md), [архитектура](docs/architecture.md),
[API](docs/API.md), [эксплуатация и ручные проверки](docs/operations.md).
