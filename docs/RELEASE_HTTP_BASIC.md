# Подключение подтверждённого HTTP Basic API 1С — 12.11.2026

## Проверенная база и границы изменения

До изменений локальная `main` и GitHub `Amadu-A/spec-mapper-ai` совпадали с mirror
`neo-term-it/spec-mapper-ai`: `fbcda48aa20c41bc32a9844b639eed475c690dc2`.
Рабочее дерево было чистым. Проверены AGENTS, LLM_CONTEXT и актуальные правила
shared-инфраструктуры; её HEAD — `839d419432fc25bc988e5871f7e53f6d282a2e77`.

Два препятствия находились в `CatalogSourceFactory`: readiness требовал непустой
Basic password, а validate запрещал любые credentials по HTTP после проверки URL.
Теперь Basic требует непустой username и сохраняет пустой password дословно.
HTTP Basic разрешён только при явном `ONEC_CATALOG_ALLOW_HTTP=true` и существующих
проверках host/IP/port. Bearer по HTTP по-прежнему запрещён, включая схему `HTTP:`.
DNS pinning, Host/SNI, TLS, запрет redirects, bounded download и completion marker сохранены.

Новых настроек, секретов, маршрутов и миграций нет. PostgreSQL/Qdrant, GUID registry,
staging/alias, webhook, scheduler, cooldown, owner/TTL, Top-5, выбор/null и approvals
не изменены. Defaults остаются `ALLOW_HTTP=false`, `CONTRACT_CONFIRMED=false`,
`CATALOG_SOURCE_TYPE=local_xml`; реальные credentials в tracked файлы не добавлялись.

## Изменённые файлы

- `src/infrastructure/catalog_sources.py` — две проверки Basic/HTTP.
- `tests/test_onec_basic_http.py` — адресные mock transport/security проверки.
- `tests/test_browser.py`, `tests/test_browser_v4.py` — ожидания завершения
  инициализации и сохранения перед следующим пользовательским действием.
- `tests/test_operator_gateway.py` — проверка полной SSH-команды с собственным логином.
- `.env.example` — только пояснение разрешающего флага, без изменения значений.
- `AGENTS.md`, `README.md`, `docs/DASHBOARD_AND_SYNC.md`, `docs/INTEGRATION_1C.md`,
  `docs/operations.md` и этот отчёт — актуальная политика и инструкции.

## Автоматические проверки

| Набор | Итог |
| --- | --- |
| Windows `ops/check-quality.ps1` | Ruff/format, 230 Python, 14 frontend, diff check — успешно |
| Linux, одноразовые контейнеры без сети | Ruff/format, 230 Python, включая Chromium — успешно |
| Coverage Windows / Linux | 86% / 86% |
| Live-маркеры в обоих offline прогонах | 12 исключены, не объявляются live-pass |

Локальные игнорируемые логи: `.http-basic-quality-final.log`,
`.http-basic-linux-final.log`, `.http-basic-transport.log`, `.http-basic-live.log`;
диагностика E2E — `.http-e2e-diagnostic.log`, `.http-e2e-approve-delay.log`,
`.http-e2e-fixed-repeat.log`.

Адресные 14 mock cases проверяют validate/probe/download/batch с кириллическим
username, пустым password и timeout 120; выключенный HTTP-флаг для всех auth;
HTTPS Basic/Bearer; отказ для чужих host/port/IP, redirect, пустого username,
неподтверждённого контракта и HTTP Bearer. Существующие тесты дополнительно
проверяют DNS rebinding/смешанные адреса, JSON completeness/limits, XML, V4,
webhook consumer, revision, owner и экспорты.

Две ошибки из приложенного Linux-лога исследованы отдельно:

1. Extended import: файл устанавливался до регистрации JS change handler;
   запроса import-json вообще не было. Тест ждёт завершения инициализации страницы.
2. Approve: закрытие диалога происходило до завершения обновления revision.
   При контролируемой задержке настоящего GET на 400 мс воспроизведён 409:
   отправлялась revision 3 при текущей 4. Тест ждёт сообщение о завершении сохранения.
   Без искусственной задержки старый approve-тест прошёл шесть прогонов.

После исправления ожиданий оба сценария прошли три последовательных Linux-прогона
и отдельный контроль с задержкой GET. Production JS не менялся: короткое окно
между закрытием диалога и обновлением revision остаётся; быстрый конфликтующий
клик защищён серверным 409. Это отдельное ограничение UI, а не блокировка 1С.
Эти браузерные проверки — offline/mocked, не live 1С.

## Реальная проверка API 1С

Из отдельного одноразового контейнера с исправленным адаптером выполнен настоящий
probe полного GET `/export`, затем POST `/items/batch` для двух полученных GUID.
Использованы согласованные Basic credentials с пустым password и timeout 120.

| Проверка | Результат |
| --- | --- |
| HTTP / completion | 200 / `X-Catalog-Complete: true` |
| Размер / время полного probe | 23 857 863 байта / 39,17 с |
| Всего / deleted | 65 493 / 3 439 |
| Конфликтующие группы кодов / лишние вхождения | 3 / 3 |
| Все карточки в карантине этих групп | 6, из них 1 deleted |
| Активные, допустимые для подбора | 62 049 |
| Batch | 2 карточки |
| Reconciliation | все записи во временной SQLite; 5 seeded legacy UUID сохранены |

Probe подтвердил контракт и не изменил рабочий каталог. Проверка reconciliation
не является публикацией в PostgreSQL/Qdrant: production import и генерация
embeddings не запускались. Временные данные удалены вместе с тестовым окружением.
ID и StartedAt рабочих API/worker/PostgreSQL/Qdrant/gateway/shared-сервисов до/после
совпали. Одноразовые контейнеры и пять созданных для этой задачи тестовых образов
удалены; исходный V4-образ сохранён. Входящий webhook непосредственно из 1С пока не проверен; mocks не заменяют
такую проверку. Полный первый production import выполняет владелец после probe.

## Ручные команды владельца

Windows PowerShell, из корня проекта; вывод проверок остаётся видимым:

```powershell
.\ops\check-quality.ps1
git diff --check
git diff
git add .env.example AGENTS.md README.md docs/DASHBOARD_AND_SYNC.md docs/INTEGRATION_1C.md docs/operations.md docs/RELEASE_HTTP_BASIC.md src/infrastructure/catalog_sources.py tests/test_onec_basic_http.py tests/test_browser.py tests/test_browser_v4.py tests/test_operator_gateway.py
git diff --cached --stat
git commit -m "fix: support explicitly allowed OneC HTTP Basic authentication"
git push origin main
```

У `origin` уже два push URL — основной репозиторий и mirror. Одна последняя команда
отправляет в оба; убедитесь в успехе каждого. Codex staging/commit/push не выполнял.

Перед обновлением в старом операторском UI выключите расписание и дождитесь
завершения активного импорта. На Linux:

```bash
cd ~/projects/spec-mapper-ai
git pull --ff-only origin main
bash scripts/test.sh
docker compose --env-file .env.example --env-file .env build api worker
nano .env
docker compose --env-file .env.example --env-file .env up -d --no-deps api worker
docker compose --env-file .env.example --env-file .env ps
docker compose --env-file .env.example --env-file .env logs --tail=100 api worker
```

В приватном `.env` примените точный блок из
[INTEGRATION_1C](INTEGRATION_1C.md#подключение-подтверждённого-http-api), заменив только
username placeholder согласованным пользователем. Password оставьте пустым,
timeout — 120, allowlist — точным для `192.168.10.30/32`. Существующие ключи сохраните.
После обновления кода владелец выставляет `CONTRACT_CONFIRMED=true` для уже проверенного
контракта. Bootstrap source не меняйте: сохранённые в PostgreSQL настройки URL/auth/
format/timeout нужно изменить отдельно в операторском UI.

Общий запуск одной командой `bash scripts/up.sh` сохранён. Перед первым ручным
probe/импортом этой доработки его не запускайте: он может автоматически импортировать
уже сохранённый источник. Указанные выше команды пересоздают только API/worker,
не удаляют volumes, не запускают bootstrap и не меняют shared-инфраструктуру.
После отдельного разрешения на runtime-проверки доступна `bash scripts/test.sh --live`;
offline gate и реальный probe выше не означают нового полного live-pass всех зависимостей.

Откройте туннель в отдельном PowerShell и оставьте окно открытым:

```powershell
ssh -N -L 18087:127.0.0.1:8088 main@192.168.55.3
```

Замените `main` собственным SSH-логином. `ssh - - ...` ошибочно воспринимает `-`
как hostname; нужны именно флаги `-N -L`. Адрес оператора:
`http://localhost:18087/operator`, настройки — `http://localhost:18087/settings`.

Вручную: получите одноразовый код, войдите; сохраните HTTP/json/GET/Basic, timeout 120
и completion header при выключенном auto; выполните probe, затем один ручной refresh.
Проверьте журнал импорта, quarantine/deleted/active, реальные `onec_guid`, переиспользованные
и рассчитанные embeddings, готовность Qdrant. После успеха включите 15:00 UTC.
Проверьте поиск, Top-5, перевыбор/null, массовое утверждение, approved.json/Excel и
неизменность исходного JSON. Реальную отправку webhook из 1С согласуйте отдельно:
202, применение в журнале и идемпотентный повтор event_id.
