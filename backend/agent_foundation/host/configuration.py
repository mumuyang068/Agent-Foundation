from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from agent_foundation.kernel.primitives import ContractModel, DomainName, stable_fingerprint


ModuleName = Literal["access", "memory", "evidence", "graph", "context", "jobs", "management", "applications"]


class StoreConfiguration(ContractModel):
    store_id: str = Field(min_length=1, max_length=120)
    driver: Literal["memory", "sqlite", "postgres", "neo4j", "qdrant"]
    endpoint_ref: str | None = Field(default=None, max_length=500)
    credential_ref: str | None = Field(default=None, max_length=240)
    database: str | None = Field(default=None, min_length=1, max_length=160)


class ProjectionConfiguration(ContractModel):
    projection_id: str = Field(min_length=1, max_length=120)
    driver: Literal["neo4j", "qdrant"]
    endpoint_ref: str = Field(min_length=1, max_length=500)
    credential_ref: str | None = Field(default=None, max_length=240)
    database_or_collection: str = Field(min_length=1, max_length=160)
    role: Literal["candidate", "projection"] = "projection"


class HostConfiguration(ContractModel):
    schema_version: Literal["agent-foundation.host-configuration.v1"] = "agent-foundation.host-configuration.v1"
    configuration_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    enabled_modules: tuple[ModuleName, ...] = Field(min_length=1)
    stores: dict[str, StoreConfiguration] = Field(default_factory=dict)
    module_stores: dict[ModuleName, str] = Field(default_factory=dict)
    module_write_stores: dict[ModuleName, str] = Field(default_factory=dict)
    projections: dict[str, ProjectionConfiguration] = Field(default_factory=dict)
    module_projections: dict[ModuleName, tuple[str, ...]] = Field(default_factory=dict)
    module_dependencies: dict[ModuleName, tuple[ModuleName, ...]] = Field(default_factory=dict)
    default_execution_profiles: dict[DomainName, str] = Field(default_factory=dict)
    trusted_algorithms: tuple[str, ...] = ()
    fingerprint: str = ""

    @field_validator("enabled_modules", "trusted_algorithms")
    @classmethod
    def unique_sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def validate_stores_and_fingerprint(self) -> "HostConfiguration":
        if "access" not in self.enabled_modules:
            raise ValueError("The access module is required")
        missing = {
            name
            for module, name in self.module_stores.items()
            if module in self.enabled_modules and name not in self.stores
        }
        if missing:
            raise ValueError(f"Unknown module stores: {', '.join(sorted(missing))}")
        missing_write_stores = {
            name
            for module, name in self.module_write_stores.items()
            if module in self.enabled_modules and name not in self.stores
        }
        if missing_write_stores:
            raise ValueError(
                f"Unknown module write stores: {', '.join(sorted(missing_write_stores))}"
            )
        disabled_write_store_owners = set(self.module_write_stores) - set(
            self.enabled_modules
        )
        if disabled_write_store_owners:
            raise ValueError(
                "Write store owners are disabled: "
                + ", ".join(sorted(disabled_write_store_owners))
            )
        unsupported_write_store_owners = set(self.module_write_stores) - {"graph"}
        if unsupported_write_store_owners:
            raise ValueError(
                "Dedicated write stores are unsupported for: "
                + ", ".join(sorted(unsupported_write_store_owners))
            )
        missing_projections = {
            name
            for module, names in self.module_projections.items()
            if module in self.enabled_modules
            for name in names
            if name not in self.projections
        }
        if missing_projections:
            raise ValueError(f"Unknown module projections: {', '.join(sorted(missing_projections))}")
        disabled_projection_owners = set(self.module_projections) - set(self.enabled_modules)
        if disabled_projection_owners:
            raise ValueError(
                "Projection owners are disabled: " + ", ".join(sorted(disabled_projection_owners))
            )
        object.__setattr__(
            self,
            "module_projections",
            {module: tuple(sorted(set(names))) for module, names in self.module_projections.items()},
        )
        normalized_dependencies = {
            module: tuple(sorted(set(dependencies)))
            for module, dependencies in self.module_dependencies.items()
        }
        object.__setattr__(self, "module_dependencies", normalized_dependencies)
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Host Configuration fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self
