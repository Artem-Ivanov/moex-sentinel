"""Shared idempotency and safe database-error translation for typed facts."""

from collections.abc import Callable
from typing import TypeVar, cast

from sqlalchemy import ColumnElement, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import InstrumentedAttribute, Session

from moex_sentinel.domain.trading_facts import TradingFactErrorCode, TradingFactPersistenceError

ModelT = TypeVar("ModelT")
ValueT = TypeVar("ValueT")

CONSTRAINT_ERRORS: dict[str, TradingFactErrorCode] = {
    "uq_trade_decisions_fact_id": TradingFactErrorCode.FACT_ID_CONFLICT,
    "uq_broker_orders_fact_id": TradingFactErrorCode.FACT_ID_CONFLICT,
    "uq_broker_order_events_fact_id": TradingFactErrorCode.FACT_ID_CONFLICT,
    "uq_trade_executions_fact_id": TradingFactErrorCode.FACT_ID_CONFLICT,
    "uq_broker_orders_scope_idempotency": TradingFactErrorCode.ORDER_IDEMPOTENCY_CONFLICT,
    "uq_broker_orders_scope_decision": TradingFactErrorCode.INVALID_STATE,
    "uq_broker_orders_scope_external_order": TradingFactErrorCode.INVALID_STATE,
    "uq_trade_executions_scope_external_execution": TradingFactErrorCode.INVALID_STATE,
    "uq_automation_events_scope_sequence": TradingFactErrorCode.SEQUENCE_CONFLICT,
    "uq_trading_automations_active_scope_instrument": TradingFactErrorCode.INVALID_STATE,
    "uq_position_cycles_open_scope_automation": TradingFactErrorCode.INVALID_STATE,
}

SQLITE_SIGNATURES: dict[str, str] = {
    "trade_decisions.fact_id": "uq_trade_decisions_fact_id",
    "broker_orders.fact_id": "uq_broker_orders_fact_id",
    "broker_order_events.fact_id": "uq_broker_order_events_fact_id",
    "trade_executions.fact_id": "uq_trade_executions_fact_id",
    "broker_orders.user_broker_id, broker_orders.idempotency_key": ("uq_broker_orders_scope_idempotency"),
    "broker_orders.user_broker_id, broker_orders.decision_id": "uq_broker_orders_scope_decision",
    "broker_orders.user_broker_id, broker_orders.external_order_id": ("uq_broker_orders_scope_external_order"),
    "trade_executions.user_broker_id, trade_executions.external_execution_id": (
        "uq_trade_executions_scope_external_execution"
    ),
    "automation_events.user_broker_id, automation_events.automation_id, "
    "automation_events.sequence_number": "uq_automation_events_scope_sequence",
    "trading_automations.user_broker_id, trading_automations.instrument_id": (
        "uq_trading_automations_active_scope_instrument"
    ),
    "position_cycles.user_broker_id, position_cycles.automation_id": ("uq_position_cycles_open_scope_automation"),
}


def _constraint_name(error: IntegrityError) -> str | None:
    diagnostic = getattr(error.orig, "diag", None)
    postgresql_name = getattr(diagnostic, "constraint_name", None)
    if isinstance(postgresql_name, str):
        return postgresql_name
    driver_message = str(error.orig)
    check_prefix = "CHECK constraint failed: "
    if check_prefix in driver_message:
        return driver_message.split(check_prefix, maxsplit=1)[1].splitlines()[0].strip()
    for signature, constraint_name in SQLITE_SIGNATURES.items():
        if signature in driver_message:
            return constraint_name
    return None


def require_scoped_reference(
    session: Session,
    *,
    scope_column: InstrumentedAttribute[str],
    id_column: InstrumentedAttribute[str],
    user_broker_id: str,
    reference_id: str | None,
    entity_type: str,
) -> None:
    """Require a referenced row to exist inside the caller's user-broker scope."""
    if reference_id is None:
        return
    found = session.scalar(
        select(id_column).where(
            scope_column == user_broker_id,
            id_column == reference_id,
        )
    )
    if found is None:
        raise TradingFactPersistenceError(TradingFactErrorCode.CROSS_SCOPE, entity_type=entity_type)


def flush_or_translate(session: Session, *, entity_type: str) -> None:
    """Flush or raise a stable typed error for a known named constraint."""
    try:
        session.flush()
    except IntegrityError as error:
        constraint_name = _constraint_name(error)
        code = CONSTRAINT_ERRORS.get(constraint_name or "")
        if (
            code is None
            and constraint_name is not None
            and constraint_name.startswith("fk_")
            and "_scoped_" in constraint_name
        ):
            code = TradingFactErrorCode.CROSS_SCOPE
        elif code is None and constraint_name is not None and constraint_name.startswith(("ck_", "uq_")):
            code = TradingFactErrorCode.INVALID_STATE
        elif code is None and "FOREIGN KEY constraint failed" in str(error.orig):
            code = TradingFactErrorCode.CROSS_SCOPE
        if code is None:
            raise
        raise TradingFactPersistenceError(
            code,
            entity_type=entity_type,
            constraint_name=constraint_name,
        ) from error


def append_idempotent(
    session: Session,
    *,
    candidate: ModelT,
    identity: ColumnElement[bool],
    to_value: Callable[[ModelT], ValueT],
    conflict_code: TradingFactErrorCode,
    entity_type: str,
) -> ValueT:
    """Append an immutable row or accept an exact retry of its identity."""
    model_type = type(candidate)
    existing = cast(ModelT | None, session.scalar(select(model_type).where(identity)))
    candidate_value = to_value(candidate)
    if existing is not None:
        existing_value = to_value(existing)
        if existing_value == candidate_value:
            return existing_value
        raise TradingFactPersistenceError(conflict_code, entity_type=entity_type)
    session.add(candidate)
    flush_or_translate(session, entity_type=entity_type)
    return candidate_value
