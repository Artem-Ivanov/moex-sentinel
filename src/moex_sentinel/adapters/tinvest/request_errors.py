"""Translate declared SDK request failures into the safe adapter contract."""

from grpc import StatusCode
from grpc.aio import AioRpcError
from t_tech.invest.exceptions import AioRequestError

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError

SDK_REQUEST_ERRORS = (AioRequestError, AioRpcError)


def request_status(error: AioRequestError | AioRpcError) -> StatusCode:
    """Read the SDK status attribute or the underlying gRPC status method."""
    return error.code if isinstance(error, AioRequestError) else error.code()


def map_request_error(error: AioRequestError | AioRpcError) -> TInvestAdapterError:
    """Classify only declared transport failures and discard private SDK diagnostics."""
    mapping = {
        StatusCode.UNAUTHENTICATED: ("BROKER_AUTH_FAILED", "Проверка токена не пройдена.", False),
        StatusCode.PERMISSION_DENIED: ("BROKER_FORBIDDEN", "Недостаточно прав доступа.", False),
        StatusCode.RESOURCE_EXHAUSTED: ("BROKER_RATE_LIMITED", "Превышен лимит запросов.", True),
        StatusCode.UNAVAILABLE: ("BROKER_UNAVAILABLE", "Площадка временно недоступна.", True),
        StatusCode.DEADLINE_EXCEEDED: ("BROKER_UNAVAILABLE", "Площадка временно недоступна.", True),
    }
    code, message, retryable = mapping.get(
        request_status(error),
        ("BROKER_UNAVAILABLE", "Не удалось получить данные площадки.", False),  # noqa: RUF001
    )
    return TInvestAdapterError(code, message, retryable=retryable)


def invalid_response_error() -> TInvestAdapterError:
    """Keep malformed broker values non-retryable and exclude their contents from messages."""
    return TInvestAdapterError(
        "BROKER_UNAVAILABLE", "Не удалось получить данные площадки.", retryable=False  # noqa: RUF001
    )
