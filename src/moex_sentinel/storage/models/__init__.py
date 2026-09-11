"""Baseline SQLAlchemy model registry."""

from moex_sentinel.storage.models.automation_facts import PositionCycleModel, TradingAutomationModel
from moex_sentinel.storage.models.base import Base
from moex_sentinel.storage.models.order_facts import (
    BrokerOrderEventModel,
    BrokerOrderModel,
    TradeDecisionModel,
    TradeExecutionModel,
)
from moex_sentinel.storage.models.position_facts import ExecutionLotAllocationModel, PositionLotModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel, InstrumentSyncStateModel
from moex_sentinel.storage.models.trading_analytics import (
    BrokerAccountFeeProfileModel,
    PortfolioSnapshotModel,
    PortfolioSnapshotRunModel,
    PositionValuationSnapshotModel,
)
from moex_sentinel.storage.models.trading_observability import AutomationEventModel, TradeAuditEventModel
from moex_sentinel.storage.models.user_brokers import UserBrokerModel

__all__ = [
    "AutomationEventModel",
    "Base",
    "BrokerAccountFeeProfileModel",
    "BrokerInstrumentModel",
    "BrokerOrderEventModel",
    "BrokerOrderModel",
    "ExecutionLotAllocationModel",
    "InstrumentSyncStateModel",
    "PortfolioSnapshotModel",
    "PortfolioSnapshotRunModel",
    "PositionCycleModel",
    "PositionLotModel",
    "PositionValuationSnapshotModel",
    "TradeAuditEventModel",
    "TradeDecisionModel",
    "TradeExecutionModel",
    "TradingAutomationModel",
    "UserBrokerModel",
]
