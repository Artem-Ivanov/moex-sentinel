"""Session-bound writer for the unified typed Worker fact outbox."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TypedDict
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel_contracts.time import floor_utc_millisecond, utc_now_ms
from sentinel_contracts.trading_facts import (
    AutomationStateChangedEnvelope,
    AutomationStateChangedPayload,
    BrokerOrderRecordedEnvelope,
    BrokerOrderRecordedPayload,
    BrokerOrderStateChangedEnvelope,
    BrokerOrderStateChangedPayload,
    ExecutionLotAllocatedEnvelope,
    ExecutionLotAllocatedPayload,
    FactEnvelope,
    FactKind,
    FactPayload,
    PositionCycleUpdatedEnvelope,
    PositionCycleUpdatedPayload,
    PositionLotOpenedEnvelope,
    PositionLotOpenedPayload,
    TradeAuditRecordedEnvelope,
    TradeAuditRecordedPayload,
    TradeDecisionRecordedEnvelope,
    TradeDecisionRecordedPayload,
    TradeExecutionRecordedEnvelope,
    TradeExecutionRecordedPayload,
)
from trading_automaton.storage.models import CachedAutomationModel, FactOutboxModel


class _EnvelopeValues(TypedDict):
    event_id: UUID
    user_broker_id: UUID
    automation_id: UUID
    sequence_number: int
    expected_revision: int
    safe_message: str
    occurred_at: datetime


class FactOutboxWriter:
    def __init__(
        self,
        id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] = utc_now_ms,
    ) -> None:
        self._id_factory = id_factory
        self._clock = clock

    def append(
        self,
        session: Session,
        cached: CachedAutomationModel,
        *,
        payload: FactPayload,
        safe_message: str,
        occurred_at: datetime,
        changes_revision: bool = False,
    ) -> FactEnvelope:
        if cached.user_broker_id is None or cached.fact_instrument_id is None:
            raise ValueError("Cached fact scope is incomplete.")
        # Flush domain changes first. With SQLite this also establishes the
        # current write transaction before the sequence is derived, so a
        # concurrent finalization cannot commit between a stale cache read and
        # the outbox insert.
        session.flush()
        session.refresh(cached, attribute_names=["last_sequence_number", "revision"])
        previous_sequence, previous_at = session.execute(
            select(
                func.max(FactOutboxModel.sequence_number),
                func.max(FactOutboxModel.occurred_at),
            ).where(FactOutboxModel.automation_id == cached.automation_id)
        ).one()
        sequence_number = max(cached.last_sequence_number, previous_sequence or 0) + 1
        normalized_at = floor_utc_millisecond(occurred_at)
        if previous_at is not None:
            normalized_at = max(normalized_at, previous_at + timedelta(milliseconds=1))
        envelope = self._envelope(
            event_id=self._id_factory(),
            user_broker_id=UUID(cached.user_broker_id),
            automation_id=UUID(cached.automation_id),
            sequence_number=sequence_number,
            expected_revision=cached.revision,
            safe_message=safe_message,
            occurred_at=normalized_at,
            payload=payload,
        )
        enqueued_at = self._clock()
        session.add(
            FactOutboxModel(
                event_id=str(envelope.event_id),
                user_broker_id=str(envelope.user_broker_id),
                automation_id=str(envelope.automation_id),
                sequence_number=envelope.sequence_number,
                expected_revision=envelope.expected_revision,
                fact_kind=envelope.fact_kind.value,
                payload=envelope.payload.model_dump(mode="json"),
                safe_message=envelope.safe_message,
                occurred_at=envelope.occurred_at,
                created_at=enqueued_at,
                updated_at=enqueued_at,
            )
        )
        cached.last_sequence_number = sequence_number
        if changes_revision:
            cached.revision += 1
        return envelope

    @staticmethod
    def _envelope(
        *,
        event_id: UUID,
        user_broker_id: UUID,
        automation_id: UUID,
        sequence_number: int,
        expected_revision: int,
        safe_message: str,
        occurred_at: datetime,
        payload: FactPayload,
    ) -> FactEnvelope:
        common: _EnvelopeValues = {
            "event_id": event_id,
            "user_broker_id": user_broker_id,
            "automation_id": automation_id,
            "sequence_number": sequence_number,
            "expected_revision": expected_revision,
            "safe_message": safe_message,
            "occurred_at": occurred_at,
        }
        match payload:
            case AutomationStateChangedPayload():
                return AutomationStateChangedEnvelope(
                    **common,
                    fact_kind=FactKind.AUTOMATION_STATE_CHANGED,
                    payload=payload,
                )
            case TradeDecisionRecordedPayload():
                return TradeDecisionRecordedEnvelope(
                    **common,
                    fact_kind=FactKind.TRADE_DECISION_RECORDED,
                    payload=payload,
                )
            case BrokerOrderRecordedPayload():
                return BrokerOrderRecordedEnvelope(
                    **common,
                    fact_kind=FactKind.BROKER_ORDER_RECORDED,
                    payload=payload,
                )
            case BrokerOrderStateChangedPayload():
                return BrokerOrderStateChangedEnvelope(
                    **common,
                    fact_kind=FactKind.BROKER_ORDER_STATE_CHANGED,
                    payload=payload,
                )
            case TradeExecutionRecordedPayload():
                return TradeExecutionRecordedEnvelope(
                    **common,
                    fact_kind=FactKind.TRADE_EXECUTION_RECORDED,
                    payload=payload,
                )
            case PositionCycleUpdatedPayload():
                return PositionCycleUpdatedEnvelope(
                    **common,
                    fact_kind=FactKind.POSITION_CYCLE_UPDATED,
                    payload=payload,
                )
            case PositionLotOpenedPayload():
                return PositionLotOpenedEnvelope(
                    **common,
                    fact_kind=FactKind.POSITION_LOT_OPENED,
                    payload=payload,
                )
            case ExecutionLotAllocatedPayload():
                return ExecutionLotAllocatedEnvelope(
                    **common,
                    fact_kind=FactKind.EXECUTION_LOT_ALLOCATED,
                    payload=payload,
                )
            case TradeAuditRecordedPayload():
                return TradeAuditRecordedEnvelope(
                    **common,
                    fact_kind=FactKind.TRADE_AUDIT_RECORDED,
                    payload=payload,
                )
            case _:
                raise AssertionError(f"Unsupported fact payload type: {type(payload).__name__}")
