"""Domain DTOs for structured use-case validation errors."""

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class FieldError(PositionalModel):
    path: str
    code: str
    message: str

    model_config = ConfigDict(frozen=True)

    def __init__(self, path: str, code: str, message: str) -> None:
        super().__init__(path=path, code=code, message=message)
