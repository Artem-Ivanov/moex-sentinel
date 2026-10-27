# Task3 updated review package

Source fivefiles unchanged from previousacceptedhashes; scope re-review test-onlyM1.

## src/moex_sentinel/domain/portfolio.py
SHA256 f607964288301fae286a4c383f16dc39b929a021921538665bc96a97fef2c498
```diff
--- baseline/src/moex_sentinel/domain/portfolio.py
+++ current/src/moex_sentinel/domain/portfolio.py
@@ -128,3 +128,15 @@
     model_config = ConfigDict(frozen=True)
     items: tuple[BrokerOperation, ...]
     errors: tuple[BrokerReadError, ...]
+
+
+class BrokerOperationsPageView(PositionalModel):
+    model_config = ConfigDict(frozen=True)
+    items: tuple[BrokerOperation, ...]
+    errors: tuple[BrokerReadError, ...]
+    input_cursor: str | None
+    next_cursor: str | None
+    source_item_count: int
+    selected_item_count: int
+    source_exhausted: bool | None
+    coverage_complete: bool
```

## src/moex_sentinel/services/portfolio.py
SHA256 bb194239ca0bd8d9be71558b7c328b5ec28a919e6839607979f416233d6919e5
```diff
--- baseline/src/moex_sentinel/services/portfolio.py
+++ current/src/moex_sentinel/services/portfolio.py
@@ -2,6 +2,7 @@
 
 import asyncio
 from collections.abc import Callable, Coroutine
+from datetime import datetime
 from decimal import Decimal
 from functools import partial
 from typing import Any, Protocol
@@ -12,6 +13,7 @@
     BrokerAccountSnapshot,
     BrokerAccountsView,
     BrokerOperation,
+    BrokerOperationsPageView,
     BrokerOperationsView,
     BrokerPosition,
     BrokerPositionsView,
@@ -26,6 +28,7 @@
 from moex_sentinel.services.ports import UserBrokerReadPort
 from moex_sentinel.services.sync_execution import run_sync
 from sentinel_contracts.broker_errors import BrokerOperationError
+from sentinel_contracts.time import floor_utc_millisecond
 
 AdapterFactory = Callable[[UserBroker], PortfolioPort]
 
@@ -117,6 +120,122 @@
             tuple(error for _, errors in results for error in errors),
         )
 
+    async def view_operations_scoped(
+        self,
+        broker_id: str,
+        account_id: str,
+        *,
+        limit: int,
+        cursor: str | None,
+        from_at: datetime,
+        to_at: datetime,
+    ) -> BrokerOperationsPageView:
+        broker = await run_sync(partial(self._brokers.get, broker_id))
+        if broker.is_test is not self._active_test():
+            raise EnvironmentMismatchError("Broker belongs to inactive environment.")
+        if not broker.enabled:
+            return self._operations_page_error(broker, account_id, "BROKER_DISABLED", cursor=cursor)
+        if broker.account_id and broker.account_id != account_id:
+            return self._operations_page_error(broker, account_id, "BROKER_ACCOUNT_SCOPE_MISMATCH", cursor=cursor)
+
+        try:
+            adapter = self._adapter_factory(broker)
+            accounts = await adapter.list_accounts()
+            if not any(account.account_id == account_id for account in accounts):
+                return self._operations_page_error(broker, account_id, "BROKER_ACCOUNT_NOT_FOUND", cursor=cursor)
+            page = await adapter.get_operations(account_id, cursor, limit, from_at=from_at, to_at=to_at)
+        except BrokerOperationError:
+            return self._operations_page_error(broker, account_id, "BROKER_OPERATIONS_UNAVAILABLE", cursor=cursor)
+        except ValueError:
+            return self._operations_page_error(broker, account_id, "BROKER_CONFIGURATION", cursor=cursor)
+
+        if len(page.items) > limit:
+            return self._operations_page_error(
+                broker, account_id, "BROKER_OPERATIONS_INVALID_PAGE", cursor=cursor, source_item_count=len(page.items)
+            )
+        if page.next_cursor is not None and (
+            not page.next_cursor or len(page.next_cursor) > 1024 or page.next_cursor == cursor
+        ):
+            return self._operations_page_error(
+                broker, account_id, "BROKER_OPERATIONS_INVALID_CURSOR", cursor=cursor, source_item_count=len(page.items)
+            )
+
+        unique: dict[str, ExternalOperation] = {}
+        for operation in page.items:
+            if operation.account_id != account_id:
+                return self._operations_page_error(
+                    broker,
+                    account_id,
+                    "BROKER_OPERATIONS_SCOPE_MISMATCH",
+                    cursor=cursor,
+                    source_item_count=len(page.items),
+                )
+            previous = unique.get(operation.operation_id)
+            if previous is not None and previous != operation:
+                return self._operations_page_error(
+                    broker,
+                    account_id,
+                    "BROKER_OPERATIONS_CONFLICTING_DUPLICATE",
+                    cursor=cursor,
+                    source_item_count=len(page.items),
+                )
+            unique[operation.operation_id] = operation
+
+        try:
+            selected = tuple(
+                operation
+                for operation in unique.values()
+                if operation.operation_type not in NON_TRADING_OPERATION_TYPES
+                and from_at < floor_utc_millisecond(operation.occurred_at) <= to_at
+            )
+        except ValueError:
+            return self._operations_page_error(
+                broker,
+                account_id,
+                "BROKER_OPERATIONS_INVALID_TIMESTAMP",
+                cursor=cursor,
+                source_item_count=len(page.items),
+            )
+
+        items = [BrokerOperation(broker.id, broker.display_name, operation) for operation in selected]
+        items.sort(key=lambda item: item.operation.operation_id)
+        items.sort(key=lambda item: floor_utc_millisecond(item.operation.occurred_at), reverse=True)
+        source_exhausted = page.next_cursor is None
+        return BrokerOperationsPageView(
+            items=tuple(items),
+            errors=(),
+            input_cursor=cursor,
+            next_cursor=page.next_cursor,
+            source_item_count=len(page.items),
+            selected_item_count=len(items),
+            source_exhausted=source_exhausted,
+            coverage_complete=cursor is None and source_exhausted,
+        )
+
+    @staticmethod
+    def _operations_page_error(
+        broker: UserBroker,
+        account_id: str,
+        code: str,
+        *,
+        cursor: str | None = None,
+        source_item_count: int = 0,
+    ) -> BrokerOperationsPageView:
+        return BrokerOperationsPageView(
+            items=(),
+            errors=(
+                BrokerReadError(
+                    broker.id, broker.display_name, account_id, code, "Не удалось прочитать историю операций."
+                ),
+            ),
+            input_cursor=cursor,
+            next_cursor=None,
+            source_item_count=source_item_count,
+            selected_item_count=0,
+            source_exhausted=None,
+            coverage_complete=False,
+        )
+
     async def view_position_operations(
         self,
         broker_id: str,
```

## src/moex_sentinel/usecases/portfolio.py
SHA256 e9188e5acac64dcaa627daeee752a7c0458972dc9d3600d7b8e153138578c6b3
```diff
--- baseline/src/moex_sentinel/usecases/portfolio.py
+++ current/src/moex_sentinel/usecases/portfolio.py
@@ -1,10 +1,12 @@
 """User intents for viewing read-only broker data."""
 
+from datetime import datetime
 from typing import Protocol
 
 from moex_sentinel.domain.brokers import BrokerRecordNotFoundError
 from moex_sentinel.domain.portfolio import (
     BrokerAccountsView,
+    BrokerOperationsPageView,
     BrokerOperationsView,
     BrokerPositionsView,
 )
@@ -20,6 +22,17 @@
     async def view_positions(self) -> BrokerPositionsView: ...
 
     async def view_operations(self, limit: int) -> BrokerOperationsView: ...
+
+    async def view_operations_scoped(
+        self,
+        broker_id: str,
+        account_id: str,
+        *,
+        limit: int,
+        cursor: str | None,
+        from_at: datetime,
+        to_at: datetime,
+    ) -> BrokerOperationsPageView: ...
 
 
 class ViewBrokerAccountsUsecase:
@@ -59,3 +72,31 @@
         if not 1 <= limit <= 500:
             raise UseCaseError("INVALID_OPERATIONS_LIMIT", "Лимит должен быть от 1 до 500.")
         return await self._service.view_operations(limit)
+
+    async def execute_scoped(
+        self,
+        broker_id: str,
+        account_id: str,
+        *,
+        limit: int,
+        cursor: str | None,
+        from_at: datetime,
+        to_at: datetime,
+    ) -> BrokerOperationsPageView:
+        if not 1 <= limit <= 500:
+            raise UseCaseError("INVALID_OPERATIONS_LIMIT", "Лимит должен быть от 1 до 500.")
+        if cursor is not None and (not cursor or len(cursor) > 1024):
+            raise UseCaseError("INVALID_OPERATIONS_CURSOR", "Проверьте курсор истории операций.")
+        try:
+            return await self._service.view_operations_scoped(
+                broker_id,
+                account_id,
+                limit=limit,
+                cursor=cursor,
+                from_at=from_at,
+                to_at=to_at,
+            )
+        except BrokerRecordNotFoundError as error:
+            raise UseCaseError("BROKER_NOT_FOUND", "Подключение брокера не найдено.") from error
+        except EnvironmentMismatchError as error:
+            raise UseCaseError("BROKER_ENVIRONMENT_MISMATCH", "Брокер относится к другому контуру.") from error
```

## src/moex_sentinel/views/portfolio.py
SHA256 87cf8b3708b5a8320d76d3c4fd483aac6d8971da1a962e19d9d9ce7a4306ead8
```diff
--- baseline/src/moex_sentinel/views/portfolio.py
+++ current/src/moex_sentinel/views/portfolio.py
@@ -1,13 +1,18 @@
 """Thin read-only HTTP views for broker portfolio data."""
 
-from fastapi import APIRouter, Query, Request
+from datetime import UTC, datetime
+from typing import Annotated
+
+from fastapi import APIRouter, HTTPException, Query, Request
 
 from moex_sentinel.views.dependencies import usecases as _usecases
 from moex_sentinel.views.schemas.portfolio import (
     AccountsResponseSchema,
+    OperationsPageResponseSchema,
     OperationsResponseSchema,
     PositionsResponseSchema,
 )
+from sentinel_contracts.time import floor_utc_millisecond
 
 router = APIRouter(tags=["portfolio"])
 
@@ -37,3 +42,32 @@
 ) -> OperationsResponseSchema:
     result = await _usecases(request).view_recent_operations.execute(limit)
     return OperationsResponseSchema.from_domain(result)
+
+
+@router.get("/operations/scoped", response_model=OperationsPageResponseSchema)
+async def scoped_operations(
+    request: Request,
+    broker_id: Annotated[str, Query(min_length=1)],
+    account_id: Annotated[str, Query(min_length=1)],
+    from_at: datetime,
+    to_at: datetime,
+    limit: Annotated[int, Query(ge=1, le=500)] = 100,
+    cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
+) -> OperationsPageResponseSchema:
+    try:
+        from_utc = floor_utc_millisecond(from_at)
+        to_utc = floor_utc_millisecond(to_at)
+    except ValueError as error:
+        raise HTTPException(status_code=422, detail="Даты операций должны содержать часовой пояс.") from error
+    if from_utc >= to_utc or to_at.astimezone(UTC) > datetime.now(UTC):
+        raise HTTPException(status_code=422, detail="Проверьте временной интервал операций.")
+
+    result = await _usecases(request).view_recent_operations.execute_scoped(
+        broker_id,
+        account_id,
+        limit=limit,
+        cursor=cursor,
+        from_at=from_utc,
+        to_at=to_utc,
+    )
+    return OperationsPageResponseSchema.from_domain(result)
```

## src/moex_sentinel/views/schemas/portfolio.py
SHA256 9f5e31f6a804540427cb8015664d9c69cf7dddbf0019683eff8594d54e40df6d
```diff
--- baseline/src/moex_sentinel/views/schemas/portfolio.py
+++ current/src/moex_sentinel/views/schemas/portfolio.py
@@ -7,6 +7,7 @@
 
 from moex_sentinel.domain.portfolio import (
     BrokerAccountsView,
+    BrokerOperationsPageView,
     BrokerOperationsView,
     BrokerPositionsView,
     Money,
@@ -162,3 +163,44 @@
             ],
             errors=[ReadErrorSchema.model_validate(item, from_attributes=True) for item in value.errors],
         )
+
+
+class OperationsPageResponseSchema(StrictSchema):
+    items: list[OperationSchema]
+    errors: list[ReadErrorSchema]
+    input_cursor: str | None
+    next_cursor: str | None
+    source_item_count: int
+    selected_item_count: int
+    source_exhausted: bool | None
+    coverage_complete: bool
+
+    @classmethod
+    def from_domain(cls, value: BrokerOperationsPageView) -> "OperationsPageResponseSchema":
+        return cls(
+            items=[
+                OperationSchema(
+                    broker_id=item.broker_id,
+                    broker_name=item.broker_name,
+                    operation_id=item.operation.operation_id,
+                    account_id=item.operation.account_id,
+                    operation_type=item.operation.operation_type,
+                    state=item.operation.state,
+                    occurred_at=item.operation.occurred_at,
+                    payment=MoneySchema.from_domain(item.operation.payment),
+                    price=MoneySchema.from_domain(item.operation.price),
+                    quantity=item.operation.quantity,
+                    commission=MoneySchema.from_domain(item.operation.commission),
+                    instrument_id=item.operation.instrument_id,
+                    ticker=item.operation.ticker,
+                )
+                for item in value.items
+            ],
+            errors=[ReadErrorSchema.model_validate(item, from_attributes=True) for item in value.errors],
+            input_cursor=value.input_cursor,
+            next_cursor=value.next_cursor,
+            source_item_count=value.source_item_count,
+            selected_item_count=value.selected_item_count,
+            source_exhausted=value.source_exhausted,
+            coverage_complete=value.coverage_complete,
+        )
```

## tests/services/test_portfolio_aggregation_service.py
SHA256 5caf68ba9dbfa906283e55473d1c036351d3938c5cb68319f4a69b920d52429c
```diff
--- baseline/tests/services/test_portfolio_aggregation_service.py
+++ current/tests/services/test_portfolio_aggregation_service.py
@@ -1,5 +1,5 @@
 import asyncio
-from datetime import UTC, datetime
+from datetime import UTC, datetime, timedelta
 from decimal import Decimal
 from types import SimpleNamespace
 
@@ -17,6 +17,7 @@
     OperationsPage,
 )
 from moex_sentinel.services.broker_factory import PortfolioAdapterFactory
+from moex_sentinel.services.environment import EnvironmentMismatchError
 from moex_sentinel.services.portfolio import PortfolioAggregationService
 
 
@@ -522,3 +523,437 @@
     assert [item.broker_id for item in result.accounts] == ["b2"]
     assert [error.code for error in result.errors] == ["BROKER_ACCOUNT_NOT_FOUND"]
     assert adapters["b1"].reads == []
+
+
+def test_scoped_operations_return_the_complete_filtered_source_page_and_cursor() -> None:
+    from_at = datetime(2026, 10, 1, tzinfo=UTC)
+    to_at = datetime(2026, 10, 20, tzinfo=UTC)
+    source_rows = tuple(
+        ExternalOperation(
+            operation_id=f"op-{index:03}",
+            account_id="account-1",
+            operation_type="OPERATION_TYPE_INPUT" if index < 99 else "BUY",
+            state="EXECUTED",
+            occurred_at=to_at - timedelta(seconds=index),
+            payment=None,
+            price=None,
+            quantity=Decimal("1"),
+            commission=None,
+        )
+        for index in range(500)
+    )
+
+    class ScopedAdapter(PortfolioAdapter):
+        calls = []
+
+        async def list_accounts(self):
+            return (
+                BrokerAccount("account-1", "Main", "OPEN", "BROKER"),
+                BrokerAccount("other-account", "Other", "OPEN", "BROKER"),
+            )
+
+        async def get_operations(self, account_id, cursor, limit, instrument_id=None, *, from_at=None, to_at=None):
+            self.calls.append((account_id, cursor, limit, from_at, to_at))
+            return OperationsPage(source_rows, "next-page")
+
+    adapter = ScopedAdapter("account-1")
+    service = PortfolioAggregationService(BrokerRepository([broker("b1", "First")]), lambda _broker: adapter)
+
+    result = asyncio.run(
+        service.view_operations_scoped(
+            "b1",
+            "account-1",
+            limit=500,
+            cursor=None,
+            from_at=from_at,
+            to_at=to_at,
+        )
+    )
+
+    assert adapter.calls == [("account-1", None, 500, from_at, to_at)]
+    assert result.source_item_count == 500
+    assert result.selected_item_count == 401
+    assert len(result.items) == 401
+    assert result.next_cursor == "next-page"
+    assert result.source_exhausted is False
+    assert result.coverage_complete is False
+
+
+def test_scoped_operations_do_not_claim_interval_complete_from_a_continuation_page() -> None:
+    from_at = datetime(2026, 10, 1, tzinfo=UTC)
+    to_at = datetime(2026, 10, 20, tzinfo=UTC)
+
+    class ScopedAdapter(PortfolioAdapter):
+        async def get_operations(self, account_id, cursor, limit, instrument_id=None, *, from_at=None, to_at=None):
+            assert (account_id, cursor, limit, from_at, to_at) == (
+                "account-1",
+                "prior-page",
+                10,
+                datetime(2026, 10, 1, tzinfo=UTC),
+                datetime(2026, 10, 20, tzinfo=UTC),
+            )
+            return OperationsPage((), None)
+
+    service = PortfolioAggregationService(
+        BrokerRepository([broker("b1", "First")]), lambda _broker: ScopedAdapter("account-1")
+    )
+
+    result = asyncio.run(
+        service.view_operations_scoped(
+            "b1",
+            "account-1",
+            limit=10,
+            cursor="prior-page",
+            from_at=from_at,
+            to_at=to_at,
+        )
+    )
+
+    assert result.input_cursor == "prior-page"
+    assert result.next_cursor is None
+    assert result.source_exhausted is True
+    assert result.coverage_complete is False
+
+
+def test_scoped_operations_walked_by_client_cursor_keep_every_selected_row() -> None:
+    from_at = datetime(2026, 10, 1, tzinfo=UTC)
+    to_at = datetime(2026, 10, 20, tzinfo=UTC)
+    pages = {
+        None: OperationsPage(
+            (
+                ExternalOperation(
+                    operation_id="ignored",
+                    account_id="account-1",
+                    operation_type="OPERATION_TYPE_INPUT",
+                    state="EXECUTED",
+                    occurred_at=to_at,
+                    payment=None,
+                    price=None,
+                    quantity=Decimal("0"),
+                    commission=None,
+                ),
+                ExternalOperation(
+                    operation_id="newer",
+                    account_id="account-1",
+                    operation_type="BUY",
+                    state="EXECUTED",
+                    occurred_at=to_at - timedelta(seconds=1),
+                    payment=None,
+                    price=None,
+                    quantity=Decimal("1"),
+                    commission=None,
+                ),
+            ),
+            "page-2",
+        ),
+        "page-2": OperationsPage(
+            (
+                ExternalOperation(
+                    operation_id="older",
+                    account_id="account-1",
+                    operation_type="SELL",
+                    state="EXECUTED",
+                    occurred_at=to_at - timedelta(seconds=2),
+                    payment=None,
+                    price=None,
+                    quantity=Decimal("1"),
+                    commission=None,
+                ),
+            ),
+            None,
+        ),
+    }
+
+    class CursorAdapter(PortfolioAdapter):
+        calls = []
+
+        async def get_operations(self, account_id, cursor, limit, instrument_id=None, *, from_at=None, to_at=None):
+            self.calls.append((account_id, cursor, limit, from_at, to_at))
+            return pages[cursor]
+
+    adapter = CursorAdapter("account-1")
+    service = PortfolioAggregationService(BrokerRepository([broker("b1", "First")]), lambda _broker: adapter)
+    cursor = None
+    collected = []
+    first_coverage = None
+    while True:
+        page = asyncio.run(
+            service.view_operations_scoped("b1", "account-1", limit=2, cursor=cursor, from_at=from_at, to_at=to_at)
+        )
+        collected.extend(item.operation.operation_id for item in page.items)
+        if cursor is None:
+            first_coverage = page.coverage_complete
+        if page.source_exhausted:
+            assert page.coverage_complete is False
+            break
+        cursor = page.next_cursor
+
+    assert collected == ["newer", "older"]
+    assert first_coverage is False
+    assert [call[1] for call in adapter.calls] == [None, "page-2"]
+
+
+@pytest.mark.parametrize(
+    ("next_cursor", "rows", "expected_code"),
+    [
+        ("same", (), "BROKER_OPERATIONS_INVALID_CURSOR"),
+        ("x" * 1025, (), "BROKER_OPERATIONS_INVALID_CURSOR"),
+        ("next", ("foreign",), "BROKER_OPERATIONS_SCOPE_MISMATCH"),
+    ],
+)
+def test_scoped_operations_reject_bad_source_cursor_and_cross_account_rows(next_cursor, rows, expected_code):
+    from_at = datetime(2026, 10, 1, tzinfo=UTC)
+    to_at = datetime(2026, 10, 20, tzinfo=UTC)
+    operations = tuple(
+        ExternalOperation(
+            operation_id=f"op-{index}",
+            account_id=account_id,
+            operation_type="BUY",
+            state="EXECUTED",
+            occurred_at=to_at - timedelta(seconds=index),
+            payment=None,
+            price=None,
+            quantity=Decimal("1"),
+            commission=None,
+        )
+        for index, account_id in enumerate(rows)
+    )
+
+    class InvalidAdapter(PortfolioAdapter):
+        async def get_operations(self, account_id, cursor, limit, instrument_id=None, *, from_at=None, to_at=None):
+            return OperationsPage(operations, next_cursor)
+
+    service = PortfolioAggregationService(
+        BrokerRepository([broker("b1", "First")]), lambda _broker: InvalidAdapter("account-1")
+    )
+    input_cursor = "same" if next_cursor == "same" else None
+
+    result = asyncio.run(
+        service.view_operations_scoped("b1", "account-1", limit=10, cursor=input_cursor, from_at=from_at, to_at=to_at)
+    )
+
+    assert result.items == ()
+    assert result.errors[0].code == expected_code
+    assert result.source_exhausted is None
+    assert result.coverage_complete is False
+    assert result.errors[0].message == "Не удалось прочитать историю операций."
+    assert result.input_cursor == input_cursor
+
+
+def test_scoped_operations_require_configured_account_before_adapter_creation():
+    from_at = datetime(2026, 10, 1, tzinfo=UTC)
+    to_at = datetime(2026, 10, 20, tzinfo=UTC)
+    record = broker("b1", "First").model_copy(update={"account_id": "configured"})
+    service = PortfolioAggregationService(
+        BrokerRepository([record]),
+        lambda _broker: (_ for _ in ()).throw(AssertionError("scope mismatch must fail before SDK creation")),
+    )
+
+    result = asyncio.run(
+        service.view_operations_scoped("b1", "other", limit=10, cursor=None, from_at=from_at, to_at=to_at)
+    )
+
+    assert result.errors[0].code == "BROKER_ACCOUNT_SCOPE_MISMATCH"
+    assert result.items == ()
+    assert result.source_exhausted is None
+
+
+def test_scoped_operations_require_account_presence_in_selected_broker():
+    from_at = datetime(2026, 10, 1, tzinfo=UTC)
+    to_at = datetime(2026, 10, 20, tzinfo=UTC)
+
+    class ForeignAccountsAdapter(PortfolioAdapter):
+        async def list_accounts(self):
+            return (BrokerAccount("other", "Other", "OPEN", "BROKER"),)
+
+        async def get_operations(self, *_args, **_kwargs):
+            raise AssertionError("account absent from selected broker must not reach operations RPC")
+
+    service = PortfolioAggregationService(
+        BrokerRepository([broker("b1", "First")]), lambda _broker: ForeignAccountsAdapter("other")
+    )
+
+    result = asyncio.run(
+        service.view_operations_scoped("b1", "account-1", limit=10, cursor=None, from_at=from_at, to_at=to_at)
+    )
+
+    assert result.errors[0].code == "BROKER_ACCOUNT_NOT_FOUND"
+    assert result.items == ()
+    assert result.source_exhausted is None
+
+
+def test_scoped_operations_reject_disabled_broker_before_adapter_creation():
+    from_at = datetime(2026, 10, 1, tzinfo=UTC)
+    to_at = datetime(2026, 10, 20, tzinfo=UTC)
+    service = PortfolioAggregationService(
+        BrokerRepository([broker("b1", "First", enabled=False)]),
+        lambda _broker: (_ for _ in ()).throw(AssertionError("disabled scope must not create an adapter")),
+    )
+
+    result = asyncio.run(
+        service.view_operations_scoped("b1", "account-1", limit=10, cursor=None, from_at=from_at, to_at=to_at)
+    )
+
+    assert result.errors[0].code == "BROKER_DISABLED"
+    assert result.items == ()
+    assert result.source_exhausted is None
+    assert result.coverage_complete is False
+
+
+def test_scoped_operations_mark_naive_source_timestamp_incomplete_and_sanitize_error():
+    from_at = datetime(2026, 10, 1, tzinfo=UTC)
+    to_at = datetime(2026, 10, 20, tzinfo=UTC)
+    operation = ExternalOperation(
+        operation_id="naive-time",
+        account_id="account-1",
+        operation_type="BUY",
+        state="EXECUTED",
+        occurred_at=datetime(2026, 10, 19, 12),
+        payment=None,
+        price=None,
+        quantity=Decimal("1"),
+        commission=None,
+    )
+
+    class NaiveTimestampAdapter(PortfolioAdapter):
+        async def get_operations(self, account_id, cursor, limit, instrument_id=None, *, from_at=None, to_at=None):
+            return OperationsPage((operation,), None)
+
+    service = PortfolioAggregationService(
+        BrokerRepository([broker("b1", "First")]), lambda _broker: NaiveTimestampAdapter("account-1")
+    )
+
+    result = asyncio.run(
+        service.view_operations_scoped("b1", "account-1", limit=10, cursor=None, from_at=from_at, to_at=to_at)
+    )
+
+    assert result.errors[0].code == "BROKER_OPERATIONS_INVALID_TIMESTAMP"
+    assert result.errors[0].message == "Не удалось прочитать историю операций."
+    assert result.items == ()
+    assert result.source_exhausted is None
+    assert result.coverage_complete is False
+
+
+def test_scoped_operations_filter_millisecond_boundaries_deduplicate_and_sort_stably():
+    from_at = datetime(2026, 10, 1, 0, 0, 0, 1000, tzinfo=UTC)
+    to_at = datetime(2026, 10, 1, 0, 0, 0, 4000, tzinfo=UTC)
+
+    def operation(operation_id, occurred_at):
+        return ExternalOperation(
+            operation_id=operation_id,
+            account_id="account-1",
+            operation_type="BUY",
+            state="EXECUTED",
+            occurred_at=occurred_at,
+            payment=None,
+            price=None,
+            quantity=Decimal("1"),
+            commission=None,
+        )
+
+    at_lower = operation("lower", from_at + timedelta(microseconds=999))
+    at_upper = operation("upper", to_at + timedelta(microseconds=999))
+    after_upper = operation("after-upper", to_at + timedelta(milliseconds=1))
+    same_time_z = operation("z", to_at - timedelta(microseconds=1000))
+    same_time_a = operation("a", to_at - timedelta(microseconds=1000))
+    page = OperationsPage((at_lower, same_time_z, at_upper, after_upper, same_time_a, same_time_a), None)
+
+    class BoundaryAdapter(PortfolioAdapter):
+        async def get_operations(self, account_id, cursor, limit, instrument_id=None, *, from_at=None, to_at=None):
+            return page
+
+    service = PortfolioAggregationService(
+        BrokerRepository([broker("b1", "First")]), lambda _broker: BoundaryAdapter("account-1")
+    )
+
+    result = asyncio.run(
+        service.view_operations_scoped("b1", "account-1", limit=10, cursor=None, from_at=from_at, to_at=to_at)
+    )
+
+    assert [item.operation.operation_id for item in result.items] == ["upper", "a", "z"]
+    assert result.source_item_count == 6
+    assert result.selected_item_count == 3
+    assert result.source_exhausted is True
+    assert result.coverage_complete is True
+
+
+@pytest.mark.parametrize("invalid_page", ["oversized", "conflicting_duplicate"])
+def test_scoped_operations_reject_untrustworthy_source_pages(invalid_page):
+    from_at = datetime(2026, 10, 1, tzinfo=UTC)
+    to_at = datetime(2026, 10, 20, tzinfo=UTC)
+    operation = ExternalOperation(
+        operation_id="same",
+        account_id="account-1",
+        operation_type="BUY",
+        state="EXECUTED",
+        occurred_at=to_at - timedelta(seconds=1),
+        payment=None,
+        price=None,
+        quantity=Decimal("1"),
+        commission=None,
+    )
+    if invalid_page == "oversized":
+        items = (operation, operation.model_copy(update={"operation_id": "other"}))
+        limit = 1
+        expected_code = "BROKER_OPERATIONS_INVALID_PAGE"
+    else:
+        items = (operation, operation.model_copy(update={"quantity": Decimal("2")}))
+        limit = 2
+        expected_code = "BROKER_OPERATIONS_CONFLICTING_DUPLICATE"
+
+    class InvalidAdapter(PortfolioAdapter):
+        async def get_operations(
+            self, account_id, cursor, requested_limit, instrument_id=None, *, from_at=None, to_at=None
+        ):
+            return OperationsPage(items, None)
+
+    service = PortfolioAggregationService(
+        BrokerRepository([broker("b1", "First")]), lambda _broker: InvalidAdapter("account-1")
+    )
+
+    result = asyncio.run(
+        service.view_operations_scoped("b1", "account-1", limit=limit, cursor=None, from_at=from_at, to_at=to_at)
+    )
+
+    assert result.errors[0].code == expected_code
+    assert result.items == ()
+    assert result.source_exhausted is None
+    assert result.coverage_complete is False
+
+
+def test_scoped_operations_sanitize_adapter_error_and_do_not_read_other_scopes():
+    from_at = datetime(2026, 10, 1, tzinfo=UTC)
+    to_at = datetime(2026, 10, 20, tzinfo=UTC)
+
+    class FailedAdapter(PortfolioAdapter):
+        async def list_accounts(self):
+            raise TInvestAdapterError("BROKER_FAILURE", "secret-token leaked", retryable=False)
+
+    service = PortfolioAggregationService(
+        BrokerRepository([broker("b1", "First"), broker("b2", "Second")]),
+        lambda selected: FailedAdapter("account-1"),
+    )
+
+    result = asyncio.run(
+        service.view_operations_scoped("b1", "account-1", limit=10, cursor=None, from_at=from_at, to_at=to_at)
+    )
+
+    assert result.errors[0].code == "BROKER_OPERATIONS_UNAVAILABLE"
+    assert "secret-token" not in result.errors[0].message
+    assert result.source_exhausted is None
+    assert result.coverage_complete is False
+
+
+def test_scoped_operations_reject_wrong_environment_before_adapter_creation():
+    from_at = datetime(2026, 10, 1, tzinfo=UTC)
+    to_at = datetime(2026, 10, 20, tzinfo=UTC)
+    service = PortfolioAggregationService(
+        BrokerRepository([broker("b1", "First")]),
+        lambda _broker: (_ for _ in ()).throw(AssertionError("wrong environment must fail before SDK")),
+        environment=ProdEnvironment(),
+    )
+
+    with pytest.raises(EnvironmentMismatchError, match="inactive environment"):
+        asyncio.run(
+            service.view_operations_scoped("b1", "account-1", limit=10, cursor=None, from_at=from_at, to_at=to_at)
+        )
```

## tests/api/test_portfolio_operations.py
SHA256 8bbdfda03f08ceef4134066157eb0bc05b6e992e2fe6882daba4fc3b9e281bd8
```diff
--- baseline/tests/api/test_portfolio_operations.py
+++ current/tests/api/test_portfolio_operations.py
@@ -0,0 +1,197 @@
+from datetime import UTC, datetime, timedelta
+from decimal import Decimal
+from types import SimpleNamespace
+
+import pytest
+from fastapi.testclient import TestClient
+
+from moex_sentinel.api.app import create_app
+from moex_sentinel.api.auth import hash_password
+from moex_sentinel.domain.portfolio import (
+    BrokerOperation,
+    ExternalOperation,
+)
+from moex_sentinel.usecases.portfolio import ViewRecentOperationsUsecase
+
+
+class ScopedOperationsUsecase:
+    def __init__(self):
+        self.calls = []
+
+    async def view_operations_scoped(self, broker_id, account_id, *, limit, cursor, from_at, to_at):
+        self.calls.append((broker_id, account_id, limit, cursor, from_at, to_at))
+        operation = ExternalOperation(
+            operation_id="op-1",
+            account_id=account_id,
+            operation_type="BUY",
+            state="EXECUTED",
+            occurred_at=datetime(2026, 10, 20, 10, tzinfo=UTC),
+            payment=None,
+            price=None,
+            quantity=Decimal("1"),
+            commission=None,
+            instrument_id="uid-1",
+        )
+        return SimpleNamespace(
+            items=(BrokerOperation(broker_id, "Broker", operation),),
+            errors=(),
+            input_cursor=cursor,
+            next_cursor="next-page",
+            source_item_count=500,
+            selected_item_count=1,
+            source_exhausted=False,
+            coverage_complete=False,
+        )
+
+
+def _app(monkeypatch, tmp_path, usecase):
+    usecases = SimpleNamespace(
+        daily_admission_preparation=SimpleNamespace(),
+        view_recent_operations=ViewRecentOperationsUsecase(usecase),
+    )
+    monkeypatch.setattr("moex_sentinel.api.app.build_application_usecases", lambda _factory, *, settings: usecases)
+    return create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'operations.db'}")
+
+
+def _params(**overrides):
+    now = datetime.now(UTC)
+    params = {
+        "broker_id": "broker-1",
+        "account_id": "account-1",
+        "from_at": (now - timedelta(days=2)).isoformat(),
+        "to_at": (now - timedelta(days=1)).isoformat(),
+        "limit": 500,
+    }
+    return params | overrides
+
+
+def test_scoped_operations_http_contract_and_cursor_continuation(monkeypatch, tmp_path):
+    service = ScopedOperationsUsecase()
+    with TestClient(_app(monkeypatch, tmp_path, service)) as client:
+        first_params = _params(
+            from_at="2026-10-01T03:00:00.001999+03:00",
+            to_at="2026-10-20T12:00:00.009999Z",
+        )
+        first = client.get("/api/operations/scoped", params=first_params)
+        second = client.get("/api/operations/scoped", params=first_params | {"cursor": first.json()["next_cursor"]})
+
+    assert first.status_code == second.status_code == 200
+    body = first.json()
+    assert body == {
+        "items": [
+            {
+                "broker_id": "broker-1",
+                "broker_name": "Broker",
+                "operation_id": "op-1",
+                "account_id": "account-1",
+                "operation_type": "BUY",
+                "state": "EXECUTED",
+                "occurred_at": "2026-10-20T10:00:00Z",
+                "payment": None,
+                "price": None,
+                "quantity": "1",
+                "commission": None,
+                "instrument_id": "uid-1",
+                "ticker": None,
+            }
+        ],
+        "errors": [],
+        "input_cursor": None,
+        "next_cursor": "next-page",
+        "source_item_count": 500,
+        "selected_item_count": 1,
+        "source_exhausted": False,
+        "coverage_complete": False,
+    }
+    assert service.calls[0][0:4] == ("broker-1", "account-1", 500, None)
+    assert service.calls[0][4] == datetime(2026, 10, 1, 0, 0, 0, 1000, tzinfo=UTC)
+    assert service.calls[0][5] == datetime(2026, 10, 20, 12, 0, 0, 9000, tzinfo=UTC)
+    assert service.calls[1][3] == "next-page"
+    assert service.calls[1][4:] == service.calls[0][4:]
+
+
+@pytest.mark.parametrize(
+    "overrides",
+    [
+        {"from_at": "2026-10-01T00:00:00"},
+        {"from_at": "2026-10-21T00:00:00Z"},
+        {"from_at": "2026-10-20T00:00:00Z", "to_at": "2026-10-19T00:00:00Z"},
+        {"from_at": ""},
+        {"to_at": ""},
+        {"limit": 0},
+        {"limit": 501},
+        {"cursor": ""},
+        {"cursor": "x" * 1025},
+    ],
+)
+def test_scoped_operations_reject_invalid_query_bounds(monkeypatch, tmp_path, overrides):
+    service = ScopedOperationsUsecase()
+    with TestClient(_app(monkeypatch, tmp_path, service)) as client:
+        response = client.get("/api/operations/scoped", params=_params(**overrides))
+
+    assert response.status_code == 422
+    assert service.calls == []
+
+
+@pytest.mark.parametrize("missing_parameter", ["from_at", "to_at"])
+def test_scoped_operations_require_both_date_bounds(monkeypatch, tmp_path, missing_parameter):
+    service = ScopedOperationsUsecase()
+    params = _params()
+    params.pop(missing_parameter)
+
+    with TestClient(_app(monkeypatch, tmp_path, service)) as client:
+        response = client.get("/api/operations/scoped", params=params)
+
+    assert response.status_code == 422
+    assert service.calls == []
+
+
+def test_scoped_operations_reject_future_to_with_valid_ordered_bounds(monkeypatch, tmp_path):
+    service = ScopedOperationsUsecase()
+    now = datetime.now(UTC)
+    params = _params(
+        from_at=(now - timedelta(hours=1)).isoformat(),
+        to_at=(now + timedelta(hours=1)).isoformat(),
+    )
+
+    with TestClient(_app(monkeypatch, tmp_path, service)) as client:
+        response = client.get("/api/operations/scoped", params=params)
+
+    assert datetime.fromisoformat(params["from_at"]) < datetime.fromisoformat(params["to_at"])
+    assert response.status_code == 422
+    assert service.calls == []
+
+
+def test_legacy_operations_endpoint_still_uses_original_limit_contract(monkeypatch, tmp_path):
+    class LegacyUsecase:
+        async def execute(self, limit):
+            assert limit == 20
+            return SimpleNamespace(items=(), errors=())
+
+    usecases = SimpleNamespace(daily_admission_preparation=SimpleNamespace(), view_recent_operations=LegacyUsecase())
+    monkeypatch.setattr("moex_sentinel.api.app.build_application_usecases", lambda _factory, *, settings: usecases)
+
+    with TestClient(create_app(test_auth_bypass=True, database_url=f"sqlite:///{tmp_path / 'legacy.db'}")) as client:
+        response = client.get("/api/operations?limit=20")
+
+    assert response.status_code == 200
+    assert response.json() == {"items": [], "errors": []}
+
+
+def test_scoped_operations_remain_behind_operator_auth(monkeypatch, tmp_path):
+    monkeypatch.setenv("AUTH_USERNAME", "operator")
+    monkeypatch.setenv("AUTH_PASSWORD_HASH", hash_password("a long test password"))
+    monkeypatch.setenv("AUTH_INSECURE_LOOPBACK", "true")
+    service = ScopedOperationsUsecase()
+    usecases = SimpleNamespace(
+        daily_admission_preparation=SimpleNamespace(),
+        view_recent_operations=ViewRecentOperationsUsecase(service),
+    )
+    monkeypatch.setattr("moex_sentinel.api.app.build_application_usecases", lambda _factory, *, settings: usecases)
+
+    with TestClient(create_app(database_url=f"sqlite:///{tmp_path / 'private-operations.db'}")) as client:
+        response = client.get("/api/operations/scoped", params=_params())
+
+    assert response.status_code == 401
+    assert response.headers["Cache-Control"] == "no-store"
+    assert service.calls == []
```
