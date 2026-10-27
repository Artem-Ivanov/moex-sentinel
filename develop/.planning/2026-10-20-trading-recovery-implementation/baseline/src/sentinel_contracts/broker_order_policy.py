"""Order transitions shared by Worker persistence and Core fact ingestion."""

from sentinel_contracts.broker_execution import BrokerOrderStatus as S

BROKER_TERMINAL_STATES = frozenset({S.FILLED, S.CANCELLED, S.REJECTED, S.EXPIRED})
LEGACY_TERMINAL_STATES = BROKER_TERMINAL_STATES | {S.FAILED}
_OPEN = {S.ACCEPTED, S.PARTIALLY_FILLED}
_TRANSITIONS = {
    S.CREATED: {S.DISPATCH_PENDING, S.SUBMITTING, S.SUBMITTED, S.UNCERTAIN} | _OPEN | BROKER_TERMINAL_STATES,
    S.DISPATCH_PENDING: {S.SUBMITTING, S.SUBMITTED, S.UNCERTAIN} | _OPEN | BROKER_TERMINAL_STATES,
    S.SUBMITTING: {S.SUBMITTED, S.UNCERTAIN} | _OPEN | BROKER_TERMINAL_STATES,
    S.SUBMITTED: {S.UNCERTAIN} | _OPEN | BROKER_TERMINAL_STATES,
    S.ACCEPTED: {S.PARTIALLY_FILLED, S.UNCERTAIN} | BROKER_TERMINAL_STATES,
    S.PARTIALLY_FILLED: {S.UNCERTAIN} | BROKER_TERMINAL_STATES,
    S.UNCERTAIN: {S.SUBMITTED} | _OPEN | BROKER_TERMINAL_STATES,
}


def validate_order_transition(current: S, target: S, *, finalized: bool) -> None:
    current, target = S(current), S(target)
    if current == target:
        return
    allowed = _TRANSITIONS.get(current, set())
    if current in LEGACY_TERMINAL_STATES:
        allowed = _OPEN | BROKER_TERMINAL_STATES
        if current != S.FAILED:
            allowed = allowed | {S.UNCERTAIN}
    if finalized or target not in allowed:
        raise ValueError(f"Invalid broker order transition: {current.value} -> {target.value}.")
