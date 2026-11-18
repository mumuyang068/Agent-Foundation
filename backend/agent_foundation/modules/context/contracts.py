from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from agent_foundation.kernel.primitives import ContractModel, ResourceRef, ScopeRef, stable_fingerprint


class ContextPolicy(ContractModel):
    policy_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    allow_full_snapshot: bool = False
    max_items: int = Field(default=20, ge=1, le=100)
    max_chars: int = Field(default=100_000, ge=1)
    fingerprint: str = ""

    @model_validator(mode="after")
    def set_fingerprint(self) -> "ContextPolicy":
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Context Policy fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self


class ContextAlgorithmIdentity(ContractModel):
    algorithm_id: str = Field(min_length=1, max_length=160)
    implementation_version: str = Field(min_length=1, max_length=120)


class ContextCandidate(ContractModel):
    ref: ResourceRef
    content: str = Field(min_length=1, max_length=200_000)
    score: float = Field(ge=0)


class ContextItem(ContractModel):
    ref: ResourceRef
    content: str
    score: float = Field(ge=0)
    pinned: bool = False


class ContextExecutionRecord(ContractModel):
    request_id: str = Field(min_length=1, max_length=240)
    grant_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_refs: tuple[ResourceRef, ...] = ()
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class ContextAssembly(ContractModel):
    assembly_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    assembled_by: str = Field(min_length=1, max_length=240)
    query: str = Field(min_length=1, max_length=20_000)
    items: tuple[ContextItem, ...]
    model_context: str
    execution: ContextExecutionRecord
    fingerprint: str = ""

    @model_validator(mode="after")
    def set_fingerprint(self) -> "ContextAssembly":
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Context assembly fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self


class ContextSnapshot(ContractModel):
    snapshot_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    created_by: str = Field(min_length=1, max_length=240)
    source_refs: tuple[ResourceRef, ...]
    source_domains: tuple[Literal["memory", "evidence", "graph"], ...]
    retention: Literal["full", "redacted", "provenance_only"] = "provenance_only"
    assembly_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_char_counts: tuple[int, ...]
    retained_items: tuple[ContextItem, ...] = ()
    retained_model_context: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContextSnapshotPage(ContractModel):
    items: tuple[ContextSnapshot, ...]


class ContextComparison(ContractModel):
    left_snapshot_id: str
    right_snapshot_id: str
    added_refs: tuple[ResourceRef, ...]
    removed_refs: tuple[ResourceRef, ...]
    order_changed: bool
    assembly_changed: bool


class ResidentContext(ContractModel):
    scope: ScopeRef
    revision: int = Field(default=1, ge=1)
    source_refs: tuple[ResourceRef, ...]
    source_domains: tuple[Literal["memory", "evidence", "graph"], ...]
    assembly_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    replaced_by: str = Field(min_length=1, max_length=240)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContextOperationReceipt(ContractModel):
    operation_id: str = Field(min_length=1, max_length=240)
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: str = Field(min_length=1, max_length=120)
    result_kind: Literal["snapshot", "resident"]
    resource_id: str = Field(min_length=1, max_length=240)
    committed_at: datetime
