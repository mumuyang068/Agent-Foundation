from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from agent_foundation.kernel.primitives import ContractModel, ResourceRef, ScopeRef, stable_fingerprint


class MemoryPolicy(ContractModel):
    policy_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    auto_accept: bool = False
    require_current_sources: bool = True
    max_content_chars: int = Field(default=20_000, ge=1)
    fingerprint: str = ""

    @model_validator(mode="after")
    def set_fingerprint(self) -> "MemoryPolicy":
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Memory Policy fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self


class MemoryAlgorithmIdentity(ContractModel):
    algorithm_id: str = Field(min_length=1, max_length=160)
    implementation_version: str = Field(min_length=1, max_length=120)


class MemoryEntry(ContractModel):
    memory_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    revision: int = Field(default=1, ge=1)
    content: str = Field(min_length=1, max_length=20_000)
    status: Literal["candidate", "active", "rejected", "superseded", "revoked"]
    source_refs: tuple[ResourceRef, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    successor_id: str | None = None
    feedback_total: float = 0
    feedback_count: int = Field(default=0, ge=0)

    def ref(self) -> ResourceRef:
        return ResourceRef(
            domain="memory",
            scope=self.scope,
            resource_type="memory",
            resource_id=self.memory_id,
            revision=str(self.revision),
        )


class MemoryRecallHit(ContractModel):
    entry: MemoryEntry
    score: float = Field(ge=0)


class MemoryExecutionRecord(ContractModel):
    request_id: str = Field(min_length=1, max_length=240)
    action: Literal["read"] = "read"
    grant_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_refs: tuple[ResourceRef, ...] = ()
    status: Literal["succeeded"] = "succeeded"
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class MemoryRecallResult(ContractModel):
    hits: tuple[MemoryRecallHit, ...]
    execution: MemoryExecutionRecord
    algorithm: MemoryAlgorithmIdentity
    authority_watermark: int = Field(ge=0)


class MemoryEntryPage(ContractModel):
    items: tuple[MemoryEntry, ...]
    authority_watermark: int = Field(ge=0)


class MemoryTrace(ContractModel):
    memory_id: str = Field(min_length=1, max_length=240)
    revisions: tuple[MemoryEntry, ...] = Field(min_length=1)


class MemoryActivation(ContractModel):
    memory_id: str = Field(min_length=1, max_length=240)
    revision: int = Field(ge=1)
    status: Literal["candidate", "active", "rejected", "superseded", "revoked"]
    sources_current: bool
    feedback_count: int = Field(ge=0)
    feedback_average: float = 0


class MemoryFeedbackEvent(ContractModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    memory_id: str = Field(min_length=1, max_length=240)
    revision: int = Field(default=1, ge=1)
    value: float = Field(ge=-1, le=1)
    status: Literal["active", "contested", "retracted"] = "active"
    reason: str | None = Field(default=None, max_length=2_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryFeedbackPage(ContractModel):
    items: tuple[MemoryFeedbackEvent, ...]
    authority_watermark: int = Field(ge=0)


class MemoryReinforcementDiagnostic(ContractModel):
    memory_id: str = Field(min_length=1, max_length=240)
    active_count: int = Field(ge=0)
    contested_count: int = Field(ge=0)
    retracted_count: int = Field(ge=0)
    active_total: float


class MemoryMaintenanceAction(ContractModel):
    action_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=240)
    action: Literal[
        "create", "update", "merge", "archive", "link", "revoke", "correct"
    ]
    disposition: Literal["auto_apply", "owner_decision", "plan_only", "blocked"] = (
        "owner_decision"
    )
    memory_id: str | None = Field(default=None, min_length=1, max_length=240)
    memory_ids: tuple[str, ...] = Field(default=(), max_length=8)
    expected_revision: int | None = Field(default=None, ge=1)
    expected_revisions: dict[str, int] = Field(default_factory=dict)
    replacement_content: str | None = Field(default=None, max_length=20_000)
    new_memory_id: str | None = Field(default=None, min_length=1, max_length=240)
    link_target: ResourceRef | None = None
    link_relation: str | None = Field(default=None, min_length=1, max_length=160)
    link_schema_id: str | None = Field(default=None, max_length=160)
    link_schema_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_action_shape(self) -> "MemoryMaintenanceAction":
        single = {"update", "archive", "link", "revoke", "correct"}
        if self.action in single and (self.memory_id is None or self.expected_revision is None):
            raise ValueError(f"{self.action} needs memory_id and expected_revision")
        if self.action in {"create", "update", "merge", "correct"} and not self.replacement_content:
            raise ValueError(f"{self.action} needs replacement_content")
        if self.action == "create" and self.new_memory_id is None:
            object.__setattr__(self, "new_memory_id", str(uuid4()))
        if self.action == "merge":
            normalized = tuple(sorted(set(self.memory_ids)))
            if not 2 <= len(normalized) <= 8 or len(normalized) != len(self.memory_ids):
                raise ValueError("merge needs 2..8 unique memory_ids")
            object.__setattr__(self, "memory_ids", normalized)
            if set(self.expected_revisions) != set(normalized) or any(
                revision < 1 for revision in self.expected_revisions.values()
            ):
                raise ValueError("merge expected_revisions must cover every source")
            if self.new_memory_id is None:
                object.__setattr__(self, "new_memory_id", str(uuid4()))
        if self.action == "link" and (
            self.link_target is None or self.link_relation is None
        ):
            raise ValueError("link needs link_target and link_relation")
        if (self.link_schema_id is None) != (self.link_schema_version is None):
            raise ValueError("link schema ID and version must be supplied together")
        if self.action != "merge" and (self.memory_ids or self.expected_revisions):
            raise ValueError("memory_ids and expected_revisions are merge-only")
        if self.action not in {"create", "merge"} and self.new_memory_id is not None:
            raise ValueError("new_memory_id is create/merge-only")
        if self.action != "link" and (
            self.link_target is not None or self.link_relation is not None
            or self.link_schema_id is not None or self.link_schema_version is not None
        ):
            raise ValueError("link fields are link-only")
        return self


class MemoryMaintenanceActionReceipt(ContractModel):
    action_id: str = Field(min_length=1, max_length=240)
    operation_id: str = Field(min_length=1, max_length=240)
    status: Literal["applied", "graph_pending", "blocked"]
    resource_refs: tuple[ResourceRef, ...] = ()
    detail: str | None = Field(default=None, max_length=500)


class MemoryDreamingWindow(ContractModel):
    window_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    source_refs: tuple[ResourceRef, ...] = Field(min_length=2, max_length=8)
    basis_watermark: int = Field(ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryDreamingWindowPage(ContractModel):
    items: tuple[MemoryDreamingWindow, ...]
    authority_watermark: int = Field(ge=0)


class MemoryMaintenancePlan(ContractModel):
    plan_id: str = Field(default_factory=lambda: str(uuid4()))
    kind: Literal[
        "capacity_forgetting",
        "reflective_rewrite",
        "dreaming_v1",
        "normalization",
        "revision_set",
    ]
    actions: tuple[MemoryMaintenanceAction, ...]
    basis_watermark: int = Field(ge=0)
    rationale: str | None = Field(default=None, max_length=2_000)
    input_window_id: str | None = Field(default=None, max_length=240)
    planner_id: str | None = Field(default=None, max_length=240)
    planner_output: dict[str, Any] | None = None
    draft: dict[str, Any] | None = None


class MemoryMaintenancePlanRecord(ContractModel):
    scope: ScopeRef
    plan: MemoryMaintenancePlan
    revision: int = Field(default=1, ge=1)
    state: Literal["planned", "graph_pending", "applied", "rolled_back", "blocked"] = "planned"
    action_receipts: tuple[MemoryMaintenanceActionReceipt, ...] = ()
    applied_operation_id: str | None = Field(default=None, max_length=240)
    rollback_operation_id: str | None = Field(default=None, max_length=240)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryMaintenancePlanPage(ContractModel):
    items: tuple[MemoryMaintenancePlanRecord, ...]
    authority_watermark: int = Field(ge=0)


class MemoryFormationStatus(ContractModel):
    candidates: int = Field(ge=0)
    active: int = Field(ge=0)
    rejected: int = Field(ge=0)
    authority_watermark: int = Field(ge=0)


class MemoryChangeSet(ContractModel):
    entries: tuple[MemoryEntry, ...]
    action_receipts: tuple[MemoryMaintenanceActionReceipt, ...] = ()


class MemoryOperationReceipt(ContractModel):
    operation_id: str = Field(min_length=1, max_length=240)
    command_digest: str = Field(min_length=1)
    action: str = Field(min_length=1, max_length=120)
    resource_ids: tuple[str, ...]
    committed_at: datetime
    authority_watermark: int = Field(ge=1)
