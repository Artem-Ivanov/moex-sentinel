import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from sentinel_contracts.trading_admission import RuntimeRegistrationResult, RuntimeRegistrationStatus
from tests.migrations.strict_state_helpers import (
    BOOTSTRAP,
    INVALID_BOOTSTRAP,
    INVALID_RUNTIME,
    VALID_RUNTIME,
    insert_bootstrap,
    insert_runtime,
    seed_scope,
)


@pytest.mark.parametrize("bootstrap", INVALID_BOOTSTRAP)
def test_orm_rejects_incomplete_or_nonpositive_bootstrap(core_engine, bootstrap):
    seed_scope(core_engine)
    with pytest.raises(IntegrityError, match="ck_trading_automations_complete_bootstrap_snapshot"):
        insert_bootstrap(core_engine, bootstrap)


@pytest.mark.parametrize("runtime", INVALID_RUNTIME)
def test_orm_rejects_incompatible_runtime_identity(core_engine, runtime):
    seed_scope(core_engine)
    with pytest.raises(IntegrityError, match="ck_worker_runtime_"):
        insert_runtime(core_engine, *runtime)


def test_orm_accepts_complete_bootstrap_and_runtime_states(core_engine):
    seed_scope(core_engine)
    insert_bootstrap(core_engine, {})
    insert_bootstrap(core_engine, BOOTSTRAP)
    for runtime in VALID_RUNTIME:
        insert_runtime(core_engine, *runtime)


@pytest.mark.parametrize("status", RuntimeRegistrationStatus)
def test_wire_registration_status_is_accepted_by_storage(core_engine, status):
    seed_scope(core_engine)
    generation, lease = (
        (1, BOOTSTRAP["bootstrap_observed_at"]) if status is RuntimeRegistrationStatus.OWNER else (None, None)
    )
    result = RuntimeRegistrationResult(
        runtime_instance_id="00000000-0000-4000-8000-000000000001",
        generation=generation,
        registration_status=status,
        lease_expires_at=lease,
        admission_status="DISARMED",
    )
    insert_runtime(
        core_engine, result.model_dump(mode="json")["registration_status"], result.generation, result.lease_expires_at
    )


def test_retired_runtime_is_storage_history_and_cannot_be_returned_as_a_registration(core_engine):
    seed_scope(core_engine)
    insert_runtime(core_engine, "RETIRED", 1, None)
    with pytest.raises(ValidationError):
        RuntimeRegistrationResult(
            runtime_instance_id="00000000-0000-4000-8000-000000000001",
            generation=1,
            registration_status="RETIRED",
            lease_expires_at=None,
            admission_status="DISARMED",
        )
