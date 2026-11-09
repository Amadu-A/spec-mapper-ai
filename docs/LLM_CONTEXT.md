<!-- docs/LLM_CONTEXT.md -->
# Контекст разработки Spec Mapper AI

Сначала читать корневой [AGENTS.md](../AGENTS.md) и актуальное состояние Git.
Код — локальный FastAPI сервис; публикация Sites и облачные модели не используются.
Без команды владельца не выполнять commit/push и не менять shared stack.

Обязательные инструкции по профилю задачи:

- Backend, Settings, тесты: [Engineering](reference/ENGINEERING_GUIDELINES.md).
- HTML, CSS, JS: [Frontend полностью](reference/FRONTEND_GUIDELINES.md).
- Docker/скрипты: [Infrastructure](reference/INFRASTRUCTURE_INSTRUCTIONS.md).
- Общие зависимости: [services.yaml](reference/services.yaml).

Копии в `reference/` получены из `Amadu-A/shared_infrasktructure/docs`, ветка `main`,
05.11.2026. При изменении проверить их актуальность в исходном репозитории.
Исходный [router shared проекта](reference/LLM_CONTEXT.md) сохранён без изменений.

Проектные решения: [ТЗ](TECHNICAL_SPECIFICATION.md), [архитектура](architecture.md),
[API](API.md), [эксплуатация](operations.md). Все новые docstrings и документация — по-русски.
Исходная пятёрка JSON неизменна. Owner и TTL проверяются до выдачи результата.

Согласовано владельцем 05.11.2026: при конфликтующих кодах исключать **все** такие
записи и показывать warning. В текущем XML GUID отсутствует; внутренний UUID по коду
никогда не называется GUID 1С. Подключение реального API 1С требует отдельного контракта.

## Актуальные дополнения V2/V3

Главная `/`, прежний mapper `/mapping`, независимая operator роль без регистрации,
additive migration 0003 и PostgreSQL UTC scheduler. Точные контракты/границы и ожидание
HTTP 1С: [DASHBOARD_AND_SYNC](DASHBOARD_AND_SYNC.md). Не ослаблять owner isolation
ради общего dashboard; не сбрасывать существующий каталог/staging/volumes.

Кнопка выдачи operator-ticket доступна только через отдельный loopback gateway
сервера `127.0.0.1:8088` (`OPERATOR_PORT`), private-only, без ai-shared/Docker socket.
Нужны web owner, CSRF и подписанный 15-секундный proof с существующим SESSION_SECRET;
Host=localhost не доказывает SSH. UI скрывает инструкцию при доверенном входе,
пользователь вводит собственный SSH-логин. Прежний CLI ticket остаётся fallback.
Публичный порт 8087 сохраняется. Новые секреты и DB migrations не добавляются.

GUI refresh использует отдельный пользовательский `/catalog/refresh`: обычному
owner разрешён только подтверждённый сохранённый HTTP источник, оператору — также
local XML. Общий SQL cooldown 60 секунд, идемпотентный replay и revision CAS;
настройки/scheduler меняет только оператор. Прежний административный imports/CLI
сохранён без нового cooldown для bootstrap. Bulk approve подтверждает все текущие
выборы/null без нового автоподбора; DELETE selection очищает только соответствие,
сохраняя source, Top-K, owner/revision/TTL и immutable approved.json. В UI — SVG favicon.
