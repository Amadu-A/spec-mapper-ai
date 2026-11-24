<!-- docs/INVOICE_STAGE_02_GAP_ANALYSIS.md -->
# Этап 02: исходная проверка и границы изменений

23.11.2026: Mapper main/HEAD и GitHub `b607270839ee03d75b17c7241f710dc88303f48e`,
Extracting main/HEAD и GitHub `9f4c911642a868cf4227ddedaec4ff6b919b6e5d`,
shared-infrastructure main/HEAD и GitHub `dee0a2a17319a24d565d5ba1cabd4b7d477c1d74`.
Тексты обязательных shared docs совпадают с reference после удаления конечной пустой строки.
Рабочий код перед изменением чистый; пользовательский незакоммиченный файл ТЗ сохранён.
GitHub connector не видит зеркало neo-term-it (404); отдельный `git ls-remote origin main`
через SSH подтвердил тот же `b607270`. У origin два push URL: основной и зеркало;
настройка remote не изменялась.

Исходный Windows gate: Ruff/format, 268 pytest (12 live исключены), Node успешны.
Первоначальный SSH timeout устранён владельцем восстановлением VPN. Реальная проверка
выполнена отдельно от mocks: PostgreSQL, RabbitMQ, Extracting/VLM, embedding и Qdrant.
Перенос семи PDF и семи JSON разрешён владельцем только для временного QA с последующим удалением.

| Область | Пробел | Изменение |
|---|---|---|
| Вход/worker | Только один PDF спецификации и плоский source.items | Отдельный invoice kind, несколько blobs, checkpoint upstream, lossless адаптер |
| Сопоставление | Общий engine уже реализован | Одна функция batched matching для обоих профилей; специальные ограничения платежей/услуг |
| Review/approval | CAS/lease/TTL/snapshot реализованы, snapshot плоский | Параметр профиля/экспортера общих use-cases; только invoice financial acknowledgment |
| Хранение | specification_uploads хранит один PDF | Additive 0006 invoice_uploads, общая транзакция create/outbox, прежний TTL |
| API/UI | Только спецификации | Независимый namespace и экран счетов, общие auth/selection/catalog/компоненты |
| Экспорт | Шаблон спецификаций | Invoice JSON v1 и финансовые листы из одного immutable snapshot |

Новые сервисы, каталог, векторная коллекция, модель и worker loop не создаются.
Изменения ограничены Mapper. Финансовые данные и исходный JSON не нормализуются.

## Совместимость фактического источника

Версия DTO Extracting содержит `payment_terms`, `field_origin`, `derived_fields`,
технические страницы и другие расширения; они учтены целиком. В трёх сохранённых JSON
поле `notes` — строка, хотя текущий DTO 8083 описывает список. Mapper принимает обе
формы без исправления исходного JSON; версия схемы, деньги, даты и счётчики проверяются строго.
Подробные результаты и границы live-проверок — в [отчёте](INVOICE_STAGE_02_REPORT.md).
