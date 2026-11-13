# Единый Excel утверждённой спецификации — 13.11.2026

## Проверенная база и анализ изменений

Локальная `main`, GitHub и mirror Mapper до изменений:
`fea5028ff85ef523664bece0e69103c2c1a559a1`, рабочее дерево чистое.
Локальный HEAD и GitHub `main` Extracting:
`9844c4ee68fbd35840b0e5bb634ab6a5eef0879e`, рабочее дерево Extracting чистое.
Shared guidelines: `839d419432fc25bc988e5871f7e53f6d282a2e77`, без обновлений.
Прочитаны AGENTS, LLM_CONTEXT, правила разработки/frontend, exporter и тесты обоих форматов.

Старый Excel Mapper создавал собственный лист/заголовки из текущих строк задания
и был доступен до утверждения. Новый exporter заменяет его на утверждённый шаблон
Extracting, принимает только сохранённый approval snapshot и использует прежний
`GET /api/v1/mappings/{job_id}/result.xlsx`. До approval — 409 `not_approved`,
owner/TTL — прежние 404/410. Имя файла остаётся `mapping.xlsx`.

Бинарный `src/resources/specification_v1.xlsx` скопирован без изменений из
`app/resources/specification_v1.xlsx` закреплённого HEAD Extracting.
SHA-256 — `008dd7b7130668acc55e1bfdf96cf4be3b8bc05c1e0c27df0668189a6f14f2c4`.
Его 18 исходных граф расширяются графой P точно как в `app/infrastructure/excel.py`
Extracting: получаются 19 граф A:S, затем добавляются ровно шесть граф T:Y.
Лист `Лист_1`, стили/ширина A:S, восемь исходных полей, имя файла, цены/суммы
и `RUB` сохраняются. Строки не сортируются, дубли не удаляются, пустые значения
не заменяются числами. Новые графы описаны в [API](API.md#excel-утверждённой-спецификации).

V получает только `onec.onec_guid`; при отсутствии реального GUID ячейка пуста.
При `onec=null` пусты T:W, но исходная строка остаётся. Строковые поля, включая
коды/артикулы/имя файла и формулоподобный текст, записываются строковым XML-типом.
Индекс Y сохраняется числом из snapshot, начиная с нуля. Старые approvals без
file_name/Brand/Article/Note дают пустые ячейки, сам JSON не изменяется.

Существующая кнопка Excel и ссылка в списке заданий доступны после approval.
Новых кнопок, страниц, вариантов Excel или маршрутов сопоставления нет.
Существующий технический Excel-отчёт журнала импорта каталога сохранён без изменений;
он не является экспортом спецификации и не входит в заменяемую логику.

## Файлы

- `src/application/reports.py` — замена существующего exporter, перенос заполнения шаблона.
- `src/presentation/routes.py` — чтение approved snapshot, прежний маршрут и OpenAPI ошибок/MIME.
- `src/resources/specification_v1.xlsx`, `src/resources/README.md` — локальный шаблон и происхождение.
- `src/web/static/js/state.js`, `mapping.js`, `jobs.js` — общее правило доступности прежних ссылок.
- `tests/test_excel_report.py` — шаблон/стили/колонки, 0–10 000 строк, дубли, legacy,
  формулоподобный текст, GUID, null, owner/TTL, snapshot и отсутствие повторного подбора.
- `tests/test_api.py`, `test_specification_v4.py`, `test_bulk_approval.py` — регрессии прежних контрактов.
- `tests/test_browser.py`, `test_browser_bulk_approval.py`, `tests/frontend/approval.test.js` —
  доступность Excel и реальное скачивание прежней кнопкой после approval.
- `README.md`, `docs/API.md`, `docs/INTEGRATION_1C.md`, `docs/operations.md`, этот отчёт — документация.

Extracting, JSON-контракты, подбор/Top-5, ручной выбор, операция approval, каталог,
webhook, SQL/Qdrant/RabbitMQ, `.env`/`.env.example`, миграции и TTL не изменялись.
Ресурс включается существующим `COPY src ./src`, Dockerfile не меняется.

## Проверки

До замены два затрагиваемых API/Excel-теста прошли. После замены адресный набор:
31 passed; регрессии общего утверждения и браузеров: 7 passed.
Сравнение с фактическим `build_excel` из закреплённого Extracting на 3 и 50 строках
подтвердило равенство всех значений/стилей A:S, column dimensions и row dimensions.
Шаблон и результат отрендерены и визуально проверены. Текстовые коды с нулями
дополнительно проверены по OOXML `inlineStr`; визуальный renderer не заменяет
эту проверку типов. Нативный Microsoft Excel автоматически не запускался.

Windows `ops/check-quality.ps1`: Ruff/format, 248 Python-тестов, 15 frontend-тестов,
`git diff --check` — успешно. Покрытие проекта — 86%, модуля reports — 100%.
12 live-тестов исключены из offline-прогона.

В одном Linux-прогоне прежний `test_browser_v4[normal]` не дождался доступности
операторского поля формата за 5 секунд. Это отдельная проверка настроек каталога,
её production-код и тест в этой задаче не менялись. С диагностикой сценарий прошёл
10 последовательных повторов; точная причина единичного сбоя не установлена.
Итоговый повтор полного Linux quality gate прошёл: Ruff/format, 248 Python-тестов
и все 15 frontend-тестов, покрытие 86%; 12 live-маркеров исключены.

Linux-прогоны выполнены в собственной временной копии и venv по существующему
uv.lock. Рабочая серверная venv, checkout, контейнеры и volumes не изменялись.
Временная Linux-копия и её venv после прогонов удалены.
Это offline/mocked проверки с реальными SQLite/FastAPI/Chromium; live-маркеры
не объявляются live-pass. Реальные 1С/embedding/Qdrant для изменения Excel не вызываются.

Локальные игнорируемые логи: `.excel-baseline.log`, `.excel-targeted.log`,
`.excel-regression-fix.log`, `.excel-quality-final.log`, `.excel-linux-final.log`,
`.excel-linux-e2e-diagnostic.log`.
Материалы сверки и рендера — только в игнорируемой `.cache/excel-qa`.

## Команды владельца

Windows PowerShell, из корня проекта; логи проверок остаются видимыми:

```powershell
.\ops\check-quality.ps1
git diff --check
git add src/application/reports.py src/presentation/routes.py src/resources/specification_v1.xlsx src/resources/README.md src/web/static/js/state.js src/web/static/js/mapping.js src/web/static/js/jobs.js
git add tests/test_excel_report.py tests/test_api.py tests/test_specification_v4.py tests/test_bulk_approval.py tests/test_browser.py tests/test_browser_bulk_approval.py tests/frontend/approval.test.js
git add README.md docs/API.md docs/INTEGRATION_1C.md docs/operations.md docs/RELEASE_EXCEL_TEMPLATE.md
git diff --cached --stat
git commit -m "feat: use approved specification template for mapping Excel"
git push origin main
```

У origin два push URL: основной репозиторий и mirror. Проверьте успех обоих.
Codex staging/commit/push не выполнял.

Linux после ручной публикации:

```bash
cd ~/projects/spec-mapper-ai
git pull --ff-only origin main
bash scripts/test.sh
docker compose --env-file .env.example --env-file .env up -d --no-deps --build api
docker compose --env-file .env.example --env-file .env ps
docker compose --env-file .env.example --env-file .env logs --tail=100 api
```

Для этой замены достаточно пересоздать собственный API: worker, каталог и scheduler
не менялись. `.env` редактировать не требуется. Общий `bash scripts/up.sh` сохранён;
для обновления Excel bootstrap/импорт каталога не нужен. Shared-сервисы и volumes
не трогайте. Production deployment выполняет владелец.

## Ручная проверка

1. На `http://192.168.55.3:8087/mapping` импортируйте JSON с именем файла, восемью
   полями, дублями и строками с начальными нулями. До approval Excel недоступен,
   прямой запрос с авторизацией даёт 409 `not_approved`; JSON работает как прежде.
2. Сделайте ручной выбор, для другой строки оставьте null и утвердите весь результат.
   Прежняя кнопка Excel становится доступна и скачивает `mapping.xlsx`.
3. Откройте файл: один лист `Лист_1`, исходные A:S, T:Y с шестью точными заголовками,
   G=`RUB`. Число/порядок строк, дубли и восемь исходных полей совпадают с JSON;
   цены и суммы пусты. Коды и артикулы с нулями сохранены, `=...` отображается текстом.
4. Сверьте T:W с `approved.json`: настоящий GUID из onec_guid, пустой V при его
   отсутствии и пустые T:W для null. X/Y совпадают с matching.status/index.
5. Проверьте исходный/предварительный JSON и финальный approved.json, повторное
   скачивание и ссылку Excel в меню утверждённого задания. Дополнительных кнопок
   и вариантов Excel спецификации быть не должно.
