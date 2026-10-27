"""Broker execution port values shared by Core and the trading worker."""

from datetime import datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, model_validator

from sentinel_contracts.base import PositionalModel
from sentinel_contracts.tinvest import BrokerAccessMode, BrokerEnvironment
from sentinel_contracts.trading_admission import ConfigurationFingerprint, PreRpcAbortProof, SubmitDenialReason


class BrokerOrderStatus(StrEnum):
    CREATED = "CREATED"
    DISPATCH_PENDING = "DISPATCH_PENDING"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNCERTAIN = "UNCERTAIN"
    FAILED = "FAILED"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class BrokerRecoveryOperation(PositionalModel):
    """Neutral recovery observation; quantity is instrument units (shares), not lots."""

    model_config = ConfigDict(frozen=True)
    operation_id: str
    side: OrderSide | None
    executed: bool
    occurred_at: datetime
    quantity_units: Decimal
    price: Decimal
    commission: Decimal
    currency: str


class OrderBookLevel(PositionalModel):
    model_config = ConfigDict(frozen=True)
    price: Decimal
    quantity_lots: int


class OrderBookSnapshot(PositionalModel):
    model_config = ConfigDict(frozen=True)
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    captured_at: datetime

    @property
    def best_bid(self) -> OrderBookLevel:
        return self.bids[0]

    @property
    def best_ask(self) -> OrderBookLevel:
        return self.asks[0]


class LimitOrderEstimate(PositionalModel):
    model_config = ConfigDict(frozen=True)
    total_amount: Decimal
    estimated_commission: Decimal
    currency: str


class BrokerOrderState(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_order_id: str
    idempotency_key: str
    status: BrokerOrderStatus
    requested_lots: int
    executed_lots: int
    requested_amount: Decimal
    executed_amount: Decimal
    estimated_commission: Decimal
    executed_commission: Decimal
    currency: str
    executed_price: Decimal = Decimal()
    executed_at: datetime | None = None
    pre_rpc_abort_proof: PreRpcAbortProof | None = Field(default=None, exclude_if=lambda value: value is None)
    submit_denial_reason: SubmitDenialReason | None = Field(default=None, exclude_if=lambda value: value is None)
    submit_authorization_elapsed_ms: Annotated[int, Field(strict=True, ge=0)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    permit_remaining_ms: Annotated[int, Field(strict=True)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_local_no_send(self):
        proof = self.pre_rpc_abort_proof
        denial = self.submit_denial_reason
        if proof is None and denial is None:
            return self
        if denial is SubmitDenialReason.ALREADY_CONSUMED:
            raise ValueError("An already-consumed submit cannot be a definite local no-send.")
        if proof is not None and str(proof.idempotency_key) != self.idempotency_key:
            raise ValueError("Pre-RPC abort proof requires a matching idempotency key.")
        if (
            not all(
                value.is_finite()
                for value in (
                    self.requested_amount,
                    self.executed_amount,
                    self.executed_commission,
                    self.executed_price,
                )
            )
            or self.status != "CANCELLED"
            or self.broker_order_id
            or self.requested_lots <= 0
            or self.requested_amount <= 0
            or self.executed_lots != 0
            or self.executed_amount != 0
            or self.executed_commission != 0
            or self.executed_price != 0
            or self.executed_at is not None
        ):
            raise ValueError("Local no-send metadata requires a zero-fill cancelled order without a broker ID.")
        return self


class BrokerScope(PositionalModel):
    """Immutable broker identity without credentials or execution capability."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    broker_id: str
    environment: BrokerEnvironment
    account_id: str


class BrokerConnection(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_id: str
    adapter_code: str
    target: str
    token: str
    is_test: bool
    environment: BrokerEnvironment | None = None
    access_mode: BrokerAccessMode = "READ_ONLY"
    account_id: str = ""
    configuration_fingerprint: ConfigurationFingerprint | None = None

    @model_validator(mode="after")
    def validate_environment(self):
        expected = "TEST" if self.is_test else "PROD"
        if self.environment is not None and self.environment != expected:
            raise ValueError("Broker environment and is_test disagree.")
        if expected == "PROD" and not self.account_id.strip():
            raise ValueError("PROD requires a selected account.")
        object.__setattr__(self, "environment", expected)
        return self


class BrokerPosition(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    quantity_lots: Decimal
    average_price: Decimal
    current_price: Decimal
    currency: str
    blocked: bool = False


class BrokerTradingStatus(PositionalModel):
    model_config = ConfigDict(frozen=True)
    status: str
    limit_order_available: bool
    market_order_available: bool
    api_trade_available: bool
