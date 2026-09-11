# Аудит и рефакторинг: trading_automaton decision layer (2026-08-09)

## Цель аудита

Проверить, что выбор решения в `trading_automaton` изолирован от runtime-пайплайна,
имеет фиксированную схему правил и покрыт регрессионными тестами.

## Что найдено до изменений

- `TradeDecisionService` содержал смешение приоритизированных правил и стратегии
  в одном методе, что затрудняло:
  - контроль порядка срабатывания условий,
  - расширение набора правил,
  - отдельное тестирование short-circuit поведения.
- `DecisionContext` в `TradeDecisionService` уже был на Pydantic, но не формализовано
  как самостоятельная стратегия-композиция в документации.

## Что изменено

1. Вынесена цепочка предикатов в отдельные правила:
   - `CoreAvailableRule`
   - `ActiveIntentRule`
   - `StopLossRule`
   - `TakeProfitRule`

2. `TradeDecisionService` переведён в оркестратор:
   - применяет правило-за-правилом (short-circuit),
   - при отсутствии срабатывания делегирует в выбранную стратегию,
   - оставляет прежний контракт вывода `TradeDecision`.

3. Добавлены регрессионные тесты в `test_decision_service.py`:
   - порядок правил и short-circuit,
   - защита от стратегии при наличии активных ограничений,
   - делегирование стратегии для нулевой позиции.

4. Добавлен ADR:
   - `docs/decisions/0006-trading-automaton-decision-pipeline.md`

## Результаты проверки

- `tests/trading_automaton/services/test_decision_service.py` — проходит.
- `tests/trading_automaton/services/test_adaptive_scalping_strategy.py` — проходит.
- Общий прогон `test_streaming_decision_pipeline` в текущем состоянии остаётся
  с одним известным падением, не связанное с новой правкой пайплайна (см. ниже).

## Открытые риски

- В `tests/trading_automaton/test_streaming_decision_pipeline.py::test_one_order_book_event_evaluates_each_position_once_before_sdk_dispatch`
  наблюдается отсутствие вызовов стратегии для двух позиций (ожидаемый счётчик `{automation-1:1, automation-2:1}`).
  Требуется отдельная декомпозиция `streaming` пайплайна: проверить,
  не режет ли ранний guard путь до `StreamingPositionDecisionService`
  и не перехватываются ли решения до построения `TradeDecision`.

## Рекомендуемый следующий шаг

- Закрыть открытый риск через выделение дополнительного integration smoke для
  `StreamingPositionDecisionService` + `PositionBatchSchedulerService` с единым
  `PositionWorkItem` для каждого инструмента и контролем, на каком этапе
  происходит возврат `WAIT`.
