<!-- docs/RELEASE_LINUX_TEST_ISOLATION.md -->
# Изоляция браузерных Linux-проверок — 13.11.2026

База: локальный HEAD и GitHub `main`
`e5c3870158767814cf4aa5d958ac12fec6d7de31`.
Правила shared-инфраструктуры сверены с
`839d419432fc25bc988e5871f7e53f6d282a2e77`.

## Причина повторного отказа

В приложенном журнале 248 тестов успешны, V4 `[normal]` падает после reload:
имя файла не появляется, страница остаётся на подготовке сессии.
Диагностический прогон во временной Linux-копии воспроизвёл ту же остановку
инициализации также на операторской странице. В серии повторов отказ получен
на двадцатом запуске. Chromium сообщил `net::ERR_NETWORK_CHANGED` при загрузке
`dashboard-state.js`, `dom.js`, `source-fields.js`, `selection.js`.
Приложение не получало необходимые модули и не начинало инициализацию.
Увеличение assertion timeout это не исправляло.

Конкретное изменение сетевых интерфейсов хоста, вызвавшее уведомление Chromium,
не установлено. Успешные варианты с `page.route` не опровергали сетевой сбой:
[Playwright отключает HTTP cache при routing](https://playwright.dev/python/docs/api/class-page#page-route),
поэтому условия загрузки в этих вариантах отличались.

## Изменения

- `scripts/test.sh`: offline Python/API/Chromium запускаются в собственном
  временном контейнере `--network none`, Ruff/Node остаются на хосте.
  [Режим Docker none](https://docs.docker.com/engine/network/drivers/none/)
  предоставляет только loopback внутри отдельного сетевого пространства.
  `.env`, данные, volumes и Docker socket не монтируются; порты не публикуются.
  Результаты и coverage копируются обратно, собственный контейнер удаляется,
  ненулевой exit code сохраняется. `--live` по-прежнему использует Compose
  и настоящие зависимости после успешных offline-проверок.
- `Dockerfile`: Chromium и его системные библиотеки добавлены только в тестовый
  target; зависимости кэшируются отдельно от исходников. Production target
  `runtime` не изменён. Отдельный тег offline-образа не заменяет образ приложения.
- `tests/test_browser_v4.py`: на любом падении сценария сохраняются trace,
  screenshot, статусы/пути запросов, незавершённые запросы, ошибки console/JS.
  Assertion timeout и успешные проверки не ослаблены, повтор теста не добавлен.
- `README.md`, `docs/operations.md`: обновлены требования и порядок проверок.
- Этот отчёт: причина, доказательства и команды приёмки.

## Проверки

Linux: выполнен именно `bash scripts/test.sh` во временной копии кода с Windows,
без изменения рабочего checkout и без подключения к shared-сетям.
Ruff и форматирование успешны; 249 Python/API/браузерных тестов успешны,
12 live-тестов исключены; покрытие 86%; 15 Node-тестов успешны.

Отдельно проверен контролируемый отказ команды контейнера: exit code 7 дошёл
до вызывающей команды, продолжение не выполнялось, артефакты/coverage скопированы,
временный контейнер удалён. Проверялся фактический фрагмент runner из `test.sh`.

Результаты Windows и серии повторов добавляются после завершения прогонов.
Внешние 1С, PostgreSQL, RabbitMQ, Qdrant и embedding в этом исправлении live
не проверялись; offline AI/broker явно подменены. Настройки, контракты,
сроки хранения и пользовательские данные не изменены. Commit/push/deployment
не выполнялись, shared-сервисы и firewall не менялись.

## Ручная публикация

Windows PowerShell, из корня проекта:

```powershell
.\ops\check-quality.ps1
git diff --check
git status --short
git add Dockerfile scripts/test.sh tests/test_browser_v4.py README.md docs/operations.md docs/RELEASE_LINUX_TEST_ISOLATION.md
git diff --cached --stat
git commit -m "test: isolate Linux browser checks from host network changes"
git push origin main
```

Linux, после ручного push:

```bash
cd ~/projects/spec-mapper-ai
git status --short
git pull --ff-only origin main
bash scripts/test.sh
```

Первый запуск соберёт тестовый образ с Chromium; его логи видны.
Этот патч меняет тесты, поэтому пересоздавать работающий API ради него не требуется.
Когда требуется полный запуск/обновление приложения, используйте прежние команды:

```bash
bash scripts/up.sh
bash scripts/test.sh --live
```

`up.sh` выполняет bootstrap, миграции, импорт настроенного каталога и readiness;
тесты не заменяют эту команду. Не выполнять `down -v` или очистку shared-сетей.

При ручной приёмке на `http://192.168.55.3:8087/mapping` импортировать JSON,
проверить имя файла/расширенные поля, ручной выбор и пустое соответствие,
утвердить результат, перезагрузить страницу и скачать Excel прежней кнопкой.
Сохранённое решение и имя файла должны восстановиться. Операторский вход через
SSH gateway должен активировать настройки; обычная сессия не должна их менять.
