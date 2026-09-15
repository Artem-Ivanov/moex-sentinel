"""Atomic transaction owner for all Core trading fact repositories."""

from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from moex_sentinel.storage.repositories.automation_facts import AutomationFactsRepository
from moex_sentinel.storage.repositories.order_facts import OrderFactsRepository
from moex_sentinel.storage.repositories.position_ledger import PositionLedgerRepository
from moex_sentinel.storage.repositories.trading_analytics import TradingAnalyticsRepository
from moex_sentinel.storage.repositories.trading_audit import TradingAuditRepository


class TradingFactsUnitOfWork:
    """Commit one automation fact group or roll its aggregate changes back."""

    automations: AutomationFactsRepository
    orders: OrderFactsRepository
    positions: PositionLedgerRepository
    audit: TradingAuditRepository
    analytics: TradingAnalyticsRepository

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._session: Session | None = None

    def __enter__(self) -> "TradingFactsUnitOfWork":
        session = self._factory()
        self._session = session
        self.automations = AutomationFactsRepository(session)
        self.orders = OrderFactsRepository(session)
        self.positions = PositionLedgerRepository(session)
        self.audit = TradingAuditRepository(session)
        self.analytics = TradingAnalyticsRepository(session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._session
        if session is None:
            return
        try:
            if exc_type is None:
                try:
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
            else:
                session.rollback()
        finally:
            session.close()
            self._session = None
