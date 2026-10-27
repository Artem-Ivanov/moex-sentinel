from moex_sentinel.services.environment import EnvironmentService


class Repository:
    def __init__(self) -> None:
        self.value = "TEST"

    def get_active_environment(self) -> str:
        return self.value

    def set_active_environment(self, environment: str) -> str:
        self.value = environment
        return environment


def test_environment_service_exposes_test_capabilities_and_persists_prod() -> None:
    repository = Repository()
    service = EnvironmentService(repository)

    assert service.view().can_prepare_test_environment is True
    state = service.switch("PROD")

    assert state.active_environment == "PROD"
    assert state.can_create_broker is False
    assert state.can_prepare_test_environment is False
    assert service.view() == state
