"""Broker observations permit recovery, never regress admission or finalized facts."""

import pytest

from sentinel_contracts.broker_execution import BrokerOrderStatus as S
from sentinel_contracts.broker_order_policy import validate_order_transition
from sentinel_contracts.trading_facts import FactBrokerOrderStatus


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("SUBMITTING", "FILLED"),
        ("UNCERTAIN", "SUBMITTED"),
        ("UNCERTAIN", "REJECTED"),
        ("FILLED", "UNCERTAIN"),
        ("FILLED", "REJECTED"),
        ("CANCELLED", "ACCEPTED"),
        ("FAILED", "ACCEPTED"),
        ("FAILED", "EXPIRED"),
        ("EXPIRED", "PARTIALLY_FILLED"),
    ],
)
def test_broker_confirmed_recovery_edges(current, target):
    validate_order_transition(S(current), S(target), finalized=False)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("ACCEPTED", "SUBMITTING"),
        ("ACCEPTED", "DISPATCH_PENDING"),
        ("PARTIALLY_FILLED", "ACCEPTED"),
        ("UNCERTAIN", "SUBMITTING"),
        ("FILLED", "CREATED"),
        ("FILLED", "SUBMITTED"),
        ("FAILED", "SUBMITTING"),
        ("FAILED", "UNCERTAIN"),
        ("CREATED", "FAILED"),
    ],
)
def test_regressions_and_invented_failed_writers_are_forbidden(current, target):
    with pytest.raises(ValueError, match="transition"):
        validate_order_transition(S(current), S(target), finalized=False)


@pytest.mark.parametrize("current", ["FILLED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED"])
def test_finalized_order_only_allows_same_state(current):
    for target in S:
        if target.value == current:
            validate_order_transition(S(current), target, finalized=True)
        else:
            with pytest.raises(ValueError, match="transition"):
                validate_order_transition(S(current), target, finalized=True)
    assert FactBrokerOrderStatus is S
