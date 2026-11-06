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
