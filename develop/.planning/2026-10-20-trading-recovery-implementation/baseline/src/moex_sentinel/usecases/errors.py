"""Transport-neutral errors exposed by application use cases."""

from moex_sentinel.domain.errors import FieldError


class UseCaseError(Exception):
    def __init__(self, code: str, message: str, fields: tuple[FieldError, ...] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields
