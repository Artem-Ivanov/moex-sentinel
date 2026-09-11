# Trading Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить независимый сбор снимков портфеля и read-only сводку торговли по валютам с P&L за 24 часа, 7 и 30 суток на странице `/positions`.

**Architecture:** `portfolio-snapshot-worker` является отдельным deployment-процессом внутри границы Core: он использует Core composition, broker adapters и PostgreSQL repositories, но не зависит от HTTP backend и не имеет доступа к SQLite торгового Worker. Каждый запуск имеет общий `captured_at`, сохраняет успешные снимки и безопасные ошибки; API строит сводку только из PostgreSQL. Уникальный минутный bucket и PostgreSQL advisory lock делают повторный и параллельный запуск идемпотентным.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, PostgreSQL 16, Alembic baseline `0001_baseline`, asyncio, Vue 3, TypeScript, Vitest, pytest.

## Global Constraints

- Все persisted-времена нормализуются до UTC с точностью до миллисекунд через `sentinel_contracts.time.normalize_utc_ms`.
- Снимки собираются каждые `PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS`; значение по умолчанию — `60`.
- Периоды скользящие: `24h`, `7d` и `30d`; валюты агрегируются раздельно без конвертации.
- Первый снимок счёта и валюты получает накопленный P&L `0`.
- Формула: `previous_cumulative_pnl + current_value - previous_value - deposits + withdrawals`.
- Если денежные операции между снимками нельзя получить полностью, снимок этого счёта не записывается.
- Ошибка одного счёта не блокирует остальные; API возвращает сохранённые безопасные ошибки последнего запуска.
- Celery и Redis не добавляются; остановка snapshot-worker не останавливает backend или торговый Worker.
- Единственная схема Core остаётся `0001_baseline`; исторические volumes не мигрируются и не удаляются.
- Маршрут страницы остаётся `/positions`; меняются название навигации и заголовок на «Торговля».
- Перед каждым commit проверить `git diff --cached`; из-за существующего несформированного initial commit использовать `git commit --only -- <paths>`, чтобы не включить чужие staged-файлы.

---

## File Structure

- `src/moex_sentinel/domain/trading_summary.py` — immutable records, cash-flow classification and deterministic P&L/window calculations.
- `src/moex_sentinel/storage/models/trading_analytics.py` — snapshot run and account/currency snapshot tables.
- `src/moex_sentinel/storage/repositories/portfolio_snapshots.py` — locking, idempotent writes and historical reads.
- `src/moex_sentinel/services/portfolio_snapshot_collection.py` — one collection run, broker retry/pagination and account isolation.
- `src/moex_sentinel/services/trading_summary.py` — PostgreSQL-only aggregation into the API view.
- `src/moex_sentinel/portfolio_snapshot_worker.py` — signal-aware periodic process.
- `frontend/src/components/TradingSummary.vue` — isolated summary cards; page orchestration stays in `PositionsListView.vue`.

### Task 1: Domain math and API-neutral records

**Files:**
- Create: `src/moex_sentinel/domain/trading_summary.py`
- Create: `tests/domain/test_trading_summary.py`

**Interfaces:**
- Consumes: `Money`, executed `ExternalOperation` values and UTC timestamps.
- Produces: `CashFlowTotals`, `PortfolioSnapshotValue`, `TradingPnlPeriod`, `CurrencyTradingSummary`, `TradingSummaryView`, `advance_cumulative_pnl(...)`, `cash_flows(...)`, and `period_from_snapshots(...)`.

- [ ] **Step 1: Write failing formula and classification tests**

```python
def test_advance_cumulative_pnl_excludes_deposits_and_withdrawals() -> None:
    assert advance_cumulative_pnl(
        previous_cumulative=Decimal("12"),
        previous_value=Decimal("100"),
        current_value=Decimal("145"),
        deposits=Decimal("50"),
        withdrawals=Decimal("5"),
    ) == Decimal("12")


def test_cash_flows_only_count_executed_money_movements_in_currency() -> None:
    result = cash_flows(operations, currency="RUB")
    assert result == CashFlowTotals(deposits=Decimal("100"), withdrawals=Decimal("25"))
```

Fixtures must include executed input/output, a rejected input, a security transfer, a different currency and a commission. Only executed monetary input/output families count, and signs are normalized with `abs(payment.amount)`.

- [ ] **Step 2: Run the focused tests and observe the missing module**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q tests/domain/test_trading_summary.py`

Expected: FAIL during collection with `ModuleNotFoundError: moex_sentinel.domain.trading_summary`.

- [ ] **Step 3: Implement immutable records and pure calculations**

```python
class CashFlowTotals(PositionalModel):
    model_config = ConfigDict(frozen=True)
    deposits: Decimal
    withdrawals: Decimal


def advance_cumulative_pnl(*, previous_cumulative: Decimal, previous_value: Decimal,
                           current_value: Decimal, deposits: Decimal,
                           withdrawals: Decimal) -> Decimal:
    return previous_cumulative + current_value - previous_value - deposits + withdrawals
```

Define `INPUT_OPERATION_TYPES` and `OUTPUT_OPERATION_TYPES` explicitly for monetary T-Invest operation names. `period_from_snapshots(latest, baseline, requested_from)` returns `value`, `from_at`, `to_at`, and `complete`; `complete` is true only when the selected history reaches the requested start.

- [ ] **Step 4: Add boundary tests for first history, incomplete history and UTC milliseconds**

```python
def test_period_marks_earliest_snapshot_as_incomplete() -> None:
    period = period_from_snapshots(latest, earliest, requested_from=latest.captured_at - timedelta(days=7))
    assert period.value == latest.cumulative_pnl - earliest.cumulative_pnl
    assert period.from_at == earliest.captured_at
    assert period.complete is False
```

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q tests/domain/test_trading_summary.py`

Expected: PASS.

- [ ] **Step 5: Commit only Task 1 paths**

```bash
git diff --cached
git commit --only -m "feat: add trading summary calculations" -- src/moex_sentinel/domain/trading_summary.py tests/domain/test_trading_summary.py
```

### Task 2: Baseline schema and snapshot repository

**Files:**
- Modify: `src/moex_sentinel/storage/models/trading_analytics.py`
- Modify: `src/moex_sentinel/storage/models/__init__.py`
- Modify: `alembic/versions/0001_baseline.py`
- Create: `src/moex_sentinel/storage/repositories/portfolio_snapshots.py`
- Create: `tests/storage/test_portfolio_snapshot_repository.py`
- Modify: `tests/migrations/test_baseline_schema.py`
- Modify: `tests/integration/postgresql/test_schema_runtime.py`

**Interfaces:**
- Consumes: `PortfolioSnapshotValue` and one SQLAlchemy `sessionmaker[Session]`.
- Produces: `portfolio_snapshot_run_lock(engine, bucket_start)`, `PortfolioSnapshotRepository.append_run_with_snapshots(...)`, `latest(...)`, `latest_run()`, `latest_snapshots(run_id)`, and `baseline(user_broker_id, account_id, currency, at_or_before)`.

- [ ] **Step 1: Write failing model and repository tests**

```python
def test_same_account_currency_minute_is_idempotent(repository) -> None:
    first = repository.append_snapshot(snapshot(id="snapshot-1"))
    second = repository.append_snapshot(snapshot(id="snapshot-2"))
    assert second.id == first.id
    assert repository.count_snapshots() == 1


def test_baseline_returns_latest_row_at_or_before_target(repository) -> None:
    repository.append_snapshot(snapshot(captured_at=NOW - timedelta(minutes=2)))
    expected = repository.append_snapshot(snapshot(captured_at=NOW - timedelta(minutes=1)))
    repository.append_snapshot(snapshot(captured_at=NOW))
    assert repository.baseline("broker-1", "account-1", "RUB", NOW - timedelta(seconds=30)) == expected
```

Also assert that one run persists `safe_errors` as JSON without broker credentials and that the SQLite lock context yields `True` for unit tests.

- [ ] **Step 2: Run focused tests and verify schema failures**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q tests/storage/test_portfolio_snapshot_repository.py tests/migrations/test_baseline_schema.py`

Expected: FAIL because run/bucket/account columns and repository do not exist.

- [ ] **Step 3: Evolve the clean baseline model**

`PortfolioSnapshotRunModel` fields:

```python
id: Mapped[str]
captured_at: Mapped[datetime]
bucket_start: Mapped[datetime]  # unique
safe_errors: Mapped[list[dict[str, object]]]  # JSON, default=list
created_at: Mapped[datetime]
```

`PortfolioSnapshotModel` fields:

```python
run_id: Mapped[str]             # FK portfolio_snapshot_runs.id
user_broker_id: Mapped[str]     # FK user_brokers.id
account_id: Mapped[str]
currency: Mapped[str]
total_value: Mapped[Decimal]
free_cash: Mapped[Decimal]
cumulative_pnl: Mapped[Decimal]
captured_at: Mapped[datetime]
bucket_start: Mapped[datetime]
created_at: Mapped[datetime]
```

Replace unused `realized_pnl`, `unrealized_pnl`, and `net_pnl` portfolio columns. Add unique constraint `(user_broker_id, account_id, currency, bucket_start)` and index `(user_broker_id, account_id, currency, captured_at)`. Keep non-negative checks only for `total_value` and `free_cash`; cumulative P&L may be negative.

- [ ] **Step 4: Implement repository locking and reads**

```python
@contextmanager
def portfolio_snapshot_run_lock(engine: Engine, bucket_start: datetime) -> Iterator[bool]:
    if engine.dialect.name != "postgresql":
        yield True
        return
    key = int.from_bytes(blake2b(bucket_start.isoformat().encode(), digest_size=8).digest(), "big", signed=True)
    with engine.connect() as connection:
        acquired = bool(connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
        try:
            yield acquired
        finally:
            if acquired:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
```

Catch only the unique bucket `IntegrityError`, roll back to a savepoint, and return the existing row; do not swallow unrelated integrity errors. Normalize every input time before query/write.

- [ ] **Step 5: Verify SQLite and PostgreSQL-specific contracts**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q tests/storage/test_portfolio_snapshot_repository.py tests/migrations/test_baseline_schema.py tests/integration/postgresql/test_schema_runtime.py`

Expected: unit/baseline tests PASS; PostgreSQL test is either PASS with configured URL or explicitly skipped.

- [ ] **Step 6: Commit only Task 2 paths**

```bash
git diff --cached
git commit --only -m "feat: persist idempotent portfolio snapshots" -- alembic/versions/0001_baseline.py src/moex_sentinel/storage/models/trading_analytics.py src/moex_sentinel/storage/models/__init__.py src/moex_sentinel/storage/repositories/portfolio_snapshots.py tests/storage/test_portfolio_snapshot_repository.py tests/migrations/test_baseline_schema.py tests/integration/postgresql/test_schema_runtime.py
```

### Task 3: Account-isolated snapshot collection

**Files:**
- Modify: `src/moex_sentinel/services/portfolio_ports.py`
- Modify: `src/moex_sentinel/adapters/tinvest/portfolio.py`
- Modify: `tests/adapters/test_tinvest_portfolio_adapter.py`
- Create: `src/moex_sentinel/services/portfolio_snapshot_collection.py`
- Create: `tests/services/test_portfolio_snapshot_collection.py`

**Interfaces:**
- Consumes: enabled `Broker` records, `PortfolioPort`, `PortfolioSnapshotRepository`, clock, async sleeper and retry settings.
- Produces: `PortfolioSnapshotCollector.collect_once() -> PortfolioSnapshotRunResult`.

- [ ] **Step 1: Extend operation reads with an explicit interval**

Add optional keyword-only values without breaking current callers:

```python
async def get_operations(self, account_id: str, cursor: str | None, limit: int,
                         instrument_id: str | None = None, *,
                         from_at: datetime | None = None,
                         to_at: datetime | None = None) -> OperationsPage: ...
```

Write an adapter test asserting `GetOperationsByCursorRequest.from_`, `.to`, and `.cursor`; then pass `from_=from_at` and `to=to_at` in `TInvestPortfolioAdapter`.

- [ ] **Step 2: Write failing collection tests**

```python
async def test_first_snapshot_starts_at_zero(collector, repository) -> None:
    result = await collector.collect_once()
    saved = repository.latest("broker-1", "account-1", "RUB")
    assert result.saved == 1
    assert saved.cumulative_pnl == Decimal("0")


async def test_unreadable_cash_operations_skip_only_that_account(collector, repository) -> None:
    result = await collector.collect_once()
    assert repository.latest("broker-1", "broken-account", "RUB") is None
    assert repository.latest("broker-1", "healthy-account", "RUB") is not None
    assert result.errors[0].account_id == "broken-account"
```

Cover deposit, withdrawal, commission-driven value change, multi-page operations, retryable error success on the third attempt, exhausted retry, duplicate run bucket, multi-currency isolation and one broken broker.

- [ ] **Step 3: Run tests and observe the missing collector**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q tests/services/test_portfolio_snapshot_collection.py tests/adapters/test_tinvest_portfolio_adapter.py`

Expected: FAIL with missing collector/interval parameters.

- [ ] **Step 4: Implement collection with bounded retry and transactions**

Use one normalized `captured_at` and minute `bucket_start` per run. Hold the session-level run advisory lock on a dedicated connection while collecting, without keeping a database transaction open across broker calls. For each enabled broker/account, fetch portfolio, read the previous snapshot, fetch all operation pages in `(previous.captured_at, captured_at]`, and calculate cash flows in the portfolio currency. After all accounts finish, persist the run, all successful snapshots and safe errors atomically with `append_run_with_snapshots(...)`. A failed account records only `broker_id`, `broker_name`, `account_id`, stable error `code`, and safe `message`.

```python
async def _retry(self, call: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(self._retry_limit + 1):
        try:
            return await call()
        except TInvestAdapterError as error:
            if not error.retryable or attempt == self._retry_limit:
                raise
            await self._sleep(self._retry_base_seconds * (2**attempt))
    raise AssertionError("unreachable")
```

Do not create a run row when the advisory lock is not acquired. The unique run bucket handles a completed retry, while the single final transaction prevents a crashed collection attempt from leaving a partial run.

- [ ] **Step 5: Run the collector suite**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q tests/services/test_portfolio_snapshot_collection.py tests/adapters/test_tinvest_portfolio_adapter.py tests/services/test_portfolio_aggregation_service.py`

Expected: PASS.

- [ ] **Step 6: Commit only Task 3 paths**

```bash
git diff --cached
git commit --only -m "feat: collect account portfolio snapshots" -- src/moex_sentinel/services/portfolio_ports.py src/moex_sentinel/adapters/tinvest/portfolio.py src/moex_sentinel/services/portfolio_snapshot_collection.py tests/adapters/test_tinvest_portfolio_adapter.py tests/services/test_portfolio_snapshot_collection.py
```

### Task 4: Dedicated Core snapshot process and Compose service

**Files:**
- Modify: `src/moex_sentinel/config.py`
- Modify: `src/moex_sentinel/composition.py`
- Create: `src/moex_sentinel/portfolio_snapshot_worker.py`
- Modify: `pyproject.toml`
- Create: `docker/portfolio-snapshot-worker.Dockerfile`
- Modify: `compose.yml`
- Modify: `.env.example`
- Create: `tests/test_portfolio_snapshot_worker.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: `Settings`, Core session factory and `PortfolioSnapshotCollector`.
- Produces: `build_portfolio_snapshot_collector(...)`, `run_forever(...)`, and CLI `portfolio-snapshot-worker`.

- [ ] **Step 1: Write failing settings and loop tests**

```python
def test_snapshot_interval_defaults_to_sixty_seconds() -> None:
    assert Settings(_env_file=None).portfolio_snapshot_interval_seconds == 60


async def test_worker_collects_immediately_then_waits() -> None:
    await run_forever(collector, interval_seconds=60, stop=stop, wait=wait)
    collector.collect_once.assert_awaited_once()
    wait.assert_awaited_once_with(stop, 60)
```

Validate interval `>= 60`, retry limit `>= 0`, and retry base `> 0`. The loop logs a safe run summary, continues after an unexpected run failure, and exits on SIGTERM/SIGINT.

- [ ] **Step 2: Run the focused tests and observe failures**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q tests/test_portfolio_snapshot_worker.py tests/test_config.py`

Expected: FAIL because settings and entrypoint are absent.

- [ ] **Step 3: Implement composition and signal-aware entrypoint**

```python
async def run_forever(collector: PortfolioSnapshotCollector, *, interval_seconds: int,
                      stop: asyncio.Event, wait: WaitPort = wait_for_stop) -> None:
    while not stop.is_set():
        await collector.collect_once()
        await wait(stop, interval_seconds)
```

Create/dispose the Core engine in `main()`, configure audit logging as component `portfolio-snapshot-worker`, and never import `trading_automaton.storage`.

- [ ] **Step 4: Add the Compose service**

Use the same Python base and source package as backend, run as `sentinel`, depend only on healthy `database`, and provide `DATABASE_URL`, `PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS`, `LOG_LEVEL`, and `LOG_FORMAT`. Do not add Redis, Celery, backend dependency, ports or volumes.

- [ ] **Step 5: Verify process and Compose configuration**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q tests/test_portfolio_snapshot_worker.py tests/test_config.py`

Run: `docker compose -f compose.yml config --quiet`

Expected: both PASS; rendered Compose contains `portfolio-snapshot-worker` and no Redis/Celery service.

- [ ] **Step 6: Commit only Task 4 paths**

```bash
git diff --cached
git commit --only -m "feat: run portfolio snapshot worker" -- .env.example compose.yml docker/portfolio-snapshot-worker.Dockerfile pyproject.toml src/moex_sentinel/config.py src/moex_sentinel/composition.py src/moex_sentinel/portfolio_snapshot_worker.py tests/test_config.py tests/test_portfolio_snapshot_worker.py
```

### Task 5: PostgreSQL-only trading summary API

**Files:**
- Create: `src/moex_sentinel/services/trading_summary.py`
- Create: `src/moex_sentinel/usecases/trading_summary.py`
- Create: `src/moex_sentinel/views/schemas/trading_summary.py`
- Create: `src/moex_sentinel/views/trading_summary.py`
- Modify: `src/moex_sentinel/composition.py`
- Modify: `src/moex_sentinel/api/app.py`
- Create: `tests/services/test_trading_summary_service.py`
- Create: `tests/api/test_trading_summary_contract.py`

**Interfaces:**
- Consumes: latest completed snapshot run plus account/currency baselines from `PortfolioSnapshotRepository`.
- Produces: `ViewTradingSummaryUsecase.execute() -> TradingSummaryView` and `GET /api/trading/summary`.

- [ ] **Step 1: Write failing aggregation tests**

```python
def test_summary_aggregates_accounts_by_currency(repository) -> None:
    seed_latest_run(repository, rub_accounts=("100", "50"), usd_accounts=("20",))
    result = TradingSummaryService(repository).view()
    rub = next(item for item in result.currencies if item.currency == "RUB")
    assert rub.portfolio_value == Decimal("150")
    assert rub.pnl_24h.value == Decimal("7")


def test_summary_uses_common_actual_start_for_incomplete_accounts(repository) -> None:
    result = TradingSummaryService(repository).view()
    assert result.currencies[0].pnl_7d.complete is False
    assert result.currencies[0].pnl_7d.from_at == NEW_ACCOUNT_FIRST_CAPTURE
```

For a currency, choose one common baseline run per window: requested start when every latest account has history, otherwise the newest first-capture among those accounts. Calculate every account against its snapshot at that common run so the returned `from` has one unambiguous meaning.

- [ ] **Step 2: Write failing API contract tests**

```python
def test_trading_summary_contract(client) -> None:
    response = client.get("/api/trading/summary")
    assert response.status_code == 200
    assert response.json()["currencies"][0]["pnl_24h"] == {
        "value": "7.000000000",
        "from": "2026-08-14T10:00:00Z",
        "to": "2026-08-15T10:00:00Z",
        "complete": True,
    }
```

Also cover empty database (`captured_at: null`, `currencies: []`, `errors: []`), separate currencies, negative/zero P&L, incomplete history and saved partial errors.

- [ ] **Step 3: Run tests and observe missing service/route**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q tests/services/test_trading_summary_service.py tests/api/test_trading_summary_contract.py`

Expected: FAIL with missing module/404.

- [ ] **Step 4: Implement query service, schemas and route**

Schema shape:

```python
class TradingPnlPeriodSchema(PositionalModel):
    value: Decimal | None
    from_: datetime | None = Field(serialization_alias="from")
    to: datetime | None
    complete: bool


class TradingSummarySchema(PositionalModel):
    captured_at: datetime | None
    currencies: tuple[CurrencyTradingSummarySchema, ...]
    errors: tuple[BrokerReadErrorSchema, ...]
```

The endpoint must only call the synchronous repository-backed service and must not receive an adapter factory. Register its use case in `ApplicationUsecases` and router in `create_app`.

- [ ] **Step 5: Run service and API tests**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q tests/services/test_trading_summary_service.py tests/api/test_trading_summary_contract.py tests/api/test_health.py`

Expected: PASS.

- [ ] **Step 6: Commit only Task 5 paths**

```bash
git diff --cached
git commit --only -m "feat: expose historical trading summary" -- src/moex_sentinel/services/trading_summary.py src/moex_sentinel/usecases/trading_summary.py src/moex_sentinel/views/schemas/trading_summary.py src/moex_sentinel/views/trading_summary.py src/moex_sentinel/composition.py src/moex_sentinel/api/app.py tests/services/test_trading_summary_service.py tests/api/test_trading_summary_contract.py
```

### Task 6: Trading page summary cards

**Files:**
- Modify: `frontend/src/api/portfolio.ts`
- Modify: `frontend/src/api/portfolio.spec.ts`
- Create: `frontend/src/components/TradingSummary.vue`
- Create: `frontend/src/components/TradingSummary.spec.ts`
- Modify: `frontend/src/views/PositionsListView.vue`
- Modify: `frontend/src/views/PositionsListView.spec.ts`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/App.spec.ts`
- Modify: `frontend/src/style.css`

**Interfaces:**
- Consumes: `fetchTradingSummary() -> Promise<TradingSummaryResponse>`.
- Produces: summary cards with signed P&L state and a page refresh that independently settles summary, automations and operations.

- [ ] **Step 1: Add failing client and component tests**

```typescript
it("loads the persisted trading summary", async () => {
  await fetchTradingSummary()
  expect(fetchMock).toHaveBeenCalledWith("/api/trading/summary")
})

it("shows incomplete and signed periods", () => {
  render(TradingSummary, { props: { summary } })
  expect(screen.getByText("+12,50 ₽").className).toContain("pnl--positive")
  expect(screen.getByText("данные с 14.08.2026")).toBeTruthy()
})
```

Cover positive, negative, zero, missing value (`—`), separate currencies and safe errors. Format values with `Intl.NumberFormat("ru-RU", { style: "currency", currency })`.

- [ ] **Step 2: Run frontend tests and observe failures**

Run: `npm --prefix frontend test -- --run frontend/src/api/portfolio.spec.ts frontend/src/components/TradingSummary.spec.ts frontend/src/App.spec.ts frontend/src/views/PositionsListView.spec.ts`

Expected: FAIL because API function/component/new labels are absent.

- [ ] **Step 3: Implement API types and summary component**

```typescript
export interface TradingPnlPeriod { value: string | null; from: string | null; to: string | null; complete: boolean }
export interface CurrencyTradingSummary { currency: string; portfolio_value: string; free_cash: string; pnl_24h: TradingPnlPeriod; pnl_7d: TradingPnlPeriod; pnl_30d: TradingPnlPeriod }
export interface TradingSummaryResponse { captured_at: string | null; currencies: CurrencyTradingSummary[]; errors: ReadError[] }
export const fetchTradingSummary = () => load<TradingSummaryResponse>("/api/trading/summary")
```

Render heading «Сводка» first, one card per currency, then existing «Открытые позиции» and «Последние операции» sections.

- [ ] **Step 4: Update page orchestration and navigation**

Rename sidebar and page heading to «Торговля» without changing route/name. On initial load and «Обновить», execute summary, automations and operations with independent `Promise.allSettled` handling; retain successful existing block data when another block fails. The one refresh button is disabled only while a refresh is active.

- [ ] **Step 5: Run frontend verification**

Run: `npm --prefix frontend test -- --run`

Run: `npm --prefix frontend run typecheck`

Run: `npm --prefix frontend run build`

Expected: PASS.

- [ ] **Step 6: Commit only Task 6 paths**

```bash
git diff --cached
git commit --only -m "feat: show trading summary on positions page" -- frontend/src/api/portfolio.ts frontend/src/api/portfolio.spec.ts frontend/src/components/TradingSummary.vue frontend/src/components/TradingSummary.spec.ts frontend/src/views/PositionsListView.vue frontend/src/views/PositionsListView.spec.ts frontend/src/App.vue frontend/src/App.spec.ts frontend/src/style.css
```

### Task 7: Contracts, runbook and end-to-end verification

**Files:**
- Modify: `docs/contracts/openapi.yaml`
- Modify: `README.md`
- Modify: `docs/development.md`
- Modify: `docs/clean-slate-cutover.md`
- Modify: `tests/test_documentation.py`
- Create: `tests/integration/test_portfolio_snapshot_worker_isolation.py`

**Interfaces:**
- Consumes: completed worker, schema, API and frontend.
- Produces: synchronized public contract, operator instructions and an isolation smoke test.

- [ ] **Step 1: Write failing documentation and isolation tests**

```python
def test_openapi_documents_trading_summary() -> None:
    contract = yaml.safe_load(Path("docs/contracts/openapi.yaml").read_text())
    assert "/api/trading/summary" in contract["paths"]


def test_snapshot_worker_has_no_sqlite_or_backend_dependency(compose) -> None:
    service = compose["services"]["portfolio-snapshot-worker"]
    assert "AUTOMATON_DATABASE_URL" not in service["environment"]
    assert "backend" not in service.get("depends_on", {})
```

Also assert no Redis/Celery service, default interval `60`, and README/runbook commands mention the new worker without any volume deletion command.

- [ ] **Step 2: Run tests and observe missing contract/docs**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q tests/test_documentation.py tests/integration/test_portfolio_snapshot_worker_isolation.py`

Expected: FAIL on absent endpoint/instructions.

- [ ] **Step 3: Update OpenAPI and operator documentation**

Document exact `TradingSummaryResponse`, period aliases, decimal strings, nullable timestamps and partial error schema. Update startup/verification commands and explain that the first capture shows incomplete periods until enough history exists.

- [ ] **Step 4: Run the complete verification matrix**

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .`

Run: `UV_CACHE_DIR=/tmp/uv-cache uv run black --check .`

Run: `npm --prefix frontend test -- --run`

Run: `npm --prefix frontend run typecheck`

Run: `npm --prefix frontend run build`

Run: `docker compose -f compose.yml config --quiet`

Expected: all commands PASS; PostgreSQL-only tests may be explicitly skipped only when `POSTGRES_TEST_DATABASE_URL` is not configured.

- [ ] **Step 5: Commit only Task 7 paths**

```bash
git diff --cached
git commit --only -m "docs: document trading summary runtime" -- docs/contracts/openapi.yaml README.md docs/development.md docs/clean-slate-cutover.md tests/test_documentation.py tests/integration/test_portfolio_snapshot_worker_isolation.py
```

## Plan Self-Review

- Spec coverage: schema, formula, cash-flow exclusion, rolling windows, incomplete history, isolation, retry, advisory lock, idempotency, API, UI, Compose and smoke verification each map to a task.
- Scope decision: `portfolio_snapshot_runs` is included because partial errors and a consistent `captured_at` cannot be served from PostgreSQL without persisting run metadata.
- Type consistency: `PortfolioSnapshotValue.cumulative_pnl`, common period `from_at`, API alias `from`, and frontend property `from` are intentionally distinct only at serialization.
- No volume deletion, currency conversion, graphing, Redis/Celery or route migration is included.
