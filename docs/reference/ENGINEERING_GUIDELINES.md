<!-- docs/ENGINEERING_GUIDELINES.md -->

# Engineering Guidelines: Architecture, Code Style, Frontend and Configuration

> **Назначение:** единый набор инженерных правил для проектов, которые проектируются или сопровождаются с использованием `shared-infrastructure`.
>
> Документ предназначен как для разработчиков, так и для LLM / AI coding agents.
>
> Ключевые слова:
>
> - **MUST** — обязательное правило;
> - **SHOULD** — правило по умолчанию, отклонение требует понятной причины;
> - **MAY** — допустимый вариант.

---

## 1. Главный принцип

Архитектура проекта должна обеспечивать:

- явные границы ответственности;
- направление зависимостей от внешних деталей к бизнес-логике, а не наоборот;
- заменяемость инфраструктурных реализаций;
- тестируемость use-case'ов без реальной БД, HTTP, очередей, файлового хранилища или LLM;
- минимальную связанность между слоями;
- единообразие структуры между проектами;
- отсутствие бизнес-логики в transport/UI и infrastructure слоях;
- отсутствие секретов в Git;
- предсказуемое подключение shared infrastructure.

LLM MUST сначала определить границы приложения и зависимости, а уже потом предлагать структуру файлов или код.

---

# Часть I. Архитектура backend

## 2. Рекомендуемые слои

Для нетривиального backend-проекта SHOULD использоваться следующая логическая модель:

```text
┌───────────────────────────────────────┐
│ Transport / Delivery                 │
│ FastAPI / Django / CLI / worker      │
└──────────────────┬────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────┐
│ Application                           │
│ use-cases / orchestration / DTO       │
│ ports / interfaces                    │
└──────────────────┬────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────┐
│ Domain                                │
│ entities / value objects / rules      │
│ domain exceptions                     │
└───────────────────────────────────────┘

Infrastructure ───────implements──────► Application ports
DB / MinIO / RabbitMQ / vLLM / SMTP / external APIs
```

Допустим более простой проект с тремя физическими слоями:

```text
transport -> services/use_cases -> repositories
```

Но даже в таком проекте MUST сохраняться то же направление зависимостей.

### 2.0. Обязательное указание относительного пути файла

При создании, изменении или демонстрации файла MUST быть явно указан его
относительный путь от корня проекта.

Для файлов, синтаксис которых позволяет комментарий, относительный путь
MUST также указываться первой допустимой строкой комментария.
Shebang и обязательные заголовки формата сохраняют своё место перед этим комментарием.

Пример Python:

```python
# app/application/use_cases/analyze_document.py

from __future__ import annotations
```

Пример JavaScript:

```javascript
// app/web/static/js/analysis.js

...
```

Пример CSS:

```css
/* app/web/static/css/blocks/_analysis.css */

...
```

Для файлов, где комментарий в содержимом невозможен или нежелателен
(например, JSON), путь MUST быть указан непосредственно перед code block:

```text
Файл: config/settings.json
```

LLM MUST NOT показывать несколько безымянных code blocks, если из ответа
неочевидно, в какие файлы их сохранять.

Если пользователь просит полный файл, LLM MUST показывать полное актуальное
содержимое файла, а не только fragment/diff.

---

### 2.1. Transport / Delivery

Примеры:

```text
api/
views/
web/
cli/
workers/
```

Transport отвечает за:

- HTTP routing;
- parsing запроса;
- transport-level validation;
- authentication / authorization context;
- cookies / headers / CSRF;
- mapping входных схем в command/DTO;
- вызов application use-case;
- преобразование результата или исключения в HTTP/HTML/CLI response.

Transport MUST NOT:

- выполнять SQL;
- содержать сложные бизнес-правила;
- выбирать алгоритм бизнес-операции;
- самостоятельно создавать глубокий граф зависимостей;
- дублировать одну и ту же бизнес-логику между HTML route и API route.

Если одна операция нужна HTML странице и API:

```text
HTML router ─┐
             ├──> same application use-case
API router ──┘
```

---

## 3. Application layer

Application layer описывает **что система делает**, но не **какой конкретно драйвер это выполняет**.

Примеры:

```text
application/
├── dto/
├── ports/
├── services/
└── use_cases/
```

Use-case SHOULD соответствовать одной законченной пользовательской или системной операции:

```text
UploadDocument
AnalyzeContract
CreateUser
CommitReview
GenerateReport
```

Application layer MAY:

- оркестрировать несколько репозиториев;
- применять правила процесса;
- обращаться к портам внешних систем;
- формировать application DTO;
- управлять атомарностью use-case;
- инициировать domain operations.

Application layer MUST NOT зависеть от:

- FastAPI `Request`, `Response`, `HTTPException`;
- Jinja2;
- конкретного RabbitMQ/MinIO/vLLM клиента;
- конкретного SMTP клиента;
- HTTP framework-specific объектов;
- CSS/JS/UI;
- прямых SQL-запросов.

---

## 4. Domain layer

Domain — наиболее независимая часть проекта.

В domain SHOULD находиться:

- entities;
- enums;
- value objects;
- чистые бизнес-правила;
- domain exceptions;
- вычисления, не требующие инфраструктуры.

Domain MUST NOT импортировать:

```text
FastAPI
Django HTTP layer
SQLAlchemy session
Celery
RabbitMQ client
MinIO client
vLLM client
requests/httpx
Jinja2
```

ORM-модели MAY быть отделены от domain-моделей в более строгой архитектуре.

Для небольших CRUD-проектов допустимо упростить модель, но бизнес-правила всё равно не должны зависеть от HTTP.

---

## 5. Infrastructure layer

Infrastructure содержит конкретные технические реализации:

```text
infrastructure/
├── database/
│   ├── models/
│   ├── repositories/
│   └── migrations/
├── messaging/
├── object_storage/
├── llm/
├── embeddings/
├── vector_store/
├── email/
└── external_api/
```

Infrastructure MAY зависеть от сторонних библиотек и SDK.

Infrastructure SHOULD реализовывать интерфейсы, объявленные со стороны application/domain.

Пример:

```python
# application/ports/object_storage.py

from typing import BinaryIO, Protocol


class ObjectStorage(Protocol):
    async def put(
        self,
        *,
        object_name: str,
        data: BinaryIO,
        size: int,
    ) -> None: ...

    async def delete(
        self,
        *,
        object_name: str,
    ) -> None: ...
```

```python
# infrastructure/object_storage/minio.py

class MinioObjectStorage:
    ...
```

Application знает `ObjectStorage`.

Application не должен быть обязан знать `MinioObjectStorage`.

---

# Часть II. SOLID и Dependency Injection

## 6. Dependency Inversion — обязательное правило

Высокоуровневая бизнес-логика MUST зависеть от контрактов, а не от конкретных реализаций.

Хорошо:

```python
class ReviewUseCase:
    def __init__(
        self,
        *,
        item_repository: ItemRepository,
        label_repository: LabelRepository,
    ) -> None:
        self._item_repository = item_repository
        self._label_repository = label_repository
```

Плохо:

```python
class ReviewUseCase:
    def __init__(self) -> None:
        self._item_repository = SqlAlchemyItemRepository()
```

И особенно плохо:

```python
async def execute(...):
    repository = SqlAlchemyItemRepository(...)
    client = Minio(...)
    llm = VllmClient(...)
```

Use-case не должен сам собирать свою инфраструктуру.

---

## 7. Где объявлять интерфейсы

Интерфейс SHOULD находиться у потребителя зависимости.

Рекомендуется:

```text
application/ports/
```

или для простой структуры:

```text
services/interfaces/
repositories/interfaces/
```

Не рекомендуется помещать интерфейс только рядом с конкретной реализацией, если из-за этого application вынужден импортировать infrastructure.

### 7.1. Маленькие интерфейсы

Соблюдаем Interface Segregation Principle.

Плохо:

```python
class Repository(Protocol):
    async def get_user(...): ...
    async def save_user(...): ...
    async def get_document(...): ...
    async def save_document(...): ...
    async def search_vectors(...): ...
    async def send_email(...): ...
```

Хорошо:

```text
UserRepository
DocumentRepository
VectorSearch
EmailSender
ObjectStorage
TaskPublisher
```

Интерфейс должен описывать одну связанную ответственность.

---

## 8. Composition Root

Создание конкретных реализаций MUST быть сосредоточено в явной точке сборки зависимостей.

Для FastAPI рекомендуется:

```text
api/dependencies.py
core/container.py
core/dependencies.py
bootstrap.py
```

Роутер SHOULD получать уже собранный use-case:

```python
@router.post("/documents")
async def upload_document(
    command: UploadCommand,
    use_case: Annotated[
        UploadDocumentUseCase,
        Depends(get_upload_document_use_case),
    ],
):
    return await use_case.execute(command)
```

Плохо создавать набор репозиториев в каждом endpoint:

```python
@router.post("/documents")
async def upload_document(session=Depends(get_session)):
    document_repo = DocumentRepository(session)
    user_repo = UserRepository(session)
    storage = MinioStorage(...)
    service = UploadService(
        document_repo=document_repo,
        user_repo=user_repo,
        storage=storage,
    )
```

Допустимо, что composition root использует framework-specific DI.

Application layer при этом остаётся framework-agnostic.

---

## 9. SOLID в практических правилах

### S — Single Responsibility

Класс или модуль SHOULD иметь одну основную причину для изменения.

Признаки нарушения:

- один service одновременно валидирует файл, делает SQL, вызывает LLM и строит HTTP response;
- один JS-файл содержит API client, работу с DOM, бизнес-state и форматирование всех страниц;
- один CSS-файл содержит весь проект.

### O — Open/Closed

Новая инфраструктурная реализация SHOULD подключаться без переписывания use-case.

Пример:

```text
ObjectStorage
├── MinioObjectStorage
└── S3ObjectStorage
```

### L — Liskov Substitution

Fake/mock implementation MUST вести себя в рамках того же контракта.

Если fake нельзя подставить вместо production adapter без изменения use-case, контракт спроектирован плохо.

### I — Interface Segregation

Не создавать «god interface».

Порт должен быть минимальным и предметным.

### D — Dependency Inversion

Application зависит от abstraction.

Infrastructure зависит от application contract.

Не наоборот.

---

# Часть III. Работа с БД

## 10. SQLAlchemy только в Data Access / Infrastructure

Правило SHOULD формулироваться не как привязка к имени папки `src/crud`, а как архитектурная граница.

SQLAlchemy queries MUST находиться только в:

```text
infrastructure/database/repositories/
```

или, в упрощённом проекте:

```text
src/crud/
src/repositories/
```

Разрешено:

```python
select(...)
insert(...)
update(...)
delete(...)
session.execute(...)
func.count(...)
```

только внутри data-access/infrastructure слоя.

Запрещено в:

```text
api/
views/
application/
services/
domain/
```

Исключения:

- Alembic migrations;
- административные одноразовые scripts;
- maintenance/migration utilities.

Исключение MUST быть очевидно по расположению файла.

---

## 11. Repository contract

Repository должен выражать бизнес-нужную операцию, а не просто прятать SQLAlchemy API.

Хорошо:

```python
class DocumentRepository(Protocol):
    async def get_by_id(
        self,
        tenant_id: UUID,
        document_id: UUID,
    ) -> Document | None: ...

    async def add(
        self,
        document: Document,
    ) -> None: ...
```

Не стоит делать абстракцию вида:

```python
async def execute(self, statement: Any) -> Any:
    ...
```

Такая абстракция фактически протаскивает SQL наружу.

---

## 12. N+1 и загрузка данных

Repository layer отвечает за стратегию загрузки ORM-данных.

Transport и application не должны исправлять N+1 вручную.

Для SQLAlchemy использовать осознанно:

```text
selectinload
joinedload
contains_eager
```

Выбор зависит от cardinality и объёма данных.

Любая новая выборка, которая проходит по связанным объектам в цикле, SHOULD проверяться на N+1.

---

# Часть IV. Транзакции и Unit of Work

## 13. Граница транзакции

Не фиксируем правило «транзакцию всегда открывает HTTP router».

HTTP-запрос и бизнес-транзакция — разные понятия.

Граница атомарности SHOULD соответствовать application use-case.

Пример:

```text
POST /document
    ↓
UploadDocumentUseCase
    ↓
атомарно:
    document
    analysis_job
    outbox_event
```

Router не обязан знать, какие именно записи должны быть атомарны.

---

## 14. Рекомендуемый Unit of Work

Для сложных операций SHOULD использоваться Unit of Work.

```python
class UnitOfWork(Protocol):
    documents: DocumentRepository
    analyses: AnalysisRepository

    async def __aenter__(self) -> "UnitOfWork": ...
    async def __aexit__(self, *args: object) -> None: ...

    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
```

Use-case:

```python
class UploadDocumentUseCase:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        storage: ObjectStorage,
    ) -> None:
        self._uow = uow
        self._storage = storage

    async def execute(self, command: UploadCommand) -> UploadResult:
        async with self._uow:
            ...
            await self._uow.documents.add(document)
            await self._uow.analyses.add(job)
            await self._uow.commit()

        return result
```

Это даёт:

- правильную границу атомарности;
- тестируемость;
- отсутствие SQLAlchemy в application;
- возможность заменить persistence mechanism.

Для маленького проекта допускается упрощённая схема, но commit/rollback не должен случайно расползаться по множеству репозиториев.

---

# Часть V. DTO, validation и exceptions

## 15. Pydantic

Pydantic SHOULD использоваться на внешних границах:

- API request;
- API response;
- settings;
- parsing external payload.

Application/domain не обязаны зависеть от Pydantic.

Для внутренних DTO допустимы:

```text
dataclass
NamedTuple
plain typed class
Pydantic model — если это осознанно упрощает проект
```

---

## 16. Validation

Разделять:

### Transport validation

Пример:

```text
поле обязательно
строка не длиннее N
UUID корректный
файл имеет допустимый MIME type
```

### Domain/Application validation

Пример:

```text
документ нельзя анализировать повторно в текущем статусе
пользователь не может изменить объект другого tenant
операция недоступна после закрытия сессии
```

Бизнес-правила MUST NOT существовать только в Pydantic validator HTTP-схемы.

---

## 17. Exceptions

Domain/application SHOULD выбрасывать предметные исключения:

```text
DocumentNotFoundError
AnalysisAlreadyRunningError
PermissionDeniedError
InvalidReviewStateError
```

Transport переводит их в:

```text
404
409
403
422
```

Application MUST NOT выбрасывать `HTTPException`.

---

# Часть VI. Python code style

## 18. Общий стиль

Python code MUST соответствовать PEP 8.

Имена:

```text
functions / variables / modules -> snake_case
classes                       -> PascalCase
constants                     -> UPPER_CASE
```

Публичные application/repository methods MUST иметь type hints.

`Any` SHOULD использоваться только на внешней или динамической границе и не должен распространяться по application layer.

---

## 18.1. Документация каждого файла — обязательна

Каждый source/config/script/template файл проекта MUST быть понятно
задокументирован **на русском языке**.

Документация должна отвечать минимум на вопросы:

```text
зачем существует этот файл;
за какую ответственность он отвечает;
какому слою / feature он принадлежит;
с какими ключевыми компонентами взаимодействует;
какие важные ограничения или side effects у него есть.
```

Документация MUST объяснять назначение, а не механически пересказывать код.

Плохо:

```python
"""Файл с функциями."""
```

Хорошо:

```python
# app/application/use_cases/analyze_document.py

"""
Use-case запуска анализа документа.

Модуль координирует получение документа, проверку допустимости запуска
анализа и публикацию фоновой задачи. Он не содержит SQL и не знает
о конкретной реализации брокера.
"""
```

Для Python основной способ документирования файла — module docstring сразу
после комментария с относительным путём.

Для JavaScript/TypeScript SHOULD использоваться верхний block comment/JSDoc.

Для CSS, YAML, shell scripts и templates SHOULD быть верхний комментарий,
который объясняет назначение файла и нетривиальные ограничения.

Для форматов, где комментарии синтаксически запрещены, например strict JSON,
документация MUST находиться в ближайшем валидном source of truth:
README, schema, соседнем Markdown-файле или документации компонента.

Комментарии MUST поддерживаться в актуальном состоянии при изменении
ответственности файла.

---

## 18.2. Документация функций, методов и классов

Каждая созданная в проекте функция, метод и класс MUST иметь содержательную
документацию **на русском языке**.

Для Python MUST использоваться docstring.

Минимальная документация объясняет:

```text
зачем существует функция/класс;
какую ответственность выполняет;
что принимает;
что возвращает;
какие значимые side effects имеет;
какие ожидаемые исключения может выбросить;
какие бизнес-ограничения важно понимать вызывающему коду.
```

Для простой функции допустим короткий docstring:

```python
def normalize_filename(filename: str) -> str:
    """Нормализует имя файла перед безопасным сохранением в object storage."""
```

Для application/service operation нужен более содержательный docstring:

```python
class AnalyzeDocumentUseCase:
    """
    Запускает анализ ранее загруженного документа.

    Use-case проверяет текущее состояние документа, не допускает повторный
    параллельный запуск анализа и публикует фоновую задачу через application
    port. Конкретный RabbitMQ/Celery client здесь не создаётся.
    """

    async def execute(self, document_id: UUID) -> AnalysisJob:
        """
        Создаёт задание анализа для документа.

        Args:
            document_id: Идентификатор документа текущего tenant.

        Returns:
            Созданное задание анализа.

        Raises:
            DocumentNotFoundError: Документ не найден.
            AnalysisAlreadyRunningError: Анализ документа уже выполняется.
        """
```

Документация MUST объяснять **почему и для чего**, если это неочевидно из
имени. Бессмысленные docstrings вида:

```text
"Выполняет функцию."
"Создаёт класс."
"Возвращает результат."
```

запрещены.

Комментарии внутри функции SHOULD объяснять причину нетривиального решения,
инвариант, workaround или ограничение.

Не нужно комментировать очевидную механику построчно:

```python
# Увеличиваем i на 1.
i += 1
```

При изменении поведения функции/class её docstring MUST обновляться в том же PR.

---

## 19. Размер функций и классов

Не вводится искусственный жёсткий лимит строк.

Вместо него правило:

> Если функция выполняет несколько логически независимых этапов, этапы SHOULD быть вынесены в отдельные функции или объекты.

Признаки необходимости декомпозиции:

- много уровней вложенности;
- несколько разных `try/except`;
- функция одновременно занимается I/O и бизнес-вычислениями;
- имя функции невозможно сформулировать одним действием;
- тест требует слишком много несвязанных fixture.

---

## 20. Imports

Импорты SHOULD группироваться:

```python
# standard library

# third party

# local application
```

Запрещены wildcard imports:

```python
from module import *
```

Циклические импорты SHOULD исправляться архитектурой, а не массовым переносом imports внутрь функций.

---

## 21. Mutable defaults

Запрещено:

```python
def foo(items=[]):
    ...
```

Использовать:

```python
def foo(items: list[str] | None = None):
    items = [] if items is None else items
```

Для dataclass:

```python
field(default_factory=list)
```

---

## 22. Async code

В async path MUST отсутствовать блокирующий I/O без явного offloading.

Проверять:

```text
requests
subprocess.run
time.sleep
sync DB drivers
тяжёлые CPU операции
```

Вместо этого:

```text
httpx.AsyncClient
async DB driver
asyncio.sleep
worker/thread/process pool для CPU/blocking
```

---

# Часть VII. Logging и observability

## 23. Основной принцип логирования

Логирование MUST помогать ответить на вопросы:

```text
что произошло;
в каком service/use-case;
с каким correlation/request/job id;
успешно ли завершилась операция;
сколько времени она заняла;
какая ошибка произошла;
```

Application services SHOULD писать logs в stdout/stderr.

Не создавать собственные бесконечно растущие `.log` файлы внутри container
без отдельной инфраструктурной причины.

Логи SHOULD быть структурированными.

Минимально рекомендуемые поля:

```text
timestamp
level
event
service
operation
request_id / correlation_id
user_id / tenant_id — только если допустимо
job_id / task_id
duration_ms
status
```

Стабильное поле `event` предпочтительнее свободного текста.

Пример:

```python
logger.info(
    "review_committed",
    extra={
        "event": "review_committed",
        "session_id": str(session_id),
        "rows": len(rows),
        "status": "success",
    },
)
```

Если выбран JSON logger, формат MUST быть единым для всего service.

---

## 23.1. Обязательное измерение времени service operations

Каждая значимая публичная операция application service/use-case MUST
логировать время выполнения через единый reusable decorator.

К значимым операциям относятся, например:

```text
AnalyzeDocumentUseCase.execute
UploadDocumentUseCase.execute
GenerateReportUseCase.execute
ImportKnowledgeBase.execute
ML/LLM inference orchestration
длительная обработка файла
внешняя интеграция, latency которой важна для эксплуатации
```

Декоратор MUST использовать monotonic timer:

```text
time.perf_counter()
```

а не wall-clock subtraction через `datetime.now()`.

В log MUST присутствовать:

```text
event=operation_timing
operation=<stable operation name>
duration_ms=<number>
status=success|error
```

Пример базовой реализации:

```python
# app/core/observability.py

"""
Инструменты наблюдаемости для измерения времени ключевых операций.

Модуль предоставляет единый timing-декоратор для application services.
Он пишет одну итоговую запись на вызов и не дублирует traceback исключений.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from functools import wraps
from time import perf_counter
from typing import Any, ParamSpec, TypeVar, cast

P = ParamSpec("P")
R = TypeVar("R")


def log_execution_time(
    *,
    operation: str,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Создаёт декоратор для измерения времени ключевой операции service/use-case.

    Декоратор пишет ровно одно событие `operation_timing` после завершения
    вызова. При исключении он фиксирует `status=error`, но не пишет traceback:
    traceback должен логироваться один раз в слое, который отвечает за
    обработку необработанной ошибки.

    Args:
        operation: Стабильное имя операции для поиска и агрегации логов.

    Returns:
        Декоратор, сохраняющий интерфейс исходной функции.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        """Оборачивает sync или async callable единым timing-логированием."""

        logger = logging.getLogger(func.__module__)

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                """Измеряет время async-операции и пишет одно итоговое событие."""

                started_at = perf_counter()
                status = "error"

                try:
                    result = await func(*args, **kwargs)
                    status = "success"
                    return result
                finally:
                    duration_ms = (perf_counter() - started_at) * 1000
                    logger.info(
                        "operation_timing",
                        extra={
                            "event": "operation_timing",
                            "operation": operation,
                            "duration_ms": round(duration_ms, 2),
                            "status": status,
                        },
                    )

            return cast(Callable[P, R], async_wrapper)

        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            """Измеряет время sync-операции и пишет одно итоговое событие."""

            started_at = perf_counter()
            status = "error"

            try:
                result = func(*args, **kwargs)
                status = "success"
                return result
            finally:
                duration_ms = (perf_counter() - started_at) * 1000
                logger.info(
                    "operation_timing",
                    extra={
                        "event": "operation_timing",
                        "operation": operation,
                        "duration_ms": round(duration_ms, 2),
                        "status": status,
                    },
                )

        return sync_wrapper

    return decorator
```

Отмена async-операции не является успехом: CancelledError продолжает
распространяться, итоговый status остаётся error.

Применение:

```python
# app/application/use_cases/analyze_document.py

class AnalyzeDocumentUseCase:
    """Оркестрирует запуск анализа документа."""

    @log_execution_time(operation="document_analysis")
    async def execute(self, command: AnalyzeDocumentCommand) -> AnalysisResult:
        """Выполняет основной use-case анализа документа."""
        ...
```

Имя operation MUST быть стабильным и не содержать идентификаторы пользователя,
UUID, filename и другие high-cardinality values.

---

## 23.2. Не превращать timing-декоратор в источник log spam

Timing decorator MUST применяться к **значимым service/use-case operations**,
а не механически к каждой функции проекта.

По умолчанию НЕ декорировать:

```text
маленькие pure helpers;
property/getter;
простые mapper-функции;
каждый repository CRUD method;
функции внутри tight loop;
вложенные функции одной уже измеряемой операции.
```

Если `AnalyzeDocumentUseCase.execute()` уже измеряет полную операцию, не нужно
без причины создавать ещё десять одинаковых timing events для каждого
внутреннего шага.

Дополнительное измерение нижнего уровня допустимо, если latency этого шага
важна отдельно:

```text
shared_vlm_inference
qdrant_search
document_ocr
external_api_request
report_render
```

При этом имя события должно позволять отличить общий use-case от dependency
operation.

---

## 23.3. Одна ошибка — один traceback

Одна и та же exception MUST NOT записываться с traceback на каждом слое.

Плохо:

```text
repository logger.exception(...)
service logger.exception(...)
router logger.exception(...)
global middleware logger.exception(...)
```

для одной ошибки.

Это создаёт четыре визуально разные ошибки вместо одной.

Правило:

- ожидаемые domain/application exceptions логируются на подходящем уровне без
  лишнего traceback либо вообще преобразуются в ожидаемый response;
- unexpected exception SHOULD иметь полный traceback ровно в одном
  ответственном error boundary;
- timing decorator фиксирует только `status=error` и `duration_ms`;
- верхний HTTP/worker error handler MAY записать traceback один раз;
- повторно логировать exception можно только если появляется новая полезная
  информация или сформирована новая ошибка.

---

## 23.4. Уровни логирования

Использовать уровни последовательно:

```text
DEBUG
    диагностические детали, отключаемые в production

INFO
    успешные значимые business/system events
    lifecycle
    operation_timing

WARNING
    восстановимая проблема
    retry
    degraded behavior
    неожиданное, но обработанное состояние

ERROR
    операция не выполнена
    требуется анализ

CRITICAL
    service не способен корректно продолжать работу
```

Не использовать `ERROR` для обычной validation/business ситуации, если она
является ожидаемой частью API contract.

---

## 23.5. Что нельзя логировать

MUST NOT попадать в logs:

```text
password
SECRET_KEY
JWT
refresh token
session cookie
SMTP password
RabbitMQ password
API key
полный Authorization header
полные credentials URL
полный пользовательский документ без явной необходимости
персональные данные без эксплуатационной необходимости
```

Перед логированием external payload SHOULD использоваться allow-list нужных
полей, а не dump всего объекта.

---

## 23.6. Ограниченный срок хранения и ротация

Логи MUST иметь ограниченный размер и/или срок хранения.

Запрещена конфигурация, при которой application/container log может расти
бесконечно до заполнения диска.

Для Docker Compose application services рекомендуемый baseline:

```yaml
# compose.yaml

services:
  api:
    logging:
      driver: local
      options:
        max-size: "10m"
        max-file: "5"
```

`local` driver предпочтителен для простого single-host deployment, если
централизованная logging platform не требует другой driver.

Если используется `json-file`, rotation MUST быть включена явно:

```yaml
# compose.yaml

services:
  api:
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
```

Размеры `10m` и `5` — baseline, а не универсальное требование.
Проект MAY выбрать другие значения исходя из traffic, disk capacity и
incident-response требований, но retention MUST оставаться конечным.

Docker log files MUST обслуживаться Docker logging driver.
Нельзя писать cron/script, который вручную удаляет активные файлы Docker logs
из `/var/lib/docker/...`.

### 23.6.1. Если service пишет в обычный файл вне Docker

Если по архитектурной причине service работает вне container и пишет logs
непосредственно в файл, обычный `FileHandler` без rotation запрещён.

MUST использоваться один из bounded вариантов:

```text
logging.handlers.RotatingFileHandler
logging.handlers.TimedRotatingFileHandler
system logrotate
journald с ограниченной retention policy
```

Пример time-based rotation:

```python
# app/core/logging_config.py

"""
Конфигурация файлового логирования для non-container deployment.

Файл включает ежедневную ротацию и автоматически удаляет архивы старше
заданного количества backup-файлов, чтобы logs не заполняли диск.
"""

from logging.handlers import TimedRotatingFileHandler


handler = TimedRotatingFileHandler(
    filename="logs/service.log",
    when="midnight",
    interval=1,
    backupCount=14,
    encoding="utf-8",
    utc=True,
)
```

`backupCount` MUST быть больше нуля.

Если retention регулируется внешним `logrotate`, application MUST NOT
одновременно применять конфликтующую внутреннюю rotation policy.

---

## 23.7. Централизованные logs

Если используется Loki, Elasticsearch/OpenSearch, Graylog, cloud logging или
другая централизованная система:

- retention MUST быть настроен явно;
- срок хранения MUST соответствовать требованиям проекта;
- debug logs SHOULD храниться меньше production operational logs;
- sensitive fields MUST фильтроваться до отправки;
- high-cardinality labels MUST NOT включать request_id/user_id/document_id,
  если это создаёт взрыв cardinality;
- application не должна одновременно бесконтрольно хранить полную копию logs
  локально и в централизованном storage.

Рекомендуемый baseline, если у проекта нет специальных требований:

```text
local rotated container logs: 50–100 MB максимум на container
centralized dev/stage retention: 7–14 дней
centralized production retention: 30 дней
```

Это SHOULD быть переопределено, если есть compliance/audit требования.

---

## 23.8. Защита от бесконечного множества логов

Для high-frequency paths SHOULD применяться один или несколько механизмов:

```text
aggregation;
sampling;
rate limiting;
метрики вместо log на каждое событие;
один summary log после batch;
лог только при изменении состояния;
```

Плохо:

```text
логировать каждый item внутри обработки 100 000 строк;
логировать heartbeat каждую секунду на INFO;
логировать polling "ничего не найдено" бесконечно;
дублировать request start/request end на нескольких слоях.
```

Лучше:

```text
batch_completed rows=100000 duration_ms=...
polling_summary attempts=60 events_found=0
worker_heartbeat — DEBUG либо metric
```

Для массовых циклов SHOULD логироваться summary:

```text
processed_count
failed_count
skipped_count
duration_ms
```

а подробности отдельных failed items — только если они нужны для диагностики.

---

# Часть VIII. Конфигурация: `.env.example` + `.env`

## 25. Главный принцип конфигурации

Используем модель:

```text
.env.example = committed baseline + полный каталог переменных
.env         = sparse local/private override
```

`.env.example` хранится в Git.

`.env` MUST находиться в `.gitignore`.

`.env` MUST NOT копировать весь `.env.example`.

---

## 26. Что хранить в `.env.example`

В `.env.example` SHOULD находиться:

- все поддерживаемые имена переменных;
- безопасные non-secret defaults;
- dev defaults;
- порты;
- hostnames;
- feature flags с безопасным значением;
- параметры batching/timeouts;
- пустые или явно фиктивные placeholders для secret values;
- комментарии к нетривиальным параметрам.

Пример:

```dotenv
APP_ENV=dev
APP_HOST=0.0.0.0
APP_PORT=8000
LOG_LEVEL=INFO

POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=app

SECRET_KEY=CHANGE_ME
POSTGRES_PASSWORD=CHANGE_ME
```

---

## 27. Что хранить в `.env`

`.env` SHOULD содержать только значения, которые нельзя или не следует фиксировать в Git.

Обычно это:

```dotenv
SECRET_KEY=...
POSTGRES_PASSWORD=...
SMTP_PASSWORD=...
RABBITMQ_PASSWORD=...
APP_ENV=prod
```

Допустимы и другие environment-specific overrides, если конкретное окружение действительно отличается от baseline.

Главное правило:

> Добавление обычной non-secret настройки в проект НЕ должно требовать ручного дописывания этой настройки в каждый существующий `.env`.

---

## 28. Pydantic Settings: обязательный паттерн

Для Python-проектов на `pydantic-settings` SHOULD использоваться layered loading:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(
            ".env.example",
            ".env",
        ),
        env_nested_delimiter="__",
        case_sensitive=False,
    )
```

Порядок:

```text
.env.example
      ↓
.env overrides only changed/private values
      ↓
process environment may override deployment values
```

Таким образом новая обычная переменная добавляется:

1. в Settings model;
2. в `.env.example`;

и не требует массового обновления `.env`.

---

## 29. Важное правило именования

Стандартное имя:

```text
.env.example
```

Опечатки вроде:

```text
.env.examlpe
.env_example
example.env
```

не использовать.

Инструменты, документация, CI и разработчики должны видеть одно каноническое имя.

---

## 30. Docker Compose и env-файлы

Нельзя предполагать, что Docker Compose автоматически применит тот же механизм, что Pydantic.

Для Compose-only проекта MUST быть явно определён один из вариантов:

### Вариант A — defaults в Compose

```yaml
environment:
  LOG_LEVEL: ${LOG_LEVEL:-INFO}
  DB_PASSWORD: ${DB_PASSWORD:?DB_PASSWORD must be set}
```

Тогда `.env` может быть sparse и содержать только обязательные secrets/overrides.

### Вариант B — явное layered env loading

Если `.env.example` должен быть реальным runtime baseline, все команды/скрипты должны одинаково использовать layered env files.

Нельзя иметь ситуацию, когда:

```text
локальный script читает два env-файла,
а docker compose up читает только один.
```

Поведение MUST быть одинаковым для:

```text
bootstrap
config validation
pull
up
test
CI
production deployment
```

---

## 31. Secrets в production

`.env` допустим как server-local secret override для небольшого deployment.

Если окружение предоставляет secret manager / Docker secrets / Kubernetes Secrets / CI secret storage, production SHOULD использовать их вместо хранения секретов в обычном файле.

Application code при этом не меняется: источник конфигурации остаётся внешним.

---

# Часть IX–XII. Frontend rules

Подробные frontend rules вынесены в отдельный обязательный документ:

```text
docs/FRONTEND_GUIDELINES.md
```

Если проект или задача затрагивает:

```text
HTML;
CSS;
JavaScript / TypeScript;
templates;
browser UI;
frontend components;
```

LLM/developer MUST полностью прочитать `FRONTEND_GUIDELINES.md` до
проектирования или изменения frontend.

Этот документ не дублирует BEM/CSS/HTML/JS rules, чтобы не создавать две
расходящиеся copies одного frontend standard.

Общие требования этого файла продолжают действовать для frontend-кода,
включая:

```text
относительный путь каждого файла;
русскую документацию файлов/functions/classes;
testing ключевой логики;
quality gates;
logging/observability там, где применимо;
configuration rules.
```

---

# Часть XIII. Testing

## 50. Ключевая логика MUST быть покрыта тестами

Вся ключевая логика service/application/domain MUST быть покрыта
автоматическими тестами.

Изменение ключевой логики без соответствующего test change MUST NOT
приниматься в merge.

К ключевой логике относятся минимум:

```text
business use-cases;
domain rules;
state transitions;
валидация бизнес-ограничений;
ветвление, влияющее на бизнес-результат;
расчёты и преобразования данных;
парсинг значимых входных форматов;
idempotency;
retry/error handling;
transaction boundaries;
permission/tenant rules;
формирование критичных reports/results;
оркестрация background jobs;
LLM/ML decision pipeline вокруг model call;
```

Сам факт вызова external library не требует unit-test её внутренней реализации,
но наша логика до/после этого вызова MUST быть проверена.

---

## 50.1. Минимальный набор сценариев для ключевого use-case

Для каждого ключевого use-case SHOULD быть проверены минимум:

```text
happy path;
ожидаемая business error;
boundary/edge case;
invalid state;
permission/tenant boundary — если применимо;
поведение при отказе обязательной dependency — если применимо.
```

Если исправляется production bug, regression test MUST воспроизводить ошибку
до исправления и проходить после него.

---

## 50.2. Unit tests

Use-case/domain tests SHOULD использовать fakes:

```text
FakeDocumentRepository
FakeObjectStorage
FakeTaskPublisher
FakeClock
FakeLLMClient
```

Они не требуют Docker или реальной БД.

Unit tests SHOULD проверять поведение, а не внутреннее количество вызовов
каждой private функции, если это не часть контракта.

---

## 50.3. Integration tests

Infrastructure adapters SHOULD иметь integration tests там, где ошибка
контракта вероятна или критична.

Проверяются, например:

```text
SQLAlchemy repositories;
PostgreSQL constraints/transactions;
Qdrant integration;
RabbitMQ publishing/consuming;
object storage adapter;
external API adapter contract;
migration behavior.
```

Integration test не заменяет unit tests business logic.

---

## 50.4. API / Transport tests

Проверяют transport contract:

```text
status code;
request validation;
response schema;
authentication;
authorization;
error mapping;
content type;
critical headers/cookies.
```

Transport test не должен быть единственным тестом сложного use-case.

---

## 50.5. Coverage

Coverage percentage — diagnostic metric, а не самоцель.

Проект SHOULD измерять line/branch coverage в CI.

При этом правило важнее процента:

> критичная ветка business logic не может оставаться без теста только потому,
> что общий coverage проекта уже высокий.

Для critical modules SHOULD стремиться к максимально полному branch coverage.

Исключения из coverage MUST быть обоснованы, а не использоваться для скрытия
непроверенной логики.

---

## 51. Архитектурные тесты

Для нетривиальных проектов SHOULD добавить автоматические проверки границ.

Например:

```text
domain не импортирует infrastructure;
application не импортирует FastAPI;
application не импортирует SQLAlchemy;
services не содержат SQLAlchemy queries;
infrastructure не создаётся внутри use-case;
```

Это можно проверять отдельным test/lint script.

---

## 51.1. Tests и timing/logging

Timing decorator SHOULD иметь unit tests минимум на:

```text
sync success;
sync exception;
async success;
async exception;
async cancellation с сохранением CancelledError и status=error;
наличие duration_ms;
наличие status;
сохранение metadata исходной функции через functools.wraps.
```

Тесты logging не должны зависеть от точного значения duration.
Проверяется наличие неотрицательного/положительного numeric `duration_ms`.

---

# Часть XIV. Linting и quality gates

## 52. Перед merge

Python-проект MUST запускать automated tests в CI:

```text
pytest
```

Если PR изменяет ключевую logic, соответствующие tests MUST входить в тот же PR.

Python-проект SHOULD иметь quality checks:

```text
ruff check
ruff format --check
coverage / branch coverage
type checking — если принят в проекте
```

Frontend при наличии toolchain SHOULD иметь аналогичные test/lint/format checks.

Merge MUST быть заблокирован при падении обязательных tests.

Не вводить инструмент только ради формальности; выбранные checks должны
реально запускаться локально и/или в CI.

---

# Часть XV. Правила для LLM / AI coding agents

## 53. Перед архитектурным планом

LLM MUST:

1. начать с `docs/LLM_CONTEXT.md` и его task router;
2. прочитать актуальные project-specific README/architecture docs;
3. прочитать этот документ, когда он требуется router;
4. проверить `docs/services.yaml`, если затрагивается shared infrastructure;
5. определить shared vs project-specific dependencies;
6. определить application boundaries;
7. определить interfaces/ports;
8. только после этого предлагать файлы и код.

---

## 54. LLM не должен механически копировать старый проект

Reference project — пример, а не источник истины.

Если старый проект:

- нарушает dependency direction;
- создаёт infrastructure внутри use-case;
- передаёт ORM session слишком глубоко;
- имеет монолитный JS/CSS;
- содержит устаревший workaround;

LLM SHOULD следовать текущим правилам, а не повторять legacy pattern.

---

## 55. При изменении архитектуры

LLM MUST явно указать:

- какие слои затронуты;
- какие новые ports/interfaces появились;
- где composition root;
- где transaction boundary;
- где persistence;
- какие shared services используются;
- какие environment variables добавлены;
- нужны ли изменения `.env.example`;
- нужен ли новый secret/override в `.env`;
- какие тесты подтверждают изменение;
- какие функции/classes получили или обновили русскую документацию;
- какие ключевые service operations используют timing decorator;
- как ограничены rotation/retention logs.

---

# Часть XVI. Что взять из существующих проектов

## 56. Паттерны, которые стоит сохранить

Из существующего подхода полезно стандартизировать:

### Backend

- application ports через `Protocol`;
- отдельные infrastructure adapters;
- отдельный DI/composition module;
- thin HTTP routers;
- DTO/use-case разделение;
- domain exceptions;
- repository isolation.

### Frontend

- единый принятый CSS entrypoint по `FRONTEND_GUIDELINES.md`;
- CSS imports из отдельных modules;
- BEM;
- semantic HTML;
- `header/main/footer/section/aside`;
- scripts в `<head>` с `defer`;
- base template;
- page/feature scripts через template block;
- `data-*` hooks;
- accessibility attributes.

### Configuration

- baseline `.env.example`;
- sparse `.env`;
- Pydantic layered loading;
- secrets не попадают в Git.

---

## 57. Что не стоит копировать 1:1

Даже хороший reference implementation не становится абсолютным стандартом.

В новых проектах SHOULD улучшать следующие места:

1. **Не создавать repository implementations внутри use-case по умолчанию.**
   Использовать composition root.

2. **Не позволять application use-case импортировать SQLAlchemy/infrastructure**, если проект уже достаточно сложный для ports + adapters.

3. **Не считать HTTP router владельцем transaction boundary.**
   Использовать application-level UoW/use-case boundary.

4. **Не раздувать page-specific CSS и JS.**
   Выделять cohesive blocks/features.

5. **Не дублировать все `.env.example` значения в `.env`.**
   `.env` остаётся минимальным override.

---

# Часть XVII. PR Checklist

## 58. Backend

- [ ] Transport не содержит SQL.
- [ ] Transport не содержит бизнес-логику.
- [ ] Application не зависит от HTTP framework.
- [ ] Domain не зависит от infrastructure.
- [ ] SQLAlchemy находится только в data-access/infrastructure.
- [ ] External systems доступны через ports/interfaces.
- [ ] DI выполняется через constructor/function injection.
- [ ] Concrete adapters собираются в composition root.
- [ ] Нет скрытого создания клиентов/репозиториев внутри use-case.
- [ ] Transaction boundary соответствует use-case.
- [ ] Для сложной атомарности используется UoW.
- [ ] Исключения application/domain не являются `HTTPException`.
- [ ] Public methods типизированы.
- [ ] Logs не содержат secrets.
- [ ] Каждый новый/изменённый source file имеет актуальную русскую документацию.
- [ ] Все новые/изменённые функции, методы и классы имеют содержательные русские docstrings/comments.
- [ ] Ключевые public service/use-case operations используют единый timing decorator.
- [ ] Timing log содержит `operation`, `duration_ms` и `status`.
- [ ] Один exception не создаёт одинаковый traceback на нескольких слоях.

---

## 59. Configuration

- [ ] Новые non-secret settings добавлены в `.env.example`.
- [ ] `.env` не был расширен без необходимости.
- [ ] `.env` находится в `.gitignore`.
- [ ] В `.env.example` нет реальных secrets.
- [ ] Pydantic читает `.env.example` перед `.env`.
- [ ] Для Docker Compose поведение env precedence явно определено.
- [ ] Production secrets не печатаются в logs/diagnostics.

---

## 60. Frontend

Единственный обязательный frontend checklist находится в
[FRONTEND_GUIDELINES.md, §49.2](FRONTEND_GUIDELINES.md#492-обязательный-frontend-review-checklist).
Он сохраняет исключение для утверждённого project entrypoint. Самостоятельная
копия frontend checklist в Engineering не ведётся.

---

## 61. Tests и observability

- [ ] Вся изменённая ключевая business/application/domain logic покрыта tests.
- [ ] Happy path ключевого use-case проверен.
- [ ] Business error и важные edge cases проверены.
- [ ] Production bug fix содержит regression test.
- [ ] Infrastructure adapters имеют integration tests при необходимости.
- [ ] API contract проверен.
- [ ] Timing decorator протестирован для sync/async success/error paths.
- [ ] CI запускает обязательные tests.
- [ ] Lint проходит.
- [ ] Форматирование проходит.
- [ ] Container/application logs имеют конечную rotation/retention policy.
- [ ] High-frequency logic не создаёт бесконтрольный log spam.
- [ ] Нет архитектурного обхода слоя «ради быстрого фикса».

---

# Часть XVIII. Использование Guidelines в новом проекте

## 62. Engineering baseline

Этот документ является общим engineering baseline для новых проектов.

Он описывает:

```text
architecture;
dependency direction;
DI / SOLID;
data access;
transactions;
Python code style;
документирование кода;
frontend structure;
configuration;
logging / observability;
testing;
quality gates.
```

Project-specific требования MAY уточнять этот baseline, если это необходимо
для конкретного framework, deployment или business domain.

Отклонение от MUST-rule требует явного объяснения причины.

---

## 63. Изучение существующего проекта

Если проект уже существует, LLM MUST дополнительно изучить его актуальные
источники:

```text
README.md
pyproject.toml
compose.yaml / docker-compose.yml
package.json
existing source tree
tests
migrations
CI configuration
project-specific architecture docs
```

Нельзя механически перестраивать существующий проект только ради совпадения
имён папок с примерами из этого документа.

Важны архитектурные границы и направление зависимостей, а не буквальное
совпадение структуры каталогов.

---

## 64. Работа с противоречиями

Если существующий проект, project-specific documentation и этот baseline
противоречат друг другу, LLM MUST:

1. явно указать противоречие;
2. объяснить его техническое влияние;
3. определить, какое решение соответствует текущим требованиям;
4. предложить согласованное изменение связанных файлов;
5. не распространять противоречивый legacy pattern на новый код без причины.

Новое правило или изменение architecture/configuration MUST сопровождаться
проверкой связанных файлов конкретного проекта, если они описывают тот же
workflow.

Это MAY включать:

```text
README.md
architecture docs
compose.yaml
.env.example
Settings/configuration
logging configuration
scripts
CI
tests
```

Не требуется менять файл только ради формальности, если новое правило на него
не влияет.

---

# Итоговая модель

```text
                         ┌──────────────────────┐
                         │       Transport      │
                         │ HTTP / HTML / CLI    │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │     Application      │
                         │ use-cases + ports    │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │        Domain        │
                         │ rules + entities     │
                         └──────────────────────┘

Infrastructure implements Application ports:

PostgreSQL / SQLAlchemy
MinIO / S3
RabbitMQ
vLLM / shared inference
Qdrant
SMTP
external APIs

Frontend:

см. docs/FRONTEND_GUIDELINES.md

Configuration:

.env.example  -> committed baseline
       +
.env          -> sparse private/environment override
```

Главная цель правил — не максимальное количество абстракций, а **правильное направление зависимостей, явные границы ответственности, понятная русская документация, проверяемая тестами ключевая логика и наблюдаемость без бесконтрольного роста логов**.

