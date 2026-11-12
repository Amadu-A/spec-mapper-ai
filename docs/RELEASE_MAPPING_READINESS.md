# Готовность формы сопоставления — 12.11.2026

## Проверка исходного состояния

Локальная `main`, GitHub `Amadu-A/spec-mapper-ai`, mirror `neo-term-it/spec-mapper-ai`
и серверный checkout совпали: `9331482d34b2d1748874eede09713b8f59e77763`.
Рабочее дерево перед изменениями было чистым. Прочитаны AGENTS, LLM_CONTEXT,
затронутые файлы/тесты и правила разработки/frontend. HEAD shared-инфраструктуры
не изменился: `839d419432fc25bc988e5871f7e53f6d282a2e77`.

V4 находится в `fbcda48`, разрешение подтверждённого HTTP Basic с пустым паролем —
в `9331482`. Это реализованный код; первый production HTTP-импорт и настоящая
обратная отправка webhook из 1С остаются отдельными runtime-проверками владельца.

## Ошибка и исправление

В приложенном логе упал один Chromium E2E: за 5 секунд не появилось «Каталог: …».
Итог — 229 passed, 1 failed, 12 live deselected. Сборка контейнеров ещё не запускалась.
Этот offline test использует собственные Settings без `.env`, SQLite и mock 1С;
приведённые владельцем Basic credentials не являются причиной данного assertion.

Старый тест ожидал фоновую сводку как признак готовности импорта. Это разные процессы:
обработчики формы подключаются после подготовки сессии, а сводка обновляется отдельно.
Кроме того, до подключения обработчиков HTML уже разрешал выбрать файл/submit.
Предыдущее исправление ожидания по сводке оказалось недостаточным.

Теперь HTML блокирует файл, UUID и submit до готовности сессии и регистрации всех
обработчиков в `createMapping`. При ошибке сессии форма остаётся заблокированной,
а существующий обработчик показывает причину в UI. При медленной сводке импорт
доступен. Тесты ждут реальную доступность формы с ограниченным timeout и выводят
при сбое сообщение UI, ошибки JS и статусы/пути HTTP без credentials.

Изменены только:

- `src/web/templates/mapping.html` — начальное состояние controls/подписи.
- `src/web/static/js/mapping.js` — разрешение ввода после подключения обработчиков.
- `tests/test_browser.py`, `tests/test_browser_bulk_approval.py` — ожидание формы.
- `tests/test_browser_v4.py` — обычный старт, медленная сводка, задержка и отказ сессии,
  безопасная диагностика; успешные API-ответы не подменяются.
- `docs/operations.md` и этот отчёт — проверка, настройка и запуск.

Backend, авторизация, Top-5, approvals, каталог, scheduler, webhook, PostgreSQL,
Qdrant, миграции, порты и `.env` не изменялись.

## Диагностика и тестирование

Исходный E2E без задержек прошёл 10 отдельных Linux-прогонов; полный исходный набор
также прошёл: 230 passed. Точная причина задержки/ошибки сводки именно в присланном
запуске не зафиксирована: в том логе нет статусов HTTP или ошибок JS.
Контроль с задержкой настоящего запроса сводки на 6 секунд воспроизвёл тот же
assertion по «Проверка каталога…». Два новых сценария с задержкой и отказом сессии
до исправления UI упали, потому что upload оставался enabled.

После исправления шесть адресных Windows-сценариев прошли; на Linux те же шесть
прошли три раза подряд, включая работу импорта при удерживаемом запросе сводки.
Управление задержкой выполнено через события, без произвольного sleep в итоговых тестах.

| Итоговая проверка | Результат |
| --- | --- |
| Windows `ops/check-quality.ps1` | Ruff/format, 233 Python, 14 frontend, diff check — успешно |
| Linux, полный Python/Chromium набор | 233 passed дважды, 12 live deselected в каждом |
| Linux Ruff/format и все frontend файлы | успешно, 14 frontend passed |
| Coverage Windows / Linux | 86% / 86% |

Linux-проверки выполнялись в отдельной `/tmp`-копии Windows-кода с теми же установленными
Python/Chromium, что у владельца. Серверный checkout, рабочие контейнеры и данные
не изменялись. Временная копия после проверок удалена.
Это offline/mocked проверки, не новый live-pass внешних интеграций.

Локальные игнорируемые логи: `.e2e-init-baseline.log`, `.e2e-init-full-baseline.log`,
`.e2e-init-slow-summary-red.log`, `.e2e-init-controls-red.log`,
`.e2e-init-windows-targeted.log`, `.e2e-init-linux-targeted.log`,
`.e2e-init-quality-final.log`, `.e2e-init-linux-final.log`, `.e2e-init-linux-frontend.log`.

## Применённая конфигурация работающего API

Read-only проверка контейнера показала `allow_http=false`, `contract_confirmed=false`,
`username_configured=false`, `password_empty=true`, `timeout_seconds=60`.
Hash адаптера в контейнере — `faae0ed9880423da7ddc19433b5b88e540b510bb548d75fb54a0a2f11bb66e3f`;
это исходный `fbcda48`, тогда как в `9331482` hash равен
`b2b01918b79338c4469ae0b3591a62738a99d2ca8d26a0a23814a0290ec77bb2`.
Следовательно, git pull уже выполнен, но исправленный образ и приватные overrides
в работающий API ещё не применены. Значения секретов не выводились;
контейнеры при диагностике не пересоздавались. Лог — `.e2e-init-runtime-readonly.log`.

## Проверка и публикация владельцем

Windows PowerShell, корень проекта:

```powershell
.\ops\check-quality.ps1
git diff --check
git add src/web/templates/mapping.html src/web/static/js/mapping.js tests/test_browser.py tests/test_browser_bulk_approval.py tests/test_browser_v4.py docs/operations.md docs/RELEASE_MAPPING_READINESS.md
git diff --cached
git commit -m "fix: enable mapping inputs after session initialization"
git push origin main
```

`origin` уже имеет два push URL; проверьте успех основного репозитория и mirror.
Codex staging/commit/push не выполнял.

На установленном сервере выключите auto и дождитесь завершения активного импорта.
Linux-команды владельца показывают логи:

```bash
cd ~/projects/spec-mapper-ai
git pull --ff-only origin main
bash scripts/test.sh
nano .env
docker compose --env-file .env.example --env-file .env up -d --no-deps --build api worker
docker compose --env-file .env.example --env-file .env ps
docker compose --env-file .env.example --env-file .env logs --tail=100 api worker
```

В показанном приватном `.env` не хватает `ONEC_CATALOG_TIMEOUT_SECONDS=120`.
Добавьте эту строку, сохранив реальные ключи и пустой пароль 1С. Остальные параметры
из [INTEGRATION_1C](INTEGRATION_1C.md#подключение-подтверждённого-http-api) наследуются
из `.env.example`, если нет иных overrides. URL/auth/json/timeout установленного
источника сохраняются отдельно в PostgreSQL через операторский UI; env их не заменяет.

Общий запуск `bash scripts/up.sh` сохранён; до первого ручного probe/импорта
используйте приведённый recreate, поскольку общий bootstrap может импортировать
уже сохранённый источник. Volumes/shared-сервисы не удаляются и не перезапускаются.

Откройте туннель и оставьте терминал открытым; `main` заменяется своим логином:

```powershell
ssh -N -L 18087:127.0.0.1:8088 main@192.168.55.3
```

Вручную проверьте `http://localhost:18087/mapping`: пока сессия готовится, ввод
недоступен, затем доступны JSON/UUID. Импортируйте расширенный JSON, проверьте
file_name/Brand/Article/Note, Top-5, поиск/перевыбор/null, массовое утверждение и экспорты.
В `/operator` получите код, затем в `/settings` сохраните HTTP/json/GET/Basic и timeout 120
при выключенном auto; выполните probe, один refresh, проверьте отчёт и реальные GUID.
После успешного импорта включите 15:00 UTC. Обратный webhook из 1С проверяется отдельно.
