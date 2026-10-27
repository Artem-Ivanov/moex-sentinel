"""Pure transition policy shared by authoritative Core and proposing Worker."""

from enum import Enum

from sentinel_contracts.trading import AutomationState

TECHNICAL_RECOVERY_RESUME_MESSAGE = "Automation resumed after technical recovery"


class TransitionOrigin(str, Enum):
    USER_COMMAND = "USER_COMMAND"
    WORKER_FACT = "WORKER_FACT"


class InvalidAutomationTransition(ValueError):
    """The requested transition contradicts the automation lifecycle."""


_USER_TRANSITIONS = {
    AutomationState.IN_QUEUE: {AutomationState.HOLD, AutomationState.CLOSED},
    AutomationState.OPENING: {AutomationState.HOLD, AutomationState.CLOSED},
    AutomationState.IN_WORK: {AutomationState.HOLD, AutomationState.CLOSED},
    AutomationState.HOLD: {AutomationState.IN_QUEUE, AutomationState.CLOSED},
    AutomationState.CLOSED: set(),
}
_WORKER_TRANSITIONS = {
    AutomationState.IN_QUEUE: {
        AutomationState.OPENING,
        AutomationState.IN_WORK,
        AutomationState.HOLD,
        AutomationState.CLOSED,
    },
    AutomationState.OPENING: {AutomationState.IN_WORK, AutomationState.HOLD, AutomationState.CLOSED},
    AutomationState.IN_WORK: {AutomationState.HOLD, AutomationState.CLOSED},
    AutomationState.HOLD: set(),
    AutomationState.CLOSED: set(),
}


def validate_automation_transition(
    current: AutomationState,
    target: AutomationState,
    *,
    origin: TransitionOrigin,
    bootstrap: bool = False,
    recovery_resume: bool = False,
) -> bool:
    """Return whether state changes; bootstrap must be verified by the caller."""
    if recovery_resume and not (
        origin is TransitionOrigin.WORKER_FACT
        and current is AutomationState.HOLD
        and target is AutomationState.IN_QUEUE
    ):
        raise InvalidAutomationTransition("Recovery resume only permits a Worker HOLD to IN_QUEUE transition.")
    if current is target:
        return False
    transitions = _USER_TRANSITIONS if origin is TransitionOrigin.USER_COMMAND else _WORKER_TRANSITIONS
    if target in transitions[current] or (
        origin is TransitionOrigin.WORKER_FACT
        and bootstrap
        and current is AutomationState.HOLD
        and target is AutomationState.IN_WORK
    ) or (
        origin is TransitionOrigin.WORKER_FACT
        and recovery_resume
        and current is AutomationState.HOLD
        and target is AutomationState.IN_QUEUE
    ):
        return True
    raise InvalidAutomationTransition(f"{origin.value} cannot transition {current.value} to {target.value}.")
