# Аудит: trading_automaton.runtime decision stage (2026-08-10)

## Цель

Подтвердить, что пайплайн принятия решений в runtime:
- детерминирован,
- покрыт отдельными регрессионными кейсами,
- не смешивает orchestration с формированием торгового решения.

## Что проверено

1. Классификация решений после получения рыночных сигналов:
   - `WAIT(MARKET_SESSION_CLOSED)` при закрытии сессии;
   - `BUY_MORE/SELL_PART/SELL_ALL` только после пересчёта с фактической оценкой комиссии;
   - fallback reason-коды (`NO_ACTION`, `POSITION_LIMIT_EXCEEDED`, `REVERSAL_NOT_CONFIRMED` и т.п.)
     сохраняются в `TradeDecision`.
2. Модель `DecisionPlan` хранит итоговое решение и `estimated_commission`.
3. `TradingRuntimeService` сохраняет все факты через существующие репозитории, при этом
   принимает решение через отдельный планировщик.

## Наблюдаемые риски после декомпозиции

- Поведение fallback на ошибках получения свечей зависит от того, что исключение пробрасывается
  из `CandleDataPort` в runtime. Runtime обязан логировать `ADAPTIVE_THRESHOLDS_FALLBACK`
  и повторно вызывать planner без свечных данных.
- Синхронизация `DecisionContext` между базовым и после-оценочным ветками теперь централизована:
  требуется проверять в тестах, что `estimated_context` используется для финального `decision`.

## Тестовый матрица

| ID | Описание кейса | Контракт |
|----|----------------|---------|
| D-PL-01 | Сессия закрыта | решение `WAIT` с `MARKET_SESSION_CLOSED`, commission=0 |
| D-PL-02 | Нет действий по стратегии (flat) | решение `NO_ACTION`/`WAIT` без estimate |
| D-PL-03 | Решение требует limit order и комиссия меняет решение | после estimate происходит повторный вызов `decide` |
| D-PL-04 | Ошибка получения свечей | fallback сигналы и reason в логе |
| D-PL-05 | Стратегия вернула actionable сигнал на BUY_MORE | в runtime сохраняется estimate и финальное решение |

## Рекомендуемые проверки

- Добавить unit-тесты для `TradingDecisionPlanner`:
  - с закрытой сессией,
  - с action и estimate path,
  - с no-action кейсом,
  - с fallback при ошибке источника свечей (через runtime-обёртку).
