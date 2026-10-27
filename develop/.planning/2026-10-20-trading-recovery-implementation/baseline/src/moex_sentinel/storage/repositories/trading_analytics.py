"""Session-bound fee-profile and analytical snapshot repository."""

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from moex_sentinel.domain.trading_facts import (
    BrokerAccountFeeProfileDraft,
    PositionValuationSnapshotDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.models.automation_facts import PositionCycleModel, TradingAutomationModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.models.trading_analytics import (
    BrokerAccountFeeProfileModel,
    PositionValuationSnapshotModel,
)
from moex_sentinel.storage.repositories.trading_facts_support import (
    append_idempotent,
    flush_or_translate,
    require_scoped_reference,
)


def _fee_value(model: BrokerAccountFeeProfileModel) -> BrokerAccountFeeProfileDraft:
    return BrokerAccountFeeProfileDraft(
        id=model.id,
        user_broker_id=model.user_broker_id,
        instrument_type=model.instrument_type,
        currency=model.currency,
        buy_rate=model.buy_rate,
        sell_rate=model.sell_rate,
        service_rate=model.service_rate,
        deal_rate=model.deal_rate,
        source=model.source,
        calculated_at=model.calculated_at,
        valid_until=model.valid_until,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _valuation_value(model: PositionValuationSnapshotModel) -> PositionValuationSnapshotDraft:
    return PositionValuationSnapshotDraft(
        id=model.id,
        user_broker_id=model.user_broker_id,
        automation_id=model.automation_id,
        position_cycle_id=model.position_cycle_id,
        instrument_id=model.instrument_id,
        quantity_lots=model.quantity_lots,
        average_price=model.average_price,
        current_price=model.current_price,
        invested_amount=model.invested_amount,
        market_value=model.market_value,
        realized_pnl=model.realized_pnl,
        unrealized_pnl=model.unrealized_pnl,
        net_pnl=model.net_pnl,
        actual_commissions=model.actual_commissions,
        source=model.source,
        captured_at=model.captured_at,
        created_at=model.created_at,
    )


def _require_scope(user_broker_id: str, value_scope: str, entity_type: str) -> None:
    if user_broker_id != value_scope:
        raise TradingFactPersistenceError(TradingFactErrorCode.CROSS_SCOPE, entity_type=entity_type)


class TradingAnalyticsRepository:
    """Persist fee profiles and immutable position valuations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_fee_profile(
        self,
        user_broker_id: str,
        value: BrokerAccountFeeProfileDraft,
    ) -> BrokerAccountFeeProfileDraft:
        _require_scope(user_broker_id, value.user_broker_id, "broker_account_fee_profile")
        existing = self._session.scalar(
            select(BrokerAccountFeeProfileModel).where(
                BrokerAccountFeeProfileModel.user_broker_id == user_broker_id,
                BrokerAccountFeeProfileModel.instrument_type == value.instrument_type,
                BrokerAccountFeeProfileModel.currency == value.currency,
            )
        )
        if existing is None:
            existing = BrokerAccountFeeProfileModel(**value.model_dump(mode="python"))
            self._session.add(existing)
        else:
            existing.buy_rate = value.buy_rate
            existing.sell_rate = value.sell_rate
            existing.service_rate = value.service_rate
            existing.deal_rate = value.deal_rate
            existing.source = value.source
            existing.calculated_at = value.calculated_at
            existing.valid_until = value.valid_until
            existing.updated_at = value.updated_at
        flush_or_translate(self._session, entity_type="broker_account_fee_profile")
        return _fee_value(existing)

    def append_position_valuation(
        self,
        user_broker_id: str,
        value: PositionValuationSnapshotDraft,
    ) -> PositionValuationSnapshotDraft:
        _require_scope(user_broker_id, value.user_broker_id, "position_valuation_snapshot")
        for scope_column, id_column, reference_id in (
            (TradingAutomationModel.user_broker_id, TradingAutomationModel.id, value.automation_id),
            (PositionCycleModel.user_broker_id, PositionCycleModel.id, value.position_cycle_id),
            (BrokerInstrumentModel.user_broker_id, BrokerInstrumentModel.id, value.instrument_id),
        ):
            require_scoped_reference(
                self._session,
                scope_column=scope_column,
                id_column=id_column,
                user_broker_id=user_broker_id,
                reference_id=reference_id,
                entity_type="position_valuation_snapshot",
            )
        matching_cycle = self._session.scalar(
            select(PositionCycleModel.id)
            .join(
                TradingAutomationModel,
                and_(
                    TradingAutomationModel.user_broker_id == PositionCycleModel.user_broker_id,
                    TradingAutomationModel.id == PositionCycleModel.automation_id,
                    TradingAutomationModel.instrument_id == PositionCycleModel.instrument_id,
                ),
            )
            .where(
                PositionCycleModel.user_broker_id == user_broker_id,
                PositionCycleModel.id == value.position_cycle_id,
                PositionCycleModel.automation_id == value.automation_id,
                PositionCycleModel.instrument_id == value.instrument_id,
            )
        )
        if matching_cycle is None:
            raise TradingFactPersistenceError(
                TradingFactErrorCode.CROSS_SCOPE,
                entity_type="position_valuation_snapshot",
            )
        model = PositionValuationSnapshotModel(**value.model_dump(mode="python"))
        return append_idempotent(
            self._session,
            candidate=model,
            identity=and_(
                PositionValuationSnapshotModel.user_broker_id == user_broker_id,
                PositionValuationSnapshotModel.id == value.id,
            ),
            to_value=_valuation_value,
            conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
            entity_type="position_valuation_snapshot",
        )
