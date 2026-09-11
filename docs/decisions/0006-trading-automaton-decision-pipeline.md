# ADR 0006: Decision pipeline in `trading_automaton`

- Status: Accepted
- Date: 2026-08-09

Уточнение 2026-09-09 (0.8): полный take-profit рассчитывается по чистой
прибыли после обеих комиссий; покупки требуют готового минутного окна и
общего фильтра тренда/диапазона. Актуальные правила —
[торговая стратегия](../trading-strategy.md). Лимитов суммы позиции нет;
недостаток доступных средств приводит к WAIT.

## Зачем это нужно

На этапе refactoring для `trading_automaton` была зафиксирована одна ответственная
цепочка принятия решения:

1) быстрая проверка технических ограничений (готовность ядра, активный intent);
2) защита по риск-правилам по уже открытой позиции;
3) делегирование в стратегию выбора действий.

Эта ADR устраняет размытость между `runtime`, `streaming_*` и `decision` сервисами
за счёт явного разделения правил и выбора стратегии.

## Контракт для `TradeDecisionService`

`TradeDecisionService` принимает `DecisionContext` и возвращает `TradeDecision`
без side effects.

- `core_available == False` → `WAIT(CORE_UNAVAILABLE)`
- `has_active_intent == True` → `WAIT(ACTIVE_INTENT)`
- для открытой позиции (`quantity_lots > 0`):
  - если `current_price <= average_price * (1 - stop_loss_percent/100)` →
    `SELL_ALL(STOP_LOSS)`
  - если `current_price >= average_price * (1 + take_profit_percent/100)` →
    `SELL_ALL(TAKE_PROFIT)`
- иначе делегирование стратегии (по умолчанию `AdaptiveScalpingStrategy`).
- для новой позиции (`quantity_lots == 0`) сразу делегирование стратегии.

Важно: стратегию для данного этапа выбирает только `TradeDecisionService`
через внедрённый `TradingStrategy`; в текущей версии это всегда
`AdaptiveScalpingStrategy`, если не передан альтернативный объект.

## Описание случаев тестирования (регрессионный список)

| ID | Случай | Ожидаемое |
|---|---|---|
| D-01 | Недоступен core | `WAIT(CORE_UNAVAILABLE)` |
| D-02 | Есть активный intent | `WAIT(ACTIVE_INTENT)` |
| D-03 | Новая позиция, входной сигнал | решение по стратегии |
| D-04 | Открытая позиция, падение до стоп-лосса | `SELL_ALL(STOP_LOSS)` |
| D-05 | Открытая позиция, достижение take-profit | `SELL_ALL(TAKE_PROFIT)` |
| D-06 | Нет условий для частичной фиксации | `WAIT(COMMISSION_EXCEEDS_PARTIAL_PROFIT)` |
| D-07 | Усреднение при недостатке свободных средств с комиссией | `WAIT(INSUFFICIENT_FREE_CASH)` на стадии materialization |
| D-08 | Условия повторного partial-маркера | `NO_ACTION`/`WAIT` согласно стратегии |

### Где это проверено

- `tests/trading_automaton/services/test_decision_service.py`
- `tests/trading_automaton/services/test_adaptive_scalping_strategy.py`

## Интерфейсы для последующего расширения

Сейчас `TradeDecisionService` опирается на:

- `DecisionRule` (`evaluate(context) -> TradeDecision | None`) — цепочка предикатов.
- `TradingStrategy` (`decide(context) -> TradeDecision`).

Это даёт возможность:
- добавить новые правила (например, лимит дневного риска);
- добавить стратегию выбора по `context.strategy` / внешней конфигурации;
- подключить диагностический слой для замера времени прохождения правил и решения.

## Audit-заметка по связности

В `runtime` и `streaming` слоях решение уже выносится в сервис и используется как
внедрённый dependency. Однако есть точка пересборки решения после предварительной
оценки комиссии (runtime) и повторного вызова `decide()` — это отдельное место
для будущего рефакторинга, но оно не входит в рамки текущего этапа.
