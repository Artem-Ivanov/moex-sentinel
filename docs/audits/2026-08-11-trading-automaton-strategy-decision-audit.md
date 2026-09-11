# Аудит алгоритма выбора стратегии и границ обработки `trading_automaton`

> Актуальное нормативное описание фактического поведения вынесено в
> [спецификацию торговой стратегии](../trading-strategy.md). Этот файл сохраняет
> контекст и результаты аудита на указанную дату.

## Цель

Сформализовать текущее поведение принятия решений в `trading_automaton` перед следующей фазой крупного рефакторинга, зафиксировать инварианты и привязать их к тестам.

Актуализировано: на этапе аудита алгоритм выбора стратегии фиксируется одним
регрессионным контрактом `S-01…S-22` в `tests/trading_automaton/services/test_strategy_selection_matrix.py`.
После изменения бизнес-правил контракт используется как первичный источник истины.

## Контур принятия решения

1. Формирование `DecisionContext` в `DecisionContextService.build`:
   - брокерская позиция (`BrokerPosition`);
   - стакан (`OrderBookSnapshot`);
   - история (`IntentHistory`), лоты и `TradingCycleState`;
   - комиссии, стратегия, доступность кеша и маржинальные параметры.
2. `TradingCycleService` обновляет состояние цикла:
   - для пустой позиции — наблюдение разворота (`observe_flat_entry`);
   - для открытой позиции — подготовка к следующему SELL-циклу (`rearm_for_next_sell`) + обработка low (`observe_low`);
3. `TradeDecisionService` применяет guard-цепочку и стратегию:
   - `core_available`, `active_intent`, `stop_loss`, `take_profit`, затем стратегия (`AdaptiveScalpingStrategy`).
   - правило-цепочка теперь инкапсулирована в `DecisionRulePipeline`, которая изолирует оркестратор-уровень от бизнес-стратегий.
4. Если решение требует action (`BUY_MORE / SELL_PART / SELL_ALL`) и доступен estimator — рассчитывается комиссия, пересоздаётся контекст и решение переоценивается.
5. `TradingRuntimeService` пишет `TradeDecision`, `PositionSnapshot`, `BusinessAudit` и только потом выполняет dispatch/отслеживание intent.

## Матрица кейсов (регрессия)

| Категория | Предусловия | Ожидаемый результат |
|---|---|---|
| `G-01` | `session_open = False` | `WAIT(MARKET_SESSION_CLOSED)`, без оценки лимитной комиссии |
| `G-02` | `TradeDecisionService` вернул `DecisionKind.WAIT` | `WAIT`, без estimate-запроса |
| `G-03` | `TradeDecisionService` вернул actionable решение и estimator возвращает комиссию | решение пересчитывается с `estimated_context`, сохраняется `estimated_commission` |
| `G-04` | `DecisionContextService` получает zero-position, cycle `sell_armed=False`, цена выше порога разворота | WAIT reason по стратегии разворота (например `ENTRY_PULLBACK_WAIT`) |
| `G-05` | zero-position с подтверждённым разворотом, лимит на лот считается >0 | `BUY_MORE` (чаще `ENTRY_REVERSAL_CONFIRMED`) |
| `G-06` | open position + `sell_armed=False` | `WAIT(SELL_CYCLE_DISARMED)` |
| `G-07` | open position + активный `LIFO`-лот с выгодным приростом | `SELL_PART` и `target_lot_id` |
| `G-08` | open position, комиссия “съедает” маржу частичного профита | `WAIT(COMMISSION_EXCEEDS_PARTIAL_PROFIT)` |
| `G-09` | open position без доступных уровней усреднения | `NO_ACTION`/`WAIT` по стратегии усреднения |
| `G-10` | cycle и индикаторы валидны, цена в диапазоне | `range_position` рассчитывается как `(price-low)/(high-low)` |
| `G-11` | `PositionDecisionEvaluator` получает flat-позицию | `observe_flat_entry` вызывается до планирования и сохраняет цикл |
| `G-12` | `PositionDecisionEvaluator` получает open позицию | цикл проходит `rearm_for_next_sell` и `observe_low` по порогу |

## Границы слоёв (SOLID-контур)

- `TradingDecisionPlanner` — изолирует сбор индикаторов + формирование `DecisionPlan` (`indicators`, `context`, `decision`, `estimated_commission`).
- `PositionDecisionEvaluator` — единичный порт, который агрегирует:
  обновление цикла + вызов planner + вычисление `range_position`.
- `TradingRuntimeService` — lifecycle-оривент: оркеструет состояние автомата, запись фактов и dispatch.
- `TradingDecisionService` не зависит от runtime-адаптеров, работает над DTO.

## Полная матрица стратегических сценариев (с учётом существующих и новых кейсов)

| ID | Предусловие | Ожидаемое решение |
|---|---|---|
| S-01 | `core_available=False` | `WAIT(CORE_UNAVAILABLE)` |
| S-02 | `has_active_intent=True` | `WAIT(ACTIVE_INTENT)` |
| S-03 | `quantity_lots=0`, `cycle is None` | `WAIT(ENTRY_REVERSAL_WAIT)` |
| S-04 | `quantity_lots=0`, cycle без `sell_armed` | `WAIT(ENTRY_PULLBACK_WAIT)` |
| S-05 | `quantity_lots=0`, есть `cycle.pending_low`, но `candle` не подтверждён | `WAIT(ENTRY_REVERSAL_WAIT)` |
| S-06 | `quantity_lots=0`, цена выше фильтра `0.5%` после подтверждения входа | `BUY_MORE(ENTRY_REVERSAL_CONFIRMED)` |
| S-07 | `quantity_lots>0`, цена ниже stop-loss | `SELL_ALL(STOP_LOSS)` |
| S-08 | `quantity_lots>0`, цена выше take-profit | `SELL_ALL(TAKE_PROFIT)` |
| S-09 | `sell cycle` отключён (`cycle.sell_armed=False`) | `WAIT(SELL_CYCLE_DISARMED)` |
| S-10 | `averaging` при доступных уровнях, комиссия и цена допустимы | `BUY_MORE(AVERAGING_LEVEL)` |
| S-11 | `averaging` перевищен лимит позиции | `WAIT(POSITION_LIMIT_EXCEEDED)` |
| S-12 | `averaging` недостаточно свободных денег | `WAIT(INSUFFICIENT_FREE_CASH)` |
| S-13 | open lot с LIFO: `gross <= buy + sell комиссии` | `WAIT(COMMISSION_EXCEEDS_PARTIAL_PROFIT)` |
| S-14 | open lot с LIFO: прибыль выше комиссий | `SELL_PART(LIFO_LOT_TAKE_PROFIT)` |
| S-15 | aggregate averaging, цена без достаточного профита | `WAIT(COMMISSION_EXCEEDS_PARTIAL_PROFIT)` |
| S-16 | aggregate averaging, цена выше порога профита | `SELL_PART(PARTIAL_TAKE_PROFIT)` |
| S-17 | `available_averaging_levels=0` | `NO_ACTION` |
| S-18 | исторический режим без индикаторов и цикла на `quantity_lots=0`, недостаточный лот | `WAIT(INITIAL_BUDGET_BELOW_LOT_COST)` |
| S-19 | диапазон верхней четверти и `best_ask` выше порога | `WAIT(ENTRY_UPPER_RANGE_FILTER)` |
| S-20 | `BUY_CANDLE_COOLDOWN` после входа в тот же candle | `WAIT(BUY_CANDLE_COOLDOWN)` |
| S-21 | downtrend в индикаторах для averaging | `WAIT(DOWNTREND_FILTER)` |
| S-22 | no sell/buy сигнала в нормальном состоянии | `WAIT(NO_THRESHOLD)` |

## Сопоставление кейсов и тестов

- `TradeDecisionService`: `tests/trading_automaton/services/test_decision_service.py`
- `AdaptiveScalpingStrategy`: `tests/trading_automaton/services/test_adaptive_scalping_strategy.py`
- `PositionDecisionEvaluator`: `tests/trading_automaton/services/test_position_decision_evaluator.py`
- `Runtime/market-поток`: `tests/trading_automaton/services/test_runtime_service.py::test_adaptive_thresholds_are_loaded_for_position_decision`
- `Полный контракт выбора стратегии`: `tests/trading_automaton/services/test_strategy_selection_matrix.py`

## Дальнейшие доработки перед refactor-фазой

1. Поддерживать и расширять контракт `S-01…S-22` в
   `tests/trading_automaton/services/test_strategy_selection_matrix.py`.
2. После этого реализовать "runtime-композицию по интерфейсам":
   - `DecisionContext/RuntimeDecisionPlan` остаются неизменяемыми DTO,
   - `TradingDecisionService` остаётся стратегическим ядром,
   - `PositionDecisionEvaluator` — границей между доменной логикой и persistence-слоем.

## Тестовое покрытие (обязательные кейсы)

После фиксации алгоритма ожидается наличие проверок:

- `tests/trading_automaton/services/test_runtime_decision_planner.py`  
  (`session_closed`, `actionable_recompute`, `indicators_cache`, `fallback`);
- `tests/trading_automaton/services/test_position_decision_evaluator.py` (новый файл)  
  (`flat_position_cycle_update`, `open_position_rearm_and_low`, `range_position_calc`);
- `tests/trading_automaton/services/test_runtime_service.py::test_adaptive_thresholds_are_loaded_for_position_decision`  
  — проверяет загрузку адаптивных порогов + факт сохранения `range_position`;
- `tests/trading_automaton/services/test_adaptive_scalping_strategy.py` и  
  `tests/trading_automaton/services/test_decision_service.py` — детерминированные сценарии стратегии и правил.

## Статус аудита

- Стратегический путь принятия решения подтверждён на уровне service-уровня.
- 2026-08-11: выделен и подключен `DecisionExecutionService` как отдельный слой исполнения.
  - `TradingRuntimeService._process_position` больше не содержит прямого вызова `IntentService`,
    не решает, нужно ли создавать intent.
  - `DecisionExecutionService` сохраняет решение в `TradeDecision`, решает сценарии `WAIT/NO_ACTION`,
    выполняет брокерное исполнение через `DecisionIntentPort`, прикрепляет `intent_id`,
    и возвращает `hold_reason` для единообразного поднятия HOLD-состояния.
- Для регресса: новые границы покрыты `tests/trading_automaton/services/test_decision_execution_service.py`.
