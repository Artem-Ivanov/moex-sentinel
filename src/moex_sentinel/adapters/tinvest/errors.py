"""Safe errors emitted by the T-Invest adapter boundary."""


class TInvestAdapterError(Exception):
    def __init__(self, code: str, safe_message: str, *, retryable: bool) -> None:
        super().__init__(safe_message)
        self.code = code
        self.retryable = retryable
