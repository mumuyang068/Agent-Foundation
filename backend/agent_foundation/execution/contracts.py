from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import Field, model_validator

from agent_foundation.kernel.primitives import ContractModel, DomainName, ResourceRef, stable_fingerprint


class AlgorithmDescriptor(ContractModel):
    algorithm_id: str = Field(min_length=1, max_length=160)
    implementation_version: str = Field(min_length=1, max_length=120)
    domains: tuple[DomainName, ...] = Field(min_length=1)
    parameter_schema: dict[str, Any] = Field(default_factory=dict)
    implementation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionProfile(ContractModel):
    schema_version: str = "agent-foundation.execution-profile.v1"
    profile_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    domain: DomainName
    algorithm_id: str = Field(min_length=1, max_length=160)
    implementation_version: str = Field(min_length=1, max_length=120)
    parameters: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, int] = Field(default_factory=dict)
    fingerprint: str = ""

    @model_validator(mode="after")
    def set_fingerprint(self) -> "ExecutionProfile":
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Execution Profile fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self


class ExecutionStatus(StrEnum):
    succeeded = "succeeded"
    degraded = "degraded"
    failed = "failed"


class ExecutionRecord(ContractModel):
    schema_version: str = "agent-foundation.execution-record.v1"
    execution_id: str = Field(default_factory=lambda: str(uuid4()))
    request_id: str = Field(min_length=1, max_length=240)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    domain: DomainName
    action: str = Field(min_length=1, max_length=120)
    grant_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_profile_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_refs: tuple[ResourceRef, ...] = ()
    output_refs: tuple[ResourceRef, ...] = ()
    status: ExecutionStatus
    diagnostics: dict[str, Any] = Field(default_factory=dict)
