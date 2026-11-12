<!-- docs/V4_GAP_ANALYSIS.md -->
# Анализ перед V4 — 11.11.2026

Локальное дерево чистое; HEAD и GitHub main Mapper: `ec26642e06d28947a752f960dd1312a62c84c34f`.
Extracting: `9844c4ee68fbd35840b0e5bb634ab6a5eef0879e`;
shared infrastructure: `839d419432fc25bc988e5871f7e53f6d282a2e77`.
Фактический Export Extracting проверен по `app/domain/models.py`: file_name и восемь строковых полей.
Копии общих инженерных правил совпадают с текущими файлами shared/docs.
Базовая Windows-проверка: 163 Python tests, 13 Node tests, Ruff/format успешно.

| Область | Пробел | Изменение в существующей архитектуре |
| --- | --- | --- |
| HTTP 1С | Только полный XML | JSON parser рядом с XML, общий защищённый download и pipeline |
| Полнота | Completion marker есть, shrink guard отсутствует | Проверка JSON, лимитов и сокращения до staging |
| Идентификаторы | code UUID и metadata hash одновременно определяют вектор | Постоянное соответствие real GUID → internal UUID, отдельный semantic fingerprint |
| Изменения 1С | Нет event inbox | Additive 0004, durable события и существующий job outbox/lease/RabbitMQ |
| Публикация | Существуют lock/staging/alias/rollback | Один coalesced batch использует тот же SynchronizeCatalogUseCase |
| Спецификации | Строгая исходная пятёрка | Два входных формата, дословный source и расширенные производные представления |
| UI/экспорты | Нет новых полей и событий | Безопасное отображение, JSON/Excel, операторский журнал событий |

Сохраняются scheduler, cooldown, Top-5, null, массовое утверждение, immutable approved snapshots,
owner isolation, TTL, именованные ключи, loopback gateway и порты 8087/8088.
Реальный контракт 1С требует completion marker, безопасного транспорта и отдельной сервисной
учётной записи. До подтверждения — `blocked_by_external_1c`; mock-проверки обозначаются отдельно.
