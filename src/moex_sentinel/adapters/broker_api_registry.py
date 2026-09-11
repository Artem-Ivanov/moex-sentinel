"""Registry of broker API modules available in this application build."""

from typing import Protocol

from moex_sentinel.domain.user_brokers import (
    BrokerApiDescriptor,
    BrokerApiNotFoundError,
    BrokerApiRegistryDuplicateError,
)


class BrokerApiModule(Protocol):
    """Code-owned broker API declaration and settings validator."""

    descriptor: BrokerApiDescriptor

    def default_fqdn(self, environment: str) -> str:
        """Return the default endpoint for a supported environment."""

    def validate_settings(self, value: object) -> dict[str, object]:
        """Validate adapter-specific settings and return JSON-ready values."""


class BrokerApiRegistry:
    """Immutable lookup of broker API modules by stable slug."""

    def __init__(self, modules: tuple[BrokerApiModule, ...]) -> None:
        self._modules = modules
        self._modules_by_slug = {module.descriptor.api_slug: module for module in modules}
        if len(self._modules_by_slug) != len(modules):
            duplicate_slug = next(
                module.descriptor.api_slug
                for index, module in enumerate(modules)
                if module.descriptor.api_slug in {item.descriptor.api_slug for item in modules[:index]}
            )
            raise BrokerApiRegistryDuplicateError(duplicate_slug)

    def list(self) -> tuple[BrokerApiDescriptor, ...]:
        return tuple(module.descriptor for module in self._modules)

    def get(self, api_slug: str) -> BrokerApiModule:
        try:
            return self._modules_by_slug[api_slug]
        except KeyError:
            raise BrokerApiNotFoundError(api_slug) from None

    def validate_settings(self, api_slug: str, value: object) -> dict[str, object]:
        return self.get(api_slug).validate_settings(value)
