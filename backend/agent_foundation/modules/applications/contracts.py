from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from agent_foundation.kernel.primitives import ContractModel, ResourceRef, ScopeRef, stable_fingerprint


class ApplicationPolicy(ContractModel):
    policy_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    fingerprint: str = ""

    @model_validator(mode="after")
    def set_fingerprint(self) -> "ApplicationPolicy":
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Application Policy fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self


class AgentTurn(ContractModel):
    turn_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    principal_id: str = Field(min_length=1, max_length=240)
    application_id: str = Field(min_length=1, max_length=160)
    revision: int = Field(default=1, ge=1)
    state: Literal["prepared", "observed", "completed"] = "prepared"
    context_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_refs: tuple[ResourceRef, ...] = ()
    memory_id: str | None = Field(default=None, max_length=240)
    memory_revision: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TurnOperationReceipt(ContractModel):
    operation_id: str = Field(min_length=1, max_length=240)
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: str = Field(min_length=1, max_length=120)
    turn_id: str = Field(min_length=1, max_length=240)
    committed_at: datetime
