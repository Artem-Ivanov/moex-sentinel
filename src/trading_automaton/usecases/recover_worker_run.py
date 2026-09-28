"""Begin one Worker run and apply restart recovery before scheduling iterations."""

from typing import Protocol


class WorkerRunStartPort(Protocol):
    def begin_run(self, worker_id: str) -> bool: ...


class WorkerRecoveryPort(Protocol):
    def recover(self, *, unclean_shutdown: bool) -> None: ...


class RecoverWorkerRunUsecase:
    """Keep durable run detection and recovery in a single startup operation."""

    def __init__(self, runs: WorkerRunStartPort, recovery: WorkerRecoveryPort, *, worker_id: str) -> None:
        self._runs = runs
        self._recovery = recovery
        self._worker_id = worker_id

    def execute(self) -> None:
        """Begin the durable run, then recover according to its previous shutdown marker."""
        unclean_shutdown = self._runs.begin_run(self._worker_id)
        self._recovery.recover(unclean_shutdown=unclean_shutdown)
