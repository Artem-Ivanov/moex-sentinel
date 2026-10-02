"""Code owned allowlist for T-Invest environments and transport targets."""

from typing import Literal

BrokerEnvironment = Literal["TEST", "PROD"]
BrokerAccessMode = Literal["READ_ONLY", "TRADE"]
TINVEST_TARGETS = {
    "TEST": "sandbox-invest-public-api.tbank.ru:443",
    "PROD": "invest-public-api.tbank.ru:443",
}


def tinvest_environment(target: str) -> BrokerEnvironment:
    for environment, allowed in TINVEST_TARGETS.items():
        if target == allowed:
            return environment  # type: ignore[return-value]
    raise ValueError("Unsupported T-Invest target.")


def resolve_tinvest_endpoint(environment: str, adapter_code: str, target: str | None = None) -> str:
    if environment not in TINVEST_TARGETS:
        raise ValueError("Unsupported broker environment.")
    if adapter_code not in {"t_invest", f"TINVEST_{'SANDBOX' if environment == 'TEST' else 'PROD'}"}:
        raise ValueError("Broker adapter does not match environment.")
    expected = TINVEST_TARGETS[environment]
    if target is not None and target != expected:
        raise ValueError("Broker target does not match environment.")
    return expected
