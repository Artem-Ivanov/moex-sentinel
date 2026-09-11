"""Business rules for the active application environment."""

from typing import Protocol, cast

from moex_sentinel.domain.environment import Environment, EnvironmentState


class EnvironmentRepositoryPort(Protocol):
    def get_active_environment(self) -> str: ...

    def set_active_environment(self, environment: str) -> str: ...


class EnvironmentStatePort(Protocol):
    def view(self) -> EnvironmentState: ...


class EnvironmentMismatchError(ValueError):
    """A broker belongs to an inactive application environment."""


class EnvironmentService:
    def __init__(self, repository: EnvironmentRepositoryPort) -> None:
        self._repository = repository

    def view(self) -> EnvironmentState:
        return self._state(self._repository.get_active_environment())

    def switch(self, environment: str) -> EnvironmentState:
        if environment not in {"TEST", "PROD"}:
            raise ValueError("Unsupported application environment.")
        return self._state(self._repository.set_active_environment(environment))

    @staticmethod
    def _state(environment: str) -> EnvironmentState:
        is_test = environment == "TEST"
        return EnvironmentState(cast(Environment, environment), is_test, is_test)
