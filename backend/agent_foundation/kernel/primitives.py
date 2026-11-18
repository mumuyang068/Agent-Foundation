from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


DomainName = Literal[
    "memory", "evidence", "graph", "context", "jobs", "management", "applications"
]


def stable_fingerprint(value: BaseModel | dict[str, Any]) -> str:
    payload = value.model_dump(mode="json", exclude={"fingerprint"}) if isinstance(value, BaseModel) else value
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScopeRef(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=120)
    space_id: str = Field(min_length=1, max_length=120)
    project_id: str | None = Field(default=None, max_length=120)


class Principal(ContractModel):
    principal_id: str = Field(min_length=1, max_length=240)
    kind: Literal["user", "service", "agent", "operator"]
    delegation_id: str | None = Field(default=None, max_length=240)


class ResourceRef(ContractModel):
    domain: DomainName
    scope: ScopeRef
    resource_type: str = Field(min_length=1, max_length=120)
    resource_id: str = Field(min_length=1, max_length=240)
    revision: str | None = Field(default=None, max_length=160)


class OperationContext(ContractModel):
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=240)
    principal: Principal
    grant_id: str = Field(min_length=1, max_length=240)
    scope: ScopeRef
    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: int = Field(ge=1)
    execution_profile_id: str | None = Field(default=None, max_length=160)
    execution_profile_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def complete_execution_profile(self) -> "OperationContext":
        if (self.execution_profile_id is None) != (self.execution_profile_version is None):
            raise ValueError("Execution Profile ID and version must be supplied together")
        return self
