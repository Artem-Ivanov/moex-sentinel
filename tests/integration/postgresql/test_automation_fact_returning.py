"""Run the acceptance contract against a migrated disposable PostgreSQL schema."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.domain.trading_facts import (
    TradingAutomationDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.database import create_database_engine
from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
from moex_sentinel.storage.repositories.automation_facts import AutomationFactsRepository
from sentinel_contracts.trading import AutomationState
from tests.storage.test_automation_fact_returning import (
    TestAutomationFactReturning as _AcceptanceContract,
)
from tests.storage.test_automation_fact_returning import (
    accept_fact,
)
from tests.storage.test_automation_fact_returning import (
    automation_factory as _shared_automation_factory,
)

automation_factory = _shared_automation_factory


@pytest.fixture
def automation_engine(isolated_postgresql_database_url: URL) -> Iterator[Engine]:
    engine = create_database_engine(isolated_postgresql_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.postgresql
class TestPostgresqlAutomationFactReturning(_AcceptanceContract):
    """Identical behavior and SQL-budget assertions on the PostgreSQL dialect."""

    @pytest.mark.parametrize("kind", ["supporting", "state"])
    def test_concurrent_accepts_have_one_winner_with_preloaded_aggregates(
        self, automation_factory: sessionmaker[Session], kind: str
    ) -> None:
        readers = Barrier(2)

        def attempt() -> TradingAutomationDraft | TradingFactErrorCode:
            try:
                with automation_factory.begin() as session:
                    loaded = session.get(TradingAutomationModel, "automation-1")
                    assert loaded is not None
                    assert (loaded.revision, loaded.last_sequence_number) == (1, 0)
                    readers.wait(timeout=10)
                    return accept_fact(AutomationFactsRepository(session), kind)
            except TradingFactPersistenceError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]

        winners = [result for result in results if isinstance(result, TradingAutomationDraft)]
        conflicts = [result for result in results if isinstance(result, TradingFactErrorCode)]
        assert len(winners) == 1
        assert conflicts == [
            TradingFactErrorCode.SEQUENCE_CONFLICT if kind == "supporting" else TradingFactErrorCode.REVISION_CONFLICT
        ]
        with automation_factory() as session:
            persisted = AutomationFactsRepository(session).get("scope-1", "automation-1")
            assert persisted == winners[0]
            assert persisted.last_sequence_number == 1
            assert persisted.revision == (1 if kind == "supporting" else 2)
            assert persisted.state is (AutomationState.IN_WORK if kind == "supporting" else AutomationState.HOLD)
