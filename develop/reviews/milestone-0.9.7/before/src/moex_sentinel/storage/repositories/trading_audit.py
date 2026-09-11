"""Session-bound accepted-envelope and audit repository."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from moex_sentinel.domain.trading_facts import (
    AutomationEnvelopeDraft,
    TradeAuditEventDraft,
    TradeAuditLevel,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from moex_sentinel.storage.models.automation_facts import TradingAutomationModel
from moex_sentinel.storage.models.order_facts import BrokerOrderModel, TradeDecisionModel, TradeExecutionModel
from moex_sentinel.storage.models.reference_data import BrokerInstrumentModel
from moex_sentinel.storage.models.trading_observability import (
    AutomationEventModel,
    TradeAuditEventModel,
)
from moex_sentinel.storage.repositories.trading_facts_support import append_idempotent, require_scoped_reference


def _audit_value(model: TradeAuditEventModel) -> TradeAuditEventDraft:
    return TradeAuditEventDraft(
        event_id=model.event_id,
        process_id=model.process_id,
        parent_process_id=model.parent_process_id,
        user_broker_id=model.user_broker_id,
        automation_id=model.automation_id,
        decision_id=model.decision_id,
        broker_order_id=model.broker_order_id,
        execution_id=model.execution_id,
        instrument_id=model.instrument_id,
        level=TradeAuditLevel(model.level),
        stage=model.stage,
        safe_message=model.safe_message,
        data=dict(model.data),
        occurred_at=model.occurred_at,
        created_at=model.created_at,
        critical=model.critical,
    )


def _envelope_value(model: AutomationEventModel) -> AutomationEnvelopeDraft:
    return AutomationEnvelopeDraft(
        event_id=model.event_id,
        automation_id=model.automation_id,
        user_broker_id=model.user_broker_id,
        sequence_number=model.sequence_number,
        expected_revision=model.expected_revision,
        fact_kind=model.fact_kind,
        safe_message=model.safe_message,
        payload=dict(model.payload),
        occurred_at=model.occurred_at,
        received_at=model.received_at,
    )


def _require_scope(user_broker_id: str, value_scope: str, entity_type: str) -> None:
    if user_broker_id != value_scope:
        raise TradingFactPersistenceError(TradingFactErrorCode.CROSS_SCOPE, entity_type=entity_type)


class TradingAuditRepository:
    """Persist accepted fact envelopes and diagnostic events."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append_audit(self, user_broker_id: str, value: TradeAuditEventDraft) -> TradeAuditEventDraft:
        _require_scope(user_broker_id, value.user_broker_id, "trade_audit_event")
        for scope_column, id_column, reference_id in (
            (TradingAutomationModel.user_broker_id, TradingAutomationModel.id, value.automation_id),
            (TradeDecisionModel.user_broker_id, TradeDecisionModel.id, value.decision_id),
            (BrokerOrderModel.user_broker_id, BrokerOrderModel.id, value.broker_order_id),
            (TradeExecutionModel.user_broker_id, TradeExecutionModel.id, value.execution_id),
            (BrokerInstrumentModel.user_broker_id, BrokerInstrumentModel.id, value.instrument_id),
        ):
            require_scoped_reference(
                self._session,
                scope_column=scope_column,
                id_column=id_column,
                user_broker_id=user_broker_id,
                reference_id=reference_id,
                entity_type="trade_audit_event",
            )
        if (
            self._session.scalar(
                select(TradingAutomationModel.id).where(
                    TradingAutomationModel.user_broker_id == user_broker_id,
                    TradingAutomationModel.id == value.automation_id,
                    TradingAutomationModel.instrument_id == value.instrument_id,
                )
            )
            is None
        ):
            raise TradingFactPersistenceError(
                TradingFactErrorCode.INVALID_STATE,
                entity_type="trade_audit_event",
            )
        for reference_model, reference_id in (
            (TradeDecisionModel, value.decision_id),
            (BrokerOrderModel, value.broker_order_id),
            (TradeExecutionModel, value.execution_id),
        ):
            if reference_id is None:
                continue
            if (
                self._session.scalar(
                    select(reference_model.id).where(
                        reference_model.user_broker_id == user_broker_id,
                        reference_model.id == reference_id,
                        reference_model.automation_id == value.automation_id,
                        reference_model.instrument_id == value.instrument_id,
                    )
                )
                is None
            ):
                raise TradingFactPersistenceError(
                    TradingFactErrorCode.INVALID_STATE,
                    entity_type="trade_audit_event",
                )
        model = TradeAuditEventModel(**value.model_dump(mode="python"))
        return append_idempotent(
            self._session,
            candidate=model,
            identity=TradeAuditEventModel.event_id == value.event_id,
            to_value=_audit_value,
            conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
            entity_type="trade_audit_event",
        )

    def append_envelope(self, user_broker_id: str, value: AutomationEnvelopeDraft) -> AutomationEnvelopeDraft:
        _require_scope(user_broker_id, value.user_broker_id, "automation_event")
        require_scoped_reference(
            self._session,
            scope_column=TradingAutomationModel.user_broker_id,
            id_column=TradingAutomationModel.id,
            user_broker_id=user_broker_id,
            reference_id=value.automation_id,
            entity_type="automation_event",
        )
        model = AutomationEventModel(**value.model_dump(mode="python"))
        return append_idempotent(
            self._session,
            candidate=model,
            identity=AutomationEventModel.event_id == value.event_id,
            to_value=_envelope_value,
            conflict_code=TradingFactErrorCode.FACT_ID_CONFLICT,
            entity_type="automation_event",
        )

    def get_envelope_by_event_id(
        self,
        user_broker_id: str,
        event_id: str,
    ) -> AutomationEnvelopeDraft | None:
        model = self._session.scalar(
            select(AutomationEventModel).where(
                AutomationEventModel.user_broker_id == user_broker_id,
                AutomationEventModel.event_id == event_id,
            )
        )
        return _envelope_value(model) if model is not None else None

    def get_envelope_by_sequence(
        self,
        user_broker_id: str,
        automation_id: str,
        sequence_number: int,
    ) -> AutomationEnvelopeDraft | None:
        model = self._session.scalar(
            select(AutomationEventModel).where(
                AutomationEventModel.user_broker_id == user_broker_id,
                AutomationEventModel.automation_id == automation_id,
                AutomationEventModel.sequence_number == sequence_number,
            )
        )
        return _envelope_value(model) if model is not None else None

    def list_audit(self, user_broker_id: str, automation_id: str) -> tuple[TradeAuditEventDraft, ...]:
        statement = (
            select(TradeAuditEventModel)
            .where(
                TradeAuditEventModel.user_broker_id == user_broker_id,
                TradeAuditEventModel.automation_id == automation_id,
            )
            .order_by(TradeAuditEventModel.occurred_at, TradeAuditEventModel.event_id)
        )
        return tuple(_audit_value(model) for model in self._session.scalars(statement))
