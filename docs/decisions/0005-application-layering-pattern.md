# ADR 0005: View → UseCase → Service → Adapter

- Status: Accepted
- Date: 2026-08-04

## Context

Приложение развивается по функциональным фазам и будет содержать HTTP, фоновые workers, торговые сценарии и несколько интеграций с площадками. Без строгого направления зависимостей транспортная обработка, orchestration и бизнес-правила начинают смешиваться во View или дублироваться между endpoint и worker.

## Decision

Backend разделяется на верхнеуровневые пакеты:

```text
moex_sentinel/
├── views/        # транспортный ввод и вывод
├── usecases/     # атомарные пользовательские/системные сценарии
├── services/     # переиспользуемая бизнес-логика и валидация
├── adapters/     # интеграции с внешними системами
└── storage/      # ORM, repositories, migrations and database lifecycle
```

Разрешённое направление вызовов:

```text
View → UseCase → Service → Service / Adapter / Repository
```

Обратные зависимости запрещены. Service не импортирует и не вызывает UseCase. UseCase не вызывает View. Adapter и Repository не зависят от View, UseCase или Service.

## View

View является транспортным адаптером. Для HTTP это FastAPI route и связанные request/response schemas.

View может:

- разобрать входной HTTP-запрос;
- вызвать один или несколько UseCase;
- преобразовать успешный результат в HTTP response;
- передать `UseCaseError` общему transport renderer.

View не может:

- содержать бизнес-валидацию;
- обращаться к Service, Repository или внешнему Adapter напрямую;
- преобразовывать внутренние service/repository exceptions;
- принимать торговые решения.

View не покрывается отдельными unit-тестами. Бизнес-поведение проверяется тестами UseCase и Service. Работоспособность HTTP wiring подтверждается только минимальными smoke/integration checks на уровне собранного приложения, без повторения бизнес-сценариев View-тестами.

## UseCase

UseCase представляет одно явное намерение актора приложения. Для локального
пользователя это сохранить настройки брокера, добавить тикер в отслеживание,
открыть позицию, переключить торговлю в AUTO или остановить автоторговлю. Для
системного актора это выполнить одну конечную торговую итерацию, broker tick,
tracking или reconciliation.

UseCase:

- принимает уже разобранные входные DTO;
- последовательно вызывает один или несколько Services;
- управляет orchestration одного сценария;
- преобразует внутренние ошибки Services в стабильный `UseCaseError`;
- возвращает результат, пригодный для View или фонового вызывающего кода.

Атомарность означает неделимость бизнес-сценария для вызывающей стороны. Если один UseCase должен атомарно изменить несколько записей одной БД, ему внедряется Unit of Work. Несвязанные repository-транзакции не объявляются атомарными без общей транзакционной границы.

UseCase не содержит переиспользуемых правил предметной области: такие правила принадлежат Services.
UseCase не вызывает другой UseCase: общий шаг выделяется в Service/port либо
входит в одну выбранную application boundary. Lifecycle runtime может запускать
несколько независимых UseCase, но не переносит в себя бизнес-логику.

Scheduler, supervisor, worker и stream consumer являются системными акторами.
Бесконечный runtime-loop, таймер и lifecycle процесса не являются UseCase, но
каждая запускаемая ими конечная application-операция входит через UseCase:
торговая итерация, broker tick, выполнение решения, tracking исполнения или
reconciliation. Background-компонент вызывает UseCase через DI и не обращается
напрямую к бизнес-сервисам, Adapter или Repository. Внутренний расчёт и
переиспользуемые правила остаются в Services.

## Service

Service реализует переиспользуемую бизнес-логику и является владельцем бизнес-валидации.

Service может:

- проверять бизнес-инварианты;
- выполнять расчёты и нормализацию;
- вызывать другие Services;
- вызывать Adapters и Repositories через явные зависимости;
- возвращать domain/application DTO;
- поднимать типизированные внутренние ошибки.

Service не знает о FastAPI, HTTP status codes, Vue и UseCases. Service не формирует пользовательские HTTP-сообщения.

## Adapter и Repository

Adapter реализует взаимодействие с внешней системой или конкретным SDK. Repository является storage adapter и скрывает SQLAlchemy/SQLite.

Они:

- реализуют узкие интерфейсы, появившиеся из реального использования;
- не содержат orchestration пользовательского сценария;
- преобразуют технические ошибки в типизированные adapter/storage errors;
- не импортируют View или UseCase.

## Dependency Injection

UseCases и Services получают зависимости извне через constructor injection. Создание concrete Services, Repositories и Adapters выполняется только в composition root.

```text
api/app.py / composition.py
→ constructs Repository and Adapter implementations
→ injects them into Services
→ injects Services into UseCases
→ exposes UseCases to Views
```

Правила DI:

- Service не создаёт Repository, Adapter или другой Service внутри метода или конструктора;
- UseCase не создаёт Service;
- зависимости сохраняются в явных типизированных constructor parameters;
- на границах Adapter/Repository допускаются узкие Protocol-интерфейсы ради заменяемости и тестирования;
- concrete infrastructure types не просачиваются в сигнатуры бизнес-слоёв;
- глобальный service locator и изменяемые singleton-контейнеры запрещены;
- FastAPI `Depends` используется только в transport/composition слое;
- DI-фреймворк не добавляется, пока ручная сборка остаётся простой.

В unit-тестах Service получает controllable fake/stub реализации портов. UseCase получает настоящий Service с подменёнными зависимостями либо узкий test double Service, когда проверяется только orchestration. Моки transport View не используются для проверки бизнес-логики.

## Error flow

Ошибки движутся только вверх:

```text
AdapterError / StorageError
→ ServiceError
→ UseCaseError(code, message)
→ shared View error renderer
→ transport response
```

`UseCaseError` содержит стабильный машинный `code` и читаемый безопасный `message`. Он не содержит токены, значения broker fields, connection strings или сырые сообщения SDK/SQLAlchemy.

HTTP status определяется централизованным transport renderer по коду ошибки. Это техническое преобразование, а не обработка бизнес-исключения во View.

### Перехват исключений

Ожидаемые типизированные исключения перехватываются отдельными ветками `except`
до общего обработчика. Проверка конкретного типа через `isinstance` внутри
`except Exception` запрещена: она скрывает контракт метода и смешивает ожидаемый
бизнес-сценарий с обработкой непредвиденного сбоя.

```python
try:
    execute_operation()
except InvalidAutomationEntryBudgetError:
    handle_invalid_budget()
except (BrokerUnavailableError, BrokerTimeoutError):
    handle_broker_failure()
except Exception:
    handle_unexpected_failure()
```

Общий `except Exception` допускается только последней fallback-веткой. Он не
распознаёт доменные или инфраструктурные типы вручную. Если несколько исключений
имеют одинаковую семантику обработки, они перечисляются кортежем в одной
типизированной ветке. Пустое подавление исключений и молчаливый возврат fallback-
значения запрещены.

Перехват, единственным результатом которого является немедленный проброс ошибки
выше по стеку, также запрещён:

```python
try:
    return service.list_active()
except Exception as error:
    raise map_error(error) from error
```

Если текущий слой не восстанавливает состояние и не преобразует конкретный
известный тип в собственный контракт ошибки, исключение должно распространяться
естественно: `return service.list_active()`. Это сохраняет исходный traceback и
не создаёт ложную точку владения ошибкой. Преобразование ошибок на границе слоя
остаётся допустимым только для явно перечисленных типов, например
`except StorageError as error: raise ServiceError(...) from error`.

Универсальные функции распознавания и преобразования произвольных исключений,
такие как `_automation_error(error: Exception)` и
`_to_usecase_error(error: Exception)`, не используются. Они скрывают цепочку
проверок типов, делают контракт обработчика неявным и побуждают перехватывать
слишком широкий `Exception`. В каждом месте вызова перечисляются только ожидаемые
типы исключений и выполняется их непосредственное преобразование. Неожиданная
ошибка не передаётся в такой mapper и обрабатывается единым верхнеуровневым
fallback-механизмом приложения.

## Validation

- Формат transport payload проверяется Pydantic во View как часть разбора запроса.
- Бизнес-валидность данных проверяется Service.
- UseCase координирует Services и преобразует их ошибки.
- Frontend не дублирует backend business validation.

## Testing

Тесты разделяются на функциональные и интеграционные.

Функциональный тест проверяет внутреннее поведение ровно одного Service,
UseCase или lifecycle-компонента с controllable fake/stub dependencies. Тесты
Service и lifecycle-компонентов хранятся в
`tests/trading_automaton/services/`; тесты UseCase — в
`tests/trading_automaton/usecases/`. Lifecycle-компонент может находиться в
production-пакете `runtime/`, но его functional suite всё равно размещается в
`services/` и импортирует компонент по фактическому production path.

Интеграционный тест проверяет взаимодействие двух или более собранных
компонентов (например, composition root и lifecycle runtime) и хранится
непосредственно в `tests/trading_automaton/`. Он проверяет wiring и наблюдаемое
совместное поведение, но не дублирует внутренние assertions функциональных
тестов.

Обязательные functional уровни:

- Service tests — бизнес-правила, валидация и взаимодействие с repositories/adapters;
- UseCase tests — последовательность вызовов Services, атомарный сценарий и readable error mapping;
- Repository/Adapter contract tests — инфраструктурные ограничения и преобразование технических ошибок;
- migration tests — соответствие ORM и схемы;
- application smoke/integration tests — только wiring, startup, health и
  взаимодействие собранных компонентов.

Отдельные unit-тесты FastAPI Views не создаются. Один и тот же бизнес-сценарий не дублируется в Service, UseCase и View test suites.
При перемещении теста вслед за владельцем поведения разрешено изменить только
import path, fixture, constructor и DI wiring. Assertions, входные параметры,
error/concurrency/replay cases и проверяемый порядок операций сохраняются 1:1.

## Enforcement

Архитектурный review проверяет и запрещает:

- `services → usecases|views`;
- `usecases → views`;
- `adapters|storage → services|usecases|views`;
- `views → repositories|storage models|adapters`.

Тесты на структуру файлов, расположение модулей и AST-граф импортов не создаются. DI проверяется поведенческими тестами Services и UseCases с внедрёнными fake/stub dependencies. Создание concrete dependencies внутри бизнес-слоёв контролируется review.

Code review также проверяет отсутствие конструкции `except Exception` с
последующим `isinstance(error, SpecificError)`. Ожидаемые типы должны быть
выражены последовательностью конкретных веток `except`, расположенных перед
общим fallback-обработчиком. Отдельно удаляются широкие обработчики, которые
только вызывают mapper и повторно поднимают исключение без локального
восстановления или типизированного преобразования контракта. Универсальные
exception-mapper функции, принимающие `Exception`, также запрещены.

Классы пользовательских сценариев именуются с суффиксом `Usecase`, например `SaveBrokerSettingsUsecase`. Классы бизнес-сервисов именуются с суффиксом `Service`, например `BrokerConfigurationService`. Это соглашение применяется к классам, но не требует искусственного переименования модулей, функций, Protocol-портов или инфраструктурных адаптеров.

View может импортировать UseCase и transport schemas. Composition root (`api/app.py`) может импортировать все слои только для сборки зависимостей и регистрации routers/handlers.

## Consequences

- Появляется дополнительный слой UseCase и явное dependency wiring.
- Бизнес-правила становятся независимо тестируемыми от FastAPI.
- HTTP вызывает пользовательские UseCases, а фоновые workers запускают конечные
  системные UseCases; оба типа сценариев переиспользуют Services.
- Ошибки имеют единый безопасный контракт для frontend.
- Для multi-repository атомарности потребуется Unit of Work, когда появится реальный сценарий.
