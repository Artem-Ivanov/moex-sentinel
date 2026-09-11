# Decision Mechanics Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development; implement and independently review each bounded task.

**Goal:** Закрыть 0.8 на существующем streaming runtime.
**Architecture:** Validated market context -> pure decision -> atomic execution/cycle state.
**Tech Stack:** Python 3.12, Pydantic, SQLAlchemy, SQLite, PostgreSQL 16, pytest.
**Spec:** `docs/superpowers/specs/2026-09-09-decision-mechanics-design.md`.

## Constraints

Не читать токены/счета и не использовать sandbox как тестовые данные.
Git unborn: не создавать искусственную историю/worktree, индекс не менять.
Скрипты разработки только в develop. Нет новых API, колонок и настроек стратегии.

## Task 1: Validated market context (root)

Files: `services/order_book_validation.py`, `services/streaming_cycle_transition.py`,
`services/streaming_batch_tick.py`, их tests. Общая проверка до cycle side effects;
пустая/нефинитная книга не должна ломать materialization или позиционный batch.

- [x] RED: stale/invalid snapshot не меняет pending_low; NaN/future/zero depth отвергаются.
- [x] GREEN: применить одну проверку до cycle и scheduler; сохранить причины WAIT.
- [x] Проверить свежий снимок после отказа, границы TTL, существующий batch pipeline.

## Task 2: Confirmed buys and net profit (decision_scope)

Files: `services/strategies.py`, `services/decision.py`, `services/market_indicators.py`,
`domain/dtos.py` при необходимости; соответствующие strategy/decision/indicator tests.
Не менять materializer/repository и файлы Task1.

- [x] RED: отсутствие полного окна не разрешает BUY; downtrend/upper range и net take-profit boundaries.
- [x] GREEN: reuse existing 5/20/10 context and 0.75 range rule; commission-aware full exit.
- [x] Нормализовать завершённые свечи, проверить боковик/рост/падение и детерминизм.

## Task 3: Durable execution cycle (execution_cycle)

Files: `storage/repository.py`, `services/decision_materialization.py`,
новые execution-cycle integration tests; существующие finalization tests.
Использовать существующий JSON для candle timestamp и TradingCycleService для
переходов, без новой колонки. Владение этими файлами только у Task3.

- [x] RED: real BUY finalization не устанавливает cooldown; SELL не disarm; replay/reopening.
- [x] GREEN: обновить цикл в одной транзакции с execution facts, нулевые fills не изменяют цикл.
- [x] Проверить same-candle WAIT после fill, next-candle eligibility, rollback, replay и legacy fallback.

## Task 4: Acceptance

- [x] Независимый review итогового scope и необходимые scoped fixes.
- [x] Полный pytest с отдельным PostgreSQL 16, develop tests, Ruff/Black.
- [x] Roadmap, strategy doc, устаревший ADR и отчёт приёмки.
- [x] Собрать и штатно обновить Compose, подтвердить health и доставку фактов.

## Execution ledger

- Authorization: user requested next milestone and delegation; scope from roadmap 0.8.
- Decisions: existing SMA20 defines confirmation window; no new jump threshold;
  full take-profit target interpreted net of commissions per 0.8.2;
  stop-loss remains price-based. Costs if changed: entry availability and full-exit timing.
- Worktree: no HEAD exists; shared current workspace with disjoint file ownership.
- Task1: RED 13 initial guards + 5 batch cases; independent review requested
  shared snapshot clock and preserved WAIT reasons. Scoped fix RED 6 -> GREEN,
  re-review APPROVED with 52 tests. Empty/NaN reason is logged without valuation.
- Task2: RED 24 -> GREEN; 330 service/pipeline tests; independent review APPROVED.
  Contiguous last 20 completed minute candles required; duplicate conflicts excluded.
- Task3: initial implementation 114 tests; review reproduced stale WAIT erasing
  SELL cycle. Fixed with SQL CAS from pre-observation cycle; stale actionable
  decision becomes WAIT/CYCLE_STATE_CHANGED without intent. Fix suite 136 passed;
  scoped re-review APPROVED, 83 independent tests.
- Interim full regression: 839 passed with PostgreSQL 16, 17 dependency warnings.
  Logging test isolated logger.disabled state left by application startup tests.
- Final regression: 844 passed; develop/docs/architecture/Compose 26 passed;
  Ruff clean; Black 344 files unchanged; three images built and deployed.
- Deployment: graceful Worker stop exit 0; existing volumes and schema preserved;
  frontend health OK, six IN_WORK, no UNCERTAIN, no summary errors. Acceptance:
  `docs/milestone-0.8-acceptance-2026-09-09.md`.
- Operational gate reopened: later delivery rejected private decision_candle_at
  leaking into immutable order strategy_snapshot. Worker stopped gracefully.
  Fix adds real Worker/Core execution-graph contract test; separate develop
  repair validates and retries only unaccepted contaminated events.
- Operational gate closed: canonical decision snapshot restored in lifecycle
  facts, real HTTP execution-graph tests RED -> GREEN; review APPROVED (36 tests).
  Full suite 846 passed; develop/docs/architecture/Compose 42 passed;
  Ruff clean, Black 347 unchanged. Repair dry-run validated 3 groups/9 changes;
  apply acknowledged all 78 facts, noop rerun confirmed, journal removed.
  Updated Worker running, six IN_WORK, zero UNCERTAIN, empty outbox, healthy API.
