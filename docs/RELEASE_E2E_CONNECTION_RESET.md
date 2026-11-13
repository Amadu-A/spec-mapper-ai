# ECONNRESET в E2E и порядок запуска — 13.11.2026

## Диагностика

Проверены локальный чистый HEAD и GitHub `main` Mapper:
`e433363a4a19007538eded5793899f3decdaa832`. В присланном логе mirror также обновлён
до этого коммита. Shared guidelines не изменились: `839d419432fc25bc988e5871f7e53f6d282a2e77`.

Упавший тест получил `APIRequestContext.get: socket hang up (ECONNRESET)` при GET
`source.json` отдельного временного сервера на loopback. HTTP-ответ не был получен;
точный источник закрытия сокета в логе не указан. Это отличается от HTTP 4xx/5xx.
Сборка рабочего образа в том же логе завершилась успешно, новый API стартовал.
Остановка старого процесса при recreate через SIGTERM (код 143) ожидаема.

Read-only проверка уже поднятого владельцем сервиса:
`/health/live` — HTTP 200, `/health/ready` — HTTP 200, `ready=true`;
database/rabbitmq/shared_embedding/qdrant/catalog — true.
Критичный адаптер каталога в API и worker совпадает (исправление HTTP Basic присутствует в обоих).
Codex сервисы не перезапускал, импорт и production deployment не выполнял.

## Исправление и доказательства

В E2E общего утверждения безопасные GET-чтения через Playwright APIRequestContext
получили `max_retries=2`. По [официальной документации Playwright](https://playwright.dev/python/docs/api/class-apirequestcontext#api-request-context-get-option-max-retries)
это повторяет только ECONNRESET, не HTTP-коды; после исчерпания лимита остаётся ошибка.
Операции записи и весь тест целиком не перезапускаются. HTTP-коды не являются
основанием повтора GET этой настройкой.

Контролируемый тестовый TCP-прокси закрывает первое соединение до ответа. До
исправления это воспроизвело ту же ошибку. После исправления второй GET получает
настоящий ответ временного FastAPI с исходными cookie и проверяет исходный JSON.
Успешный ответ не подменяется. Дополнительные контроли: 503 возвращается после
одной попытки, постоянный reset даёт ошибку после трёх попыток. Обычный сценарий
по-прежнему проверяет выбор/null, конфликт revision, approve и реальное скачивание Excel.

После reload UI при сетевой ошибке повторяет status GET через 5000 мс, а фоновые
виджеты обновляются раз в 10 секунд. Прежние 5 секунд assertion не покрывали даже
один такой цикл с HTTP. Контролируемый отказ первого status GET воспроизвёл таймаут;
теперь E2E имеют ограниченный бюджет 15 секунд (один цикл + запас), после теста
default восстанавливается. API/unit-проверки, HTTP timeout и production retry не меняются.
При сбое восстановления выводятся статусы/пути HTTP, ошибки JS и сообщение UI без cookie.

При полном Linux-прогоне выявлена отдельная гонка теста публичного refresh:
он ждал пустого `jobs.pending()`, хотя этот outbox не содержит задания, уже взятого
worker по lease. Затем тест проверял доступность кнопки, пока импорт ещё мог идти.
Теперь он получает job_id настоящего POST/202 и ждёт `state=complete` этого задания
перед переводом тестовых часов и reload. `failed` также проверяется явно.

Изменены только `tests/test_browser_bulk_approval.py`, `tests/test_browser_public_refresh.py`,
`tests/conftest.py`, пояснение маркера `e2e`
в `pyproject.toml`, `README.md`, `docs/operations.md` и этот отчёт.
Production-код, экспорт Excel/JSON, `.env`, зависимости/uv.lock, Docker, startup
scripts, каталог, SQL/Qdrant/RabbitMQ и shared-инфраструктура не изменялись.

Windows-адресный набор после всех изменений: 3 passed.

| Итоговая проверка | Результат |
| --- | --- |
| Windows `ops/check-quality.ps1` | Ruff/format, 249 Python, 15 frontend, diff check — успешно |
| Linux в изолированной копии | Ruff/format, 249 Python, 15 frontend — успешно |
| Контролируемые reset/polling/cooldown E2E на Linux | 3 сценария прошли 3 раза подряд |
| Покрытие Windows / Linux | 86% / 86% |
| Live-маркеры | 12 исключены из offline-прогонов; не объявляются live-pass |

Linux использовал собственные временную копию и venv по неизменённому uv.lock;
рабочие checkout/venv/контейнеры не менялись. Тестовое окружение после прогонов удалено.
Логи в локальных игнорируемых файлах: `.econnreset-red.log`,
`.econnreset-targeted.log`, `.econnreset-windows-final.log`,
`.econnreset-reload-red.log`, `.econnreset-final-targeted.log`,
`.econnreset-linux-final.log`, `.econnreset-runtime-health.log`.

## Почему тесты можно запускать до up.sh

- `bash scripts/up.sh` — одна команда полного запуска: проверка окружения, секреты,
  bootstrap, миграции, API/worker/gateway, импорт сохранённого источника и readiness.
  Ruff/pytest она не запускает. Повторный импорт использует существующий pipeline.
- `bash scripts/test.sh` — обычные offline-проверки: собственные временные HTTP/SQLite,
  mock внешних систем, Ruff, pytest, Node и Chromium. Рабочий сервис не требуется.
  Его запуск не влияет на временный loopback-сервер упавшего теста.
- `bash scripts/test.sh --live` — дополнительно реальные зависимости/API;
  они должны быть запущены заранее. Live-маркеры не приравниваются к offline-pass.

Проверка кода перед обновлением контейнеров рекомендована, чтобы регрессии обнаружились
до deployment. Технической зависимости offline-тестов от `up.sh` нет: их можно выполнить
до или после полного запуска. Для предыдущей замены только Excel был дан selective
recreate API, чтобы не запускать ненужный bootstrap/импорт каталога.

## Команды владельца

Windows PowerShell из корня проекта:

```powershell
.\ops\check-quality.ps1
git add tests/test_browser_bulk_approval.py tests/test_browser_public_refresh.py tests/conftest.py pyproject.toml README.md docs/operations.md docs/RELEASE_E2E_CONNECTION_RESET.md
git diff --cached
git commit -m "test: stabilize E2E network reads and state waits"
git push origin main
```

После публикации на Linux:

```bash
cd ~/projects/spec-mapper-ai
git pull --ff-only origin main
bash scripts/test.sh
```

Эта правка касается только тестов/пояснений, уже работающий API пересобирать из-за неё
не требуется. Команда полного запуска при необходимости остаётся прежней:

```bash
bash scripts/up.sh
```

При полном запуске учитывайте, что он импортирует сохранённый источник. После него
при необходимости выполняется `bash scripts/test.sh --live`. Логи всех ручных команд
видимы. `.env` не меняйте, volumes не удаляйте. Commit/push выполняет владелец.

Вручную откройте `http://192.168.55.3:8087/mapping`, импортируйте JSON, проверьте выбор/null,
утверждение и прежнюю кнопку Excel: до approval недоступна, после — `mapping.xlsx`
с `Лист_1`, A:S и T:Y. Runtime-поведение этой тестовой правкой не менялось.
