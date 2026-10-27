"""The lifecycle contract distinguishes user intent from Worker proposals."""

import pytest

from sentinel_contracts.automation_lifecycle import (
    InvalidAutomationTransition,
    TransitionOrigin,
    validate_automation_transition,
)
from sentinel_contracts.trading import AutomationState


@pytest.mark.parametrize(
    ("origin_name", "current", "allowed"),
    [
        ("USER_COMMAND", "IN_QUEUE", {"HOLD", "CLOSED"}),
        ("USER_COMMAND", "OPENING", {"HOLD", "CLOSED"}),
        ("USER_COMMAND", "IN_WORK", {"HOLD", "CLOSED"}),
        ("USER_COMMAND", "HOLD", {"IN_QUEUE", "CLOSED"}),
        ("USER_COMMAND", "CLOSED", set()),
        ("WORKER_FACT", "IN_QUEUE", {"OPENING", "IN_WORK", "HOLD", "CLOSED"}),
        ("WORKER_FACT", "OPENING", {"IN_WORK", "HOLD", "CLOSED"}),
        ("WORKER_FACT", "IN_WORK", {"HOLD", "CLOSED"}),
        ("WORKER_FACT", "HOLD", set()),
        ("WORKER_FACT", "CLOSED", set()),
    ],
)
@pytest.mark.parametrize("target", list(AutomationState))
def test_lifecycle_matrix(origin_name: str, current: str, allowed: set[str], target: AutomationState) -> None:
    origin = TransitionOrigin(origin_name)
    if target.value == current:
        assert validate_automation_transition(AutomationState(current), target, origin=origin) is False
    elif target.value in allowed:
        assert validate_automation_transition(AutomationState(current), target, origin=origin) is True
    else:
        with pytest.raises(InvalidAutomationTransition):
            validate_automation_transition(AutomationState(current), target, origin=origin)


@pytest.mark.parametrize("origin_name", ["USER_COMMAND", "WORKER_FACT"])
@pytest.mark.parametrize("current", [AutomationState.HOLD, AutomationState.CLOSED])
def test_bootstrap_only_permits_worker_activation_from_hold(origin_name: str, current: AutomationState) -> None:
    if current is AutomationState.HOLD and origin_name == "WORKER_FACT":
        assert (
            validate_automation_transition(
                current, AutomationState.IN_WORK, origin=TransitionOrigin(origin_name), bootstrap=True
            )
            is True
        )
    else:
        with pytest.raises(InvalidAutomationTransition):
            validate_automation_transition(
                current, AutomationState.IN_WORK, origin=TransitionOrigin(origin_name), bootstrap=True
            )
