"""Lifecycle failures use the same neutral contract for every repository."""

import asyncio

import pytest

from moex_sentinel.domain.persistence_errors import DuplicateRecordError, RecordNotFoundError, RevisionConflictError
from moex_sentinel.usecases.automations import CreateTradingAutomationUsecase, ViewTradingAutomationUsecase
from moex_sentinel.usecases.errors import UseCaseError


class FailingAutomation:
    def __init__(self, error):
        self.error = error

    def create(self, *, broker_id, account_id, instrument_id):
        raise self.error

    def get(self, automation_id):
        raise self.error


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (RecordNotFoundError, "AUTOMATION_NOT_FOUND"),
        (DuplicateRecordError, "AUTOMATION_ALREADY_ACTIVE"),
        (RevisionConflictError, "AUTOMATION_REVISION_CONFLICT"),
    ],
)
@pytest.mark.parametrize("operation", ["create", "get"])
def test_repository_independent_failures_keep_lifecycle_public_codes(error_type, code, operation):
    error = error_type("private repository diagnostic")
    service = FailingAutomation(error)

    def execute():
        if operation == "create":
            return asyncio.run(CreateTradingAutomationUsecase(service).execute("broker", "account", "instrument"))
        return ViewTradingAutomationUsecase(service).execute("automation")

    with pytest.raises(UseCaseError) as caught:
        execute()
    assert caught.value.code == code
    assert caught.value.__cause__ is error
    assert "private" not in caught.value.message
