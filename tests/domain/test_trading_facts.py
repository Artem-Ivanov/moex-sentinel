from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

from moex_sentinel.domain.trading_facts import (
    PositionCycleDraft,
    TradingFactErrorCode,
    TradingFactPersistenceError,
)
from tests.domain.trading_facts_helpers import all_fact_drafts


@pytest.mark.parametrize("draft", all_fact_drafts())
def test_fact_drafts_are_frozen_and_forbid_extra_fields(draft: object) -> None:
    model_type = cast(type[BaseModel], type(draft))
    model_dump = draft.model_dump()  # type: ignore[attr-defined]

    with pytest.raises(ValidationError):
        model_type.model_validate({**model_dump, "unexpected": True})
    with pytest.raises(ValidationError):
        draft.__setattr__(next(iter(model_dump)), "changed")


def test_invalid_lifecycle_value_is_rejected() -> None:
    cycle = next(value for value in all_fact_drafts() if isinstance(value, PositionCycleDraft))

    with pytest.raises(ValidationError):
        PositionCycleDraft.model_validate({**cycle.model_dump(), "state": "BROKEN"})


def test_persistence_error_exposes_stable_code_without_payload() -> None:
    error = TradingFactPersistenceError(
        TradingFactErrorCode.FACT_ID_CONFLICT,
        entity_type="trade_execution",
        constraint_name="uq_trade_executions_fact_id",
    )

    assert error.code is TradingFactErrorCode.FACT_ID_CONFLICT
    assert error.entity_type == "trade_execution"
    assert error.constraint_name == "uq_trade_executions_fact_id"
    assert "synthetic-payload" not in str(error)
