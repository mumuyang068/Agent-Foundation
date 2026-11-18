from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from agent_foundation.kernel.primitives import ContractModel, ScopeRef, stable_fingerprint


class JobPolicy(ContractModel):
    policy_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    max_attempts: int = Field(default=5, ge=1, le=100)
    max_lease_seconds: int = Field(default=300, ge=1, le=3600)
    fingerprint: str = ""

    @model_validator(mode="after")
    def set_fingerprint(self) -> "JobPolicy":
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Job Policy fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self


class ManagedJob(ContractModel):
    job_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    planned_by: str = Field(min_length=1, max_length=240)
    kind: str = Field(min_length=1, max_length=160)
    payload: dict[str, Any] = Field(default_factory=dict)
    revision: int = Field(default=1, ge=1)
    status: Literal["planned", "authorized", "running", "succeeded", "failed", "cancelled"] = "planned"
    cursor: str | None = None
    attempts: int = Field(default=0, ge=0)
    retryable: bool = False
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class JobStepResult(ContractModel):
    done: bool
    cursor: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class JobOperationReceipt(ContractModel):
    operation_id: str = Field(min_length=1, max_length=240)
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: str = Field(min_length=1, max_length=120)
    job_id: str = Field(min_length=1, max_length=240)
    committed_at: datetime


class JobOutboxEvent(ContractModel):
    sequence: int = Field(ge=1)
    event_id: str = Field(min_length=1, max_length=260)
    job_id: str = Field(min_length=1, max_length=240)
    kind: str = Field(min_length=1, max_length=160)
    payload: dict[str, Any] = Field(default_factory=dict)
    acknowledged: bool = False
    created_at: datetime


class JobPage(ContractModel):
    items: tuple[ManagedJob, ...]
