"""Safe broker failures are independent of the vendor transport."""

from sentinel_contracts.broker_errors import BrokerOperationError


def test_broker_failure_exposes_only_safe_public_fields():

    error = BrokerOperationError("UNAVAILABLE", "Broker temporarily unavailable", retryable=True)
    assert str(error) == "Broker temporarily unavailable"
    assert error.code == "UNAVAILABLE"
    assert error.retryable is True
