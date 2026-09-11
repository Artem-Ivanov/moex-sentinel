# Instrument Identity Boundaries Bugfix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Вернуть тикер в последние операции и восстановить открытие карточки инструмента без смешения внутреннего и внешнего идентификаторов.

**Architecture:** Catalog API и FK используют внутренний `id`; брокерские API используют внешний `instrument_id`. Явно названный repository lookup по внешнему UID обслуживает только обогащение брокерских данных.

**Tech Stack:** Python 3.13, SQLAlchemy 2, FastAPI, Vue 3, TypeScript, Vitest, pytest.

## Global Constraints

- Работа выполняется inline в текущем workspace по ранее полученному разрешению пользователя.
- Коммиты не создаются.
- Изменения выполняются через TDD: каждый регрессионный тест сначала обязан упасть по ожидаемой причине.
- Внутренний `id` и внешний `instrument_id` не принимаются одним неоднозначным lookup-методом.

---

### Task 1: Frontend boundaries for catalog and broker calls

**Files:**
- Modify: `frontend/src/views/InstrumentsListView.vue`
- Modify: `frontend/src/views/InstrumentsListView.spec.ts`
- Modify: `frontend/src/views/InstrumentItemView.vue`
- Modify: `frontend/src/views/InstrumentItemView.spec.ts`

**Interfaces:**
- Consumes: `CatalogInstrument.id`, `CatalogInstrument.instrument_id`, `InstrumentDetails.instrument.instrument_id`.
- Produces: route/details/selection/create calls with internal ID; candle calls with external ID.

- [ ] **Step 1: Write failing list-view tests**

Use fixtures where `id="row-1"` and `instrument_id="uid-1"`. Assert that double-click navigation and selection call use `row-1`, not `uid-1`.

- [ ] **Step 2: Verify RED**

Run: `npm test -- src/views/InstrumentsListView.spec.ts`

Expected: route/selection expectations receive `uid-1` on current production code.

- [ ] **Step 3: Implement the list-view boundary**

Use `item.id` for selected-row identity, `setInstrumentSelection`, and `instrument-details` route params. Keep `item.instrument_id` only as broker UID data.

- [ ] **Step 4: Write failing item-view tests**

Mount route `/instruments/broker-1/row-1`; make details return `instrument.instrument_id="uid-1"`. Assert details and automation creation use `row-1`, while historic candles use `uid-1`.

- [ ] **Step 5: Verify RED**

Run: `npm test -- src/views/InstrumentItemView.spec.ts`

Expected: candle request receives `row-1` on current production code.

- [ ] **Step 6: Implement the item-view boundary**

Await details by route ID, assign `data`, then call `loadCandles(brokerId, response.instrument.instrument_id)`. Keep automation creation on the route ID.

- [ ] **Step 7: Verify GREEN**

Run: `npm test -- src/views/InstrumentsListView.spec.ts src/views/InstrumentItemView.spec.ts`

Expected: all selected frontend tests pass.

### Task 2: Scoped external catalog lookup for operation ticker

**Files:**
- Modify: `src/moex_sentinel/storage/repositories/reference_catalog.py`
- Modify: `src/moex_sentinel/services/portfolio.py`
- Modify: `tests/storage/test_reference_catalog_repository.py`
- Modify: `tests/services/test_portfolio_aggregation_service.py`

**Interfaces:**
- Produces: `ReferenceCatalogRepository.find_by_external_instrument_id(user_broker_id: str, external_instrument_id: str) -> UserBrokerCatalogInstrument | None`.
- Consumes: broker operation `instrument_id` as the external UID.

- [ ] **Step 1: Write failing repository test**

Create two broker scopes containing the same external UID. Assert that lookup returns the record from the requested scope and returns `None` for an absent external UID.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest -q tests/storage/test_reference_catalog_repository.py`

Expected: failure because `find_by_external_instrument_id` does not exist.

- [ ] **Step 3: Implement the scoped lookup**

Select `BrokerInstrumentModel` by both `user_broker_id` and `external_instrument_id`; return `_record(model)` or `None`. Do not filter inactive records because historical operations still need their ticker.

- [ ] **Step 4: Write failing service test**

Return an operation with external `instrument_id="uid-1"`; provide a catalog fake whose external lookup returns ticker `TEST`. Assert the resulting operation ticker equals the literal `TEST`.

- [ ] **Step 5: Verify RED**

Run: `uv run pytest -q tests/services/test_portfolio_aggregation_service.py`

Expected: ticker remains `None` because production code still invokes `.get()`.

- [ ] **Step 6: Implement ticker enrichment**

Define a narrow catalog protocol in `portfolio.py`, call `find_by_external_instrument_id`, and update the immutable operation only when a record exists. Remove the broad exception handler.

- [ ] **Step 7: Verify GREEN**

Run: `uv run pytest -q tests/storage/test_reference_catalog_repository.py tests/services/test_portfolio_aggregation_service.py`

Expected: all selected backend tests pass.

### Task 3: Integrated verification and documentation status

**Files:**
- Modify: `docs/clean-slate-cutover.md`

**Interfaces:**
- Consumes: completed frontend and backend identifier contracts.
- Produces: recorded bug cause, fix, and verification evidence.

- [ ] **Step 1: Run focused frontend verification**

Run: `npm test -- src/views/InstrumentsListView.spec.ts src/views/InstrumentItemView.spec.ts src/views/PositionsListView.spec.ts`

- [ ] **Step 2: Run focused backend verification**

Run: `uv run pytest -q tests/storage/test_reference_catalog_repository.py tests/services/test_portfolio_aggregation_service.py tests/api/test_instrument_catalog.py`

- [ ] **Step 3: Run static checks for changed files**

Run: `uv run ruff check src/moex_sentinel/storage/repositories/reference_catalog.py src/moex_sentinel/services/portfolio.py tests/storage/test_reference_catalog_repository.py tests/services/test_portfolio_aggregation_service.py`

Run: `npm run typecheck`

- [ ] **Step 4: Record the resolved regressions**

Add a concise section to `docs/clean-slate-cutover.md` describing the strict internal/external identifier boundary and both regression checks without local record identifiers.
