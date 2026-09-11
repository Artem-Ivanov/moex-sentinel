# Broker Account Snapshot Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ограничить чтение портфеля и свободных средств одним удалённым обновлением на счёт в минуту, сохраняя fail-closed поведение торговых тиков.

**Architecture:** Долгоживущая `BrokerSdkSession` владеет in-memory TTL-кэшем позиций и денежных остатков. Все потребители сессии, включая reconciliation, используют его автоматически; terminal order инвалидирует счёт. `BrokerRuntimeService` удерживает runtime при retryable broker error и не открывает market ticks до успешной подготовки.

**Tech Stack:** Python 3.13, asyncio, T-Invest async SDK, pytest.

## Global Constraints

- TTL равен 60 секундам.
- Кэш пуст после каждого старта процесса и не сохраняется в SQLite или Redis.
- Просроченное значение не возвращается при неудачном обновлении.
- После terminal order кэш позиций и денег соответствующего счёта удаляется.
- Коммиты не создаются.
- Каждый production-шаг выполняется только после наблюдаемого RED-теста.

---

### Task 1: Broker-session TTL cache

**Files:**
- Modify: `src/trading_automaton/adapters/tinvest_broker_session.py`
- Modify: `tests/trading_automaton/adapters/test_tinvest_broker_session.py`

**Interfaces:**
- `BrokerSdkSession(..., snapshot_ttl_seconds: float = 60.0, monotonic: Callable[[], float] = time.monotonic)`.
- `async invalidate_account_snapshot(account_id: str) -> None`.

- [ ] Add RED tests proving repeated `get_positions`, `inspect_position`, and currency cash reads reuse one SDK response before 60 seconds.
- [ ] Add RED test proving reads after 60 seconds call the SDK again and a failed refresh does not return the expired value.
- [ ] Implement account-scoped position and money cache entries guarded by an asyncio lock; cache all currencies returned by one money response.
- [ ] Add RED test proving terminal `get_order_state` invalidates both position and money entries.
- [ ] Invalidate the account after `FILLED`, `REJECTED`, or `CANCELLED` results from dispatch, exact lookup, or idempotency lookup.
- [ ] Run `uv run pytest -q tests/trading_automaton/adapters/test_tinvest_broker_session.py` and expect all tests to pass.

### Task 2: Fail-closed retryable preparation

**Files:**
- Modify: `src/trading_automaton/services/broker_runtime.py`
- Modify: `tests/trading_automaton/services/test_broker_runtime_service.py`

**Interfaces:**
- Consumes `TInvestAdapterError.retryable` from broker preparation.
- Keeps `_prepared` unset after a failed refresh and retries after a bounded delay without terminating the runtime task.

- [ ] Add a RED test where preparation first raises retryable `BROKER_RATE_LIMITED`, then succeeds; assert runtime task remains alive and trading tick runs only after success.
- [ ] Add a test that a non-retryable preparation exception still terminates the runtime task.
- [ ] Catch only retryable `TInvestAdapterError` in `_control_ticks`, log its safe code, clear `_prepared`, and wait `max(tick_seconds, 5.0)` before retry.
- [ ] Run `uv run pytest -q tests/trading_automaton/services/test_broker_runtime_service.py` and expect all tests to pass.

### Task 3: Verification and runtime acceptance

**Files:**
- Modify: `docs/clean-slate-cutover.md`

**Interfaces:**
- Documents fixed 60-second local TTL; no Redis configuration is introduced.

- [ ] Run focused adapter/runtime/preparation/reconciliation tests.
- [ ] Run Ruff and Black checks for changed Python files.
- [ ] Rebuild only `trading-automaton` with `STRATEGY_ENABLED=false` on the baseline volumes.
- [ ] Verify logs no longer show per-second portfolio calls or broker-runtime recreation; inspect only aggregate Core/Worker states.
- [ ] Re-enable strategy only after clean shutdown and stable reconciliation.
- [ ] Run full backend and frontend test/build verification.
