"""Safe broker failures crossing application and adapter boundaries."""


class BrokerOperationError(Exception):
    """A stable public code, safe message and explicit retry policy."""

    def __init__(self, code: str, safe_message: str, *, retryable: bool) -> None:
        super().__init__(safe_message)
        self.code = code
        self.retryable = retryable
