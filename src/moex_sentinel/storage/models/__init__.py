"""Baseline SQLAlchemy model registry."""

from moex_sentinel.storage.models.automation_facts import PositionCycleModel, TradingAutomationModel
from moex_sentinel.storage.models.base import Base
from moex_sentinel.storage.models.execution_commissions import (
    BrokerFeeScanStateModel,
    BrokerOrderFeeObservationModel,
)
from moex_sentinel.storage.models.order_facts import (
    BrokerOrderEventModel,
    BrokerOrderModel,
    TradeDecisionModel,
    TradeExecutionModel,
)
from moex_sentinel.storage.models.position_facts import ExecutionLotAllocationModel, PositionLotModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel, InstrumentSyncStateModel
from moex_sentinel.storage.models.trading_admission import (
    TradingAccountAdmissionModel,
    TradingAdmissionBaselineModel,
    TradingAdmissionEventModel,
    TradingReconciliationModel,
    TradingSubmitReservationModel,
    WorkerRuntimeInstanceModel,
)
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
    "BrokerFeeScanStateModel",
    "BrokerInstrumentModel",
    "BrokerOrderEventModel",
    "BrokerOrderFeeObservationModel",
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
    "TradingAccountAdmissionModel",
    "TradingAdmissionBaselineModel",
    "TradingAdmissionEventModel",
    "TradingAutomationModel",
    "TradingReconciliationModel",
    "TradingSubmitReservationModel",
    "UserBrokerModel",
    "WorkerRuntimeInstanceModel",
]
