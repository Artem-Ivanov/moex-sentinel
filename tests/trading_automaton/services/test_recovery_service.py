"""Restart recovery never resumes trading automatically."""

from types import SimpleNamespace

from trading_automaton.services.recovery import RecoveryService


class RepositoryStub:
    def __init__(self, monitored=None, active=None) -> None:
        self.monitored = monitored or []
        self.active = active or set()
        self.calls: list[tuple[str, str | None]] = []

    def list_monitored(self):
        return [
            SimpleNamespace(
                automation_id=(automation_id[0] if isinstance(automation_id, tuple) else automation_id),
                bootstrap=(automation_id[1] if isinstance(automation_id, tuple) else None),
            )
            for automation_id in self.monitored
        ]

    def get_active_intent(self, automation_id: str):
        return True if automation_id in self.active else None

    def hold_active(self, reason: str, automation_id: str | None = None) -> None:
        self.calls.append((reason, automation_id))


def test_unclean_restart_moves_active_items_without_open_intent_to_manual_hold() -> None:
    repository = RepositoryStub()
    repository.monitored = ("a", "b", "c")
    repository.active = {"a"}

    RecoveryService(repository).recover(unclean_shutdown=True)

    assert repository.calls == [
        ("Worker restarted; manual RESUME is required", "b"),
        ("Worker restarted; manual RESUME is required", "c"),
    ]


def test_clean_restart_does_not_move_active_items_to_hold() -> None:
    repository = RepositoryStub()

    RecoveryService(repository).recover(unclean_shutdown=False)

    assert repository.calls == []


def test_unclean_restart_preserves_idempotent_position_bootstrap_recovery() -> None:
    repository = RepositoryStub(monitored=[("bootstrap", object())])

    RecoveryService(repository).recover(unclean_shutdown=True)

    assert repository.calls == []
