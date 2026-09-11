"""Runtime-independent access to the application use-case registry."""

from typing import Any

from fastapi import Request


def usecases(request: Request) -> Any:
    """Return the registry without importing the concrete composition root."""
    return request.app.state.usecases
