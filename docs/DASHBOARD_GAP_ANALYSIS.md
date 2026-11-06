<!-- docs/DASHBOARD_GAP_ANALYSIS.md -->
# Главная и синхронизация: gap analysis, 06.11.2026

До изменений Amadu-A/main, neo-term-it/main (проверен Git SSH) и локальный HEAD
совпадают: `677f1d2298cd9786ca927b876143afad0e6e57f4`. Единственный untracked файл —
предоставленный владельцем `renders/render_v2.png`. Правила shared/docs сверены по
актуальным SHA; сохранённые reference соответствуют main.

Regression baseline: Windows check-quality — 85 Python и 6 Node tests passed,
покрытие 80%. На сервере тот же чистый HEAD, API/worker работают, ready=200,
импорт завершён: 62 052 позиции, версия `d548a881-17a4-4d9a-9811-25bc6647a2a4`.
Это read-only диагностика, запусков/перезапусков или записи в рабочие данные не было.

| Область | Фактическое состояние | Требуемое дополнение |
|---|---|---|
| Сопоставление | JSON/UUID, Top-5, поиск, manual/null, revision, approved.json готовы | Сохранить экран и старый `?job=UUID`, выделить `/mapping` |
| Главная | Корень показывает сопоставление; настоящее меню из трёх JS-вкладок | Dashboard по render_v2, маршруты и deep links |
| KPI | Только totals временного job; история удаляется по TTL | Агрегаты без документов, раздельные matched/manual, знаменатели и coverage |
| Расписание | Интервал от last_success, флаги окружения | Постоянные настройки, календарные UTC-сутки, default disabled/15:00 |
| Запуск импорта | Durable jobs/outbox, lease, locks и resumable staging готовы | Координация manual/scheduled, дедупликация и ограниченный catch-up |
| Источник | Проверенный локальный XML через CatalogSource | HTTP full XML адаптер, приватный staging, SSRF/TLS/лимиты; явное ожидание контракта |
| История | Временный catalog job и метаданные последнего успеха | Долговечные технические события/отчёты с отдельной retention |
| Интеграции | Readiness только внутренних зависимостей | Безопасные probes extracting, honest HTTP 1С state, inbound активность telegram |
| Web управление | Импорт требует X-Catalog-Key, operator cookie нет | Короткая operator-сессия, одноразовый CLI ticket, CSRF/Origin, без выдачи ключей |
| БД/развёртывание | PostgreSQL/Qdrant persistent, миграции до 0002, порт 8087 | Только additive 0003, прежние volumes/alias/approval snapshots сохраняются |

Оператор получает полномочия через одноразовый короткоживущий ticket, созданный
локальной CLI по SSH. Ввод выполняется через HTTPS или localhost SSH-туннель.
CATALOG_ADMIN_KEY и credentials 1С не передаются в HTML/JS. GUI хранит только
credential reference; реальные secrets читаются Pydantic на сервере.

Публичные API-owner и web-owner остаются изолированы. Оператору доступны глобальные
агрегаты и метаданные заданий; права на чужой source/result/selection не расширяются.
После первой инициализации GUI-расписание читает только PostgreSQL; старые env-флаги
используются один раз как bootstrap с документированной UTC-семантикой.

HTTP 1С live остаётся blocked до утверждения URL, allowlist, метода, авторизации,
полного XML и признака завершённости. Никакие sample URL из рендера не считаются
рабочими настройками. Mock HTTP tests проверят адаптер и общий pipeline отдельно.
