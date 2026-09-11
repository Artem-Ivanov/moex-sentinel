# ADR 0007: Runtime decision coordinator in `trading_automaton`

- Status: Accepted
- Date: 2026-08-09

Уточнение 2026-09-09: этот ADR описывает сохранённый planner-контракт.
Действующий streaming runtime заранее загружает commission schedule, строит
единый immutable market snapshot и вызывает decision один раз; повторный
broker estimate внутри этого пути не выполняется. Веха 0.8 закрепляет
проверку стакана до cycle, net take-profit и atomic execution/cycle state.

## Проблема

`TradingRuntimeService` в одной итерации одновременно:
- тянет/обновляет рыночные сигналы;
- строит контекст решения;
- оценивает комиссию и повторно решает;
- формирует audit/snapshot;
- пишет событие в БД;
- запускает исполнение.

При росте сценариев это затрудняло изоляцию слоёв и регрессионную проверку.

## Решение

Выделить отдельный слой `TradingDecisionPlanner`:

- получает входные доменные объекты (`AutomationCommand`, `BrokerPosition`, `OrderBookSnapshot`,
  `IntentHistory`, `TradeLotRecord`, `TradingCycleState`);
- загружает и кэширует рыночные индикаторы;
- строит базовый и пересчитанный (по оценке комиссии) `DecisionContext`;
- вызывает `TradeDecisionService` и возвращает `DecisionPlan`;
- сам по себе ничего не пишет в БД и не исполняет intent.

В `TradingRuntimeService` остаётся lifecycle:
- контроль очереди, статусов и state-machine automaton;
- reconciliation и обновление цикла;
- запись фактов (`save_decision`, snapshot, audit, outbox);
- исполнение уже сформированного плана через `IntentService`.

## Контракт `DecisionPlan`

- `indicators`: актуальные `MarketIndicators` (caching-aware);
- `context`: первичная раскладка принятия решения;
- `decision`: итоговое решение после полной оценки (включая пересчет по комиссии);
- `estimated_commission`: комиссия оценки для решения с execution;
- `context_after_estimate`: контекст после пересчёта по оценочной комиссии (если применялся).

## Последовательность внутри `process_position`

1. Получить `order_book` и проверить состояние торговой сессии.
2. Обновить `TradingCycleState` (flat entry / rearm and low observation).
3. Загрузить индикаторы через `TradingDecisionPlanner.load_market_indicators`.
4. Построить план решения через `TradingDecisionPlanner.plan`.
5. Зафиксировать snapshot и факты решения.
6. Если решение требует исполнения — передать в `IntentService`.

## Граничные инварианты

- Эксплуатационная ошибка в загрузке свечей не ломает пайплайн: fallback на `FALLBACK` индикаторы.
- Для `BUY_MORE/SELL_PART/SELL_ALL` сначала запрашивается estimate limit order, затем решение
  пересчитывается с учетом комиссии.
- При `MARKET_SESSION_CLOSED` возвращается `WAIT` без запроса на estimate.

## Рекомендуемые следующие шаги

- [x] Вынесены этапы исполнения в отдельный `DecisionExecutionService`.
- Добавлены unit-тесты для `DecisionExecutionService`:
  - `tests/trading_automaton/services/test_decision_execution_service.py`.
- `TradingRuntimeService` переведён в роль lifecycle-контроллера на этапе сохранения решения и
  делегации исполнения в отдельный слой.
