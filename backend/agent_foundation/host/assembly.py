from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from agent_foundation.kernel.errors import FoundationError


class ComponentFactory(Protocol):
    def __call__(self, context: "ComponentContext") -> Any: ...


@dataclass(frozen=True)
class ComponentSpec:
    name: str
    dependencies: tuple[str, ...]
    factory: ComponentFactory


class ComponentContext:
    """Dependency view containing only the dependencies declared by a component."""

    def __init__(self, owner: str, allowed: tuple[str, ...], components: dict[str, Any]) -> None:
        self._owner = owner
        self._allowed = frozenset(allowed)
        self._components = components

    def require(self, name: str) -> Any:
        if name not in self._allowed:
            raise FoundationError(
                "module_dependency_undeclared",
                f"Component {self._owner!r} did not declare dependency {name!r}",
            )
        try:
            return self._components[name]
        except KeyError as error:
            raise FoundationError("module_dependency_unavailable", f"Dependency {name!r} is unavailable") from error


class ComponentAssembly:
    """One-time composition with deterministic failure cleanup."""

    def __init__(self, enabled: tuple[str, ...], specs: tuple[ComponentSpec, ...]) -> None:
        self._components: dict[str, Any] = {}
        self._closed = False
        catalog = {spec.name: spec for spec in specs}
        if len(catalog) != len(specs):
            raise FoundationError("module_duplicate", "Component specs must have unique names")
        unknown = set(enabled) - set(catalog)
        if unknown:
            raise FoundationError("module_unknown", f"No component factory for: {', '.join(sorted(unknown))}")
        visiting: set[str] = set()
        try:
            for name in enabled:
                self._build(name, enabled=frozenset(enabled), catalog=catalog, visiting=visiting)
        except BaseException:
            self.close()
            raise

    def _build(
        self,
        name: str,
        *,
        enabled: frozenset[str],
        catalog: dict[str, ComponentSpec],
        visiting: set[str],
    ) -> Any:
        if name in self._components:
            return self._components[name]
        if name in visiting:
            raise FoundationError("module_dependency_cycle", f"Dependency cycle reaches {name!r}")
        visiting.add(name)
        spec = catalog[name]
        disabled = set(spec.dependencies) - enabled
        if disabled:
            raise FoundationError(
                "module_dependency_disabled",
                f"Component {name!r} requires disabled components: {', '.join(sorted(disabled))}",
            )
        for dependency in spec.dependencies:
            self._build(dependency, enabled=enabled, catalog=catalog, visiting=visiting)
        component = spec.factory(ComponentContext(name, spec.dependencies, dict(self._components)))
        if component is None:
            raise FoundationError("module_factory_invalid", f"Component {name!r} returned no implementation")
        self._components[name] = component
        visiting.remove(name)
        return component

    def get(self, name: str) -> Any:
        try:
            return self._components[name]
        except KeyError as error:
            raise FoundationError("module_disabled", f"Component {name!r} is not enabled") from error

    @property
    def enabled(self) -> tuple[str, ...]:
        return tuple(self._components)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for component in reversed(tuple(self._components.values())):
            close: Callable[[], None] | None = getattr(component, "close", None)
            if close is not None:
                close()
        self._components.clear()
