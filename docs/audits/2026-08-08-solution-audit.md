# Общий аудит решения MOEX Sentinel

- Дата: 2026-08-08
- Статус: `NO-GO` для реальной торговли; структурный рефакторинг
  `trading_automaton` в `Usecase → Service` отложен до устранения фаз A–B.
- Область: Core/backend, trading worker, frontend, контракты, SQLite,
  Docker Compose и эксплуатационные проверки.
- Ограничение проверки: production-код и тесты в рамках аудита не изменялись;
  runtime-контейнеры и внешний sandbox не инспектировались.

## Резюме

Основные инварианты R0 — durable intent, атомарная фиксация терминального
исполнения, LIFO ledger, replay и локальный active-intent gate — подтверждены
тестами. Однако lifecycle автомата пока может убрать `CLOSED`-позицию из
наблюдения до терминализации активного intent, а восстановление после падения не
возобновляет сопровождение всех нетерминальных intent. Эти два дефекта являются
P0 и блокируют реальную торговлю.

Семантика `HOLD` скорректирована по решению владельца продукта: `HOLD` запрещает
новые решения, но не обязан отменять уже принятую брокером заявку. Watcher обязан
продолжать её сопровождение до подтверждённого терминального состояния.
`CancelOrder` является отдельным пользовательским действием для активной
лимитной заявки, а не автоматическим следствием `HOLD`.

## Findings

### F-01 — P0: `CLOSED` может наступить до терминализации активного intent

- Evidence:
  [services/automations.py:111](../../src/moex_sentinel/services/automations.py#L111),
  [storage/repository.py:819](../../src/trading_automaton/storage/repository.py#L819),
  [streaming_runtime_coordinator.py:192](../../src/trading_automaton/services/streaming_runtime_coordinator.py#L192),
  [composition.py:66](../../src/trading_automaton/composition.py#L66).
- Сценарий: Core сразу сохраняет `CLOSED`; worker исключает эту запись из
  `list_active`, coordinator удаляет broker bundle, а завершение bundle ждёт лишь
  уже существующие in-memory watcher. Intent, который принят брокером, но не был
  восстановлен в watcher, перестаёт сопровождаться.
- Влияние: неизвестный итог заявки, несогласованные broker/Core/ledger/cash и
  потеря факта частичного либо полного исполнения.
- Когда исправлять: **до рефакторинга**, фаза A.
- Требование: `CLOSED` — желаемое терминальное состояние автомата. Фактический
  переход разрешён только после отсутствия нетерминальных intent; до этого новые
  решения запрещены, а tracking продолжает работать. Автоматическая отмена при
  `HOLD/CLOSED` не требуется.

### F-02 — P0: crash recovery не восстанавливает все нетерминальные intent

- Evidence:
  [services/recovery.py:10](../../src/trading_automaton/services/recovery.py#L10),
  [__main__.py:19](../../src/trading_automaton/__main__.py#L19),
  [storage/repository.py:1095](../../src/trading_automaton/storage/repository.py#L1095),
  [order_tracking.py:200](../../src/trading_automaton/services/order_tracking.py#L200).
- Сценарий: после unclean shutdown recovery только переводит активные автоматы в
  `HOLD`. Для durable состояний `CREATED/DISPATCH_PENDING/SUBMITTING/SUBMITTED/
  ACCEPTED/PARTIALLY_FILLED` не создаётся восстановленный watcher и не выполняется
  поиск заявки по idempotency key/order id.
- Влияние: принятая заявка может исполниться без фиксации ledger, операции,
  комиссии и cash; ручной `RESUME` не является заменой reconciliation.
- Когда исправлять: **до рефакторинга**, фаза A.
- Требование: при старте классифицировать каждый нетерминальный intent, восстановить
  tracking/reconciliation и сохранять автомат в `HOLD` до доказанного результата.

### F-03 — P1: watcher ограничен polling timeout и привязан к market bundle

- Evidence:
  [order_tracking.py:356](../../src/trading_automaton/services/order_tracking.py#L356),
  [order_tracking.py:372](../../src/trading_automaton/services/order_tracking.py#L372),
  [broker_runtime.py:94](../../src/trading_automaton/services/broker_runtime.py#L94),
  [composition.py:66](../../src/trading_automaton/composition.py#L66).
- Сценарий: после исчерпания ограниченного polling intent становится `UNCERTAIN`;
  lifecycle watcher принадлежит bundle, который одновременно владеет market
  stream и SDK session.
- Влияние: длительно исполняющаяся заявка теряет непрерывное сопровождение при
  перестройке market runtime.
- Когда исправлять: **до рефакторинга**, фаза B.
- Требование: durable order supervisor с ограниченными отдельными запросами, но
  без потери общего сопровождения; его lifecycle не зависит от market-data bundle.

### F-04 — P1: reconciliation `UNCERTAIN` не освобождает cash reservation

- Evidence:
  [uncertain_intent_reconciliation.py:91](../../src/trading_automaton/services/uncertain_intent_reconciliation.py#L91),
  [uncertain_intent_reconciliation.py:120](../../src/trading_automaton/services/uncertain_intent_reconciliation.py#L120),
  [order_tracking.py:194](../../src/trading_automaton/services/order_tracking.py#L194),
  [composition.py:163](../../src/trading_automaton/composition.py#L163).
- Сценарий: reconciliation сохраняет терминальный broker state и очищает
  active-intent gate, но не вызывает cash completion. В обычном terminal watcher
  cash и gate очищаются совместно.
- Влияние: фантомно зарезервированные средства блокируют последующие покупки.
- Когда исправлять: **до рефакторинга**, фаза B.
- Требование: единый идемпотентный post-terminal cleanup для watcher и
  reconciliation; резерв сохраняется только пока результат действительно
  `UNCERTAIN`.

### F-05 — P1: отсутствует singleton/fencing между worker-процессами

- Evidence:
  [storage/repository.py:927](../../src/trading_automaton/storage/repository.py#L927),
  [streaming_runtime_coordinator.py:98](../../src/trading_automaton/services/streaming_runtime_coordinator.py#L98),
  [compose.yml:33](../../compose.yml#L33).
- Сценарий: `worker_runs` — маркер clean shutdown, но не lease/lock; Core claim
  принимает `worker_id`, однако сервис claim не закрепляет автомат за владельцем.
  Два процесса могут одновременно обработать один durable intent.
- Влияние: дублирование broker dispatch и конфликт локального SQLite/in-memory
  состояния.
- Когда исправлять: **до рефакторинга**, фаза B.
- Требование MVP: жёсткий singleton lock. Перед горизонтальным масштабированием —
  lease/fencing token и атомарный claim с владельцем.

### F-06 — High: durable audit допускает сырые внешние детали

- Evidence:
  [business_audit.py:33](../../src/trading_automaton/services/business_audit.py#L33),
  [business_audit.py:160](../../src/trading_automaton/services/business_audit.py#L160),
  [storage/repository.py:321](../../src/trading_automaton/storage/repository.py#L321).
- Сценарий: allowlist включает `broker_error_details`, `reconciliation_error`,
  `position_snapshot` и `recent_operations`, после чего значения без нормализации
  сохраняются в worker DB и публикуются в Core.
- Влияние: нестабильная схема аудита, избыточные/чувствительные внешние payload и
  неограниченный объём durable события.
- Когда исправлять: **во время рефакторинга**, фаза C.
- Требование: typed safe audit DTO, коды ошибок и ограниченные диагностические
  поля; никакого raw SDK exception/payload.

### F-07 — High: broad catch и универсальные exception mapper

- Evidence:
  [usecases/automations.py:193](../../src/moex_sentinel/usecases/automations.py#L193),
  [usecases/automations.py:308](../../src/moex_sentinel/usecases/automations.py#L308),
  [usecases/brokers.py:24](../../src/moex_sentinel/usecases/brokers.py#L24).
- Сценарий: `except Exception` затем распознаёт тип через `isinstance` либо только
  передаёт исключение в `_automation_error/_to_usecase_error` и выбрасывает выше.
- Влияние: скрывается реальная граница обработки, неожиданные ошибки выглядят как
  ожидаемые, код сложнее читать и тестировать.
- Когда исправлять: **во время рефакторинга**, фаза C.
- Требование: отдельный `except SpecificError` для каждого ожидаемого типа;
  `except Exception` только как последний recovery/logging boundary. Если локального
  восстановления или конкретного преобразования нет — исключение не перехватывать.

### F-08 — High: конфликт lifecycle policy при падении broker runtime

- Evidence:
  [streaming_runtime_coordinator.py:199](../../src/trading_automaton/services/streaming_runtime_coordinator.py#L199),
  [storage/repository.py:944](../../src/trading_automaton/storage/repository.py#L944),
  [план вехи 1: lifecycle safety](../phase-1-implementation-plan.md#16-надёжность-выполнения-и-реакция-жизненного-цикла-crit).
- Сценарий: coordinator автоматически закрывает завершившийся bundle и строит
  новый, тогда как fail-safe контракт требует `HOLD` и ручной `RESUME`.
- Влияние: после неизвестной ошибки торговля может возобновиться автоматически.
- Когда исправлять: **до рефакторинга**, фаза B.
- Требование: сначала зафиксировать единую policy; рекомендуемый safe default —
  durable `HOLD`, восстановление order tracking и только ручной `RESUME`.

### F-09 — High: направление зависимостей `Service → Usecase`

- Evidence:
  [services/brokers.py:14](../../src/moex_sentinel/services/brokers.py#L14),
  [services/automation_entry_budget.py:6](../../src/moex_sentinel/services/automation_entry_budget.py#L6),
  [services/strategies.py:7](../../src/moex_sentinel/services/strategies.py#L7).
- Сценарий: сервисы импортируют `FieldError` из application/usecase слоя.
- Влияние: service нельзя использовать независимо; нарушено согласованное
  `View → Usecase → Service → Adapter/Repository`.
- Когда исправлять: **во время рефакторинга**, фаза C.
- Требование: domain/service validation issue не зависит от Usecase; Usecase явно
  преобразует конкретные service errors в transport-safe field errors.

### F-10 — High: strict DTO contract ещё не проведён через границы

- Evidence:
  [sentinel_contracts/business_audit.py:40](../../src/sentinel_contracts/business_audit.py#L40),
  [storage/repository.py:39](../../src/trading_automaton/storage/repository.py#L39),
  [streaming_runtime_coordinator.py:163](../../src/trading_automaton/services/streaming_runtime_coordinator.py#L163),
  [views/schemas/brokers.py:11](../../src/moex_sentinel/views/schemas/brokers.py#L11).
- Сценарий: HTTP использует Pydantic, но shared/worker DTO остаются dataclass и
  `dict[str, object]`; обязательность полей и extra policy различаются.
- Влияние: поздние `KeyError/TypeError`, молчаливые optional fallback и расхождение
  Core–worker/frontend контрактов.
- Когда исправлять: **во время рефакторинга**, фаза C.
- Требование: по утверждённой strict-Pydantic спецификации валидировать внешний
  контракт в adapter, бизнес-инвариант в Service и преобразовывать только известные
  ошибки в Usecase. Обязательное отсутствующее поле — ошибка.

### F-11 — High: у worker нет собственного healthcheck

- Evidence:
  [compose.yml:33](../../compose.yml#L33),
  [docker/automaton.Dockerfile:1](../../docker/automaton.Dockerfile#L1),
  [__main__.py:30](../../src/trading_automaton/__main__.py#L30).
- Сценарий: контейнер может быть `running`, когда coordinator loop, market stream,
  Core synchronization либо broker session неработоспособны.
- Влияние: Compose не отличает работоспособный worker от зависшего.
- Когда исправлять: **после блокирующих trading fixes, до рефакторинга**, фаза B.
- Требование: health/readiness worker с возрастом heartbeat/tick, состоянием Core,
  broker runtime и последней критической ошибкой.

### F-12 — High: cold Compose не гарантирует сборку base image

- Evidence:
  [compose.yml:2](../../compose.yml#L2),
  [compose.yml:10](../../compose.yml#L10),
  [docker/backend.Dockerfile:3](../../docker/backend.Dockerfile#L3),
  [docker/automaton.Dockerfile:3](../../docker/automaton.Dockerfile#L3).
- Сценарий: `python-base` скрыт профилем `build`; backend/worker используют его
  image, но не имеют Compose dependency, гарантирующей сборку на чистой машине.
- Влияние: cold `docker compose build/up` может завершиться ошибкой отсутствующего
  `moex-sentinel-python-base:local`.
- Когда исправлять: **после рефакторинга**, фаза D, либо раньше для CI.
- Требование: документированная двухшаговая сборка или bake/multi-stage схема,
  воспроизводимо строящая base до зависимых образов.

### F-13 — High: worker schema меняется ad-hoc без версий и backup policy

- Evidence:
  [storage/database.py:23](../../src/trading_automaton/storage/database.py#L23),
  [composition.py:193](../../src/trading_automaton/composition.py#L193),
  [compose.yml:43](../../compose.yml#L43).
- Сценарий: startup вызывает `create_all` и набор условных `ALTER TABLE`; нет
  версии schema, полного порядка upgrade, rollback/backup и проверки совместимости.
- Влияние: новая схема может частично примениться к единственной durable worker DB,
  а восстановление данных не определено.
- Когда исправлять: **после стабилизации, до следующих schema changes**, фаза D.
- Требование: versioned worker migrations, pre-upgrade backup, проверка текущей
  версии и документированный restore.

### F-14 — High, unresolved: место расходования broker rate limit в R0.5

- Evidence:
  [position_batch_scheduler.py:123](../../src/trading_automaton/services/position_batch_scheduler.py#L123),
  [position_batch_scheduler.py:131](../../src/trading_automaton/services/position_batch_scheduler.py#L131),
  [broker_rate_limit.py:15](../../src/trading_automaton/services/broker_rate_limit.py#L15).
- Сценарий: token расходуется до вызова стратегии, поэтому соблюдается требование
  «не считать стратегию при исчерпанном budget», но token также расходуется для
  результата стратегии `WAIT/NO_ACTION`, где broker dispatch не будет.
- Влияние: позиции искусственно получают `BROKER_RATE_LIMIT_BUDGET`, хотя реальная
  пропускная способность broker order API не использована.
- Когда исправлять: **до принятия R0.5**, фаза A.
- Требование: разделить дешёвую eligibility preparation, ровно один strategy call
  и dispatch admission. Rate-limit token резервируется только для решения с
  broker action непосредственно перед созданием/dispatch intent, без повторной
  оценки стратегии.

## Принятые решения и не-findings

- Plaintext editable token в браузере/SQLite и отсутствие авторизации — осознанный
  MVP-компромисс для локального single-user приложения. Это не finding текущего
  аудита; перед удалённым доступом или multi-user режимом решение пересматривается.
- `HOLD` не обязан вызывать `CancelOrder`. Он запрещает новые решения, сохраняя
  tracking уже принятой заявки. Отмена — отдельный usecase с последующим ожиданием
  фактического статуса, включая возможное частичное исполнение.
- Несколько broker configurations архитектурно предусмотрены, но единственный
  реализованный provider/adapter T-Invest Sandbox принят для MVP и не является
  finding.
- Локальное хранение credentials принято для MVP и не требует отдельного vault.
- Сам рефакторинг `trading_automaton` в `Usecase → Service` утверждён, но отложен
  до завершения общего аудита и фаз A–B.
- `InventoryRiskGuardService`, торговля от полос, benchmark и расширение списка
  providers остаются отдельными последующими вехами.

## Quality evidence

Состояние зафиксировано завершёнными audit-прогонами 2026-08-08:

| Проверка | Результат | Вывод |
|---|---:|---|
| Backend/worker pytest | `490 passed` | Поведенческий regression suite зелёный |
| Frontend Vitest | `65 passed` | Component/API regression suite зелёный |
| Frontend typecheck | pass | TypeScript-контракты компилируются |
| Frontend production build | pass | Сборка frontend зелёная |
| Black | pass | Формат production/test Python-файлов согласован |
| Ruff | `21 errors` | Quality gate красный; исправить без изменения смысла тестов |
| mypy | `95 errors` | Static typing gate красный; значительная contract debt |
| Docker Compose config | valid | Статическая конфигурация корректна |
| Docker runtime/sandbox | not inspected | Нельзя заявлять runtime readiness/health |

Зелёный pytest не отменяет P0/P1: отсутствующие crash/lifecycle сценарии не
доказываются существующими тестами. При исправлениях тесты разрешено переносить и
адаптировать к структуре, но нельзя менять их бизнес-смысл. Функциональные тесты
сервисов размещаются рядом с service test package, интеграционные взаимодействия
между сервисами — в верхнеуровневом `tests/`.

## Порядок remediation

### Фаза A — торговые блокеры

1. Исправить `CLOSED requested → terminal intent tracking → CLOSED committed`.
2. Реализовать startup recovery всех нетерминальных intent.
3. Разрешить R0.5 rate-limit placement без второго вызова стратегии.
4. Добавить lifecycle/crash/replay regression tests, сохраняя согласованный смысл
   существующих тестов.

Exit criteria: нет intent без durable owner/tracker; `CLOSED` не теряет broker
order; rate limiter расходуется только на broker action; полный pytest зелёный.

### Фаза B — runtime safety и эксплуатационная видимость

1. Отделить durable order supervisor от market bundle и определить bounded polling.
2. Унифицировать terminal cleanup reconciliation: cash + gate + ledger/outbox.
3. Ввести singleton lock/fencing для worker.
4. Зафиксировать fail-safe lifecycle policy и исключить автоматическое
   возобновление после неизвестного сбоя.
5. Добавить worker health/readiness.

Exit criteria: crash/restart/long-running order сценарии детерминированы; один
automation обрабатывает ровно один worker owner; health отражает реальную работу.

### Фаза C — утверждённый архитектурный рефакторинг

1. Ввести конечные worker Usecases и оставить scheduler/runtime только lifecycle.
2. Устранить `Service → Usecase`; сохранить направление
   `entrypoint/view → Usecase → Service → Adapter/Repository`.
3. Удалить `_automation_error`, `_to_usecase_error`, broad catch-and-rethrow;
   ловить конкретные ошибки.
4. Перевести граничные DTO на strict Pydantic по утверждённой спецификации.
5. Типизировать и санитизировать durable audit.
6. Разделить service и integration tests без изменения проверяемой логики.

Exit criteria: архитектурные границы не образуют обратных импортов; обязательные
контракты fail fast; pytest/Frontend/Black/Ruff/mypy зелёные.

### Фаза D — воспроизводимость и дальнейшее развитие

1. Ввести versioned worker migrations, backup/restore.
2. Сделать cold Docker build воспроизводимым и провести runtime smoke.
3. Выполнить benchmark/soak test, затем вернуться к Inventory Risk Guard и
   расширению стратегии/providers.

Exit criteria: clean-host build/up воспроизводим; migration/restore проверены;
runtime и SLA подтверждены измерениями, а не только unit-тестами.

## Итоговый verdict

Текущий результат пригоден для дальнейшей локальной разработки и
детерминированного тестирования в sandbox, но не готов к автономной реальной
торговле. Следующая работа — фаза A, затем B. Usecase-рефакторинг начинается
только после повторного аудита exit criteria этих фаз.
