from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from agent_foundation.kernel.primitives import ContractModel, ResourceRef, ScopeRef, stable_fingerprint


class SourceRange(ContractModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> "SourceRange":
        if self.end <= self.start:
            raise ValueError("Source range end must follow start")
        return self


class EvidencePolicy(ContractModel):
    policy_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    max_content_chars: int = Field(default=100_000, ge=1)
    require_current_supports: bool = True
    fingerprint: str = ""

    @model_validator(mode="after")
    def set_fingerprint(self) -> "EvidencePolicy":
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Evidence Policy fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self


class EvidenceAlgorithmIdentity(ContractModel):
    algorithm_id: str = Field(min_length=1, max_length=160)
    implementation_version: str = Field(min_length=1, max_length=120)


class EvidenceRevision(ContractModel):
    revision_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    source_id: str = Field(min_length=1, max_length=240)
    source_version: int = Field(ge=1)
    content: str = Field(min_length=1)
    source_range: SourceRange | None = None
    status: Literal["active", "superseded", "revoked"] = "active"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    validated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def ref(self) -> ResourceRef:
        return ResourceRef(
            domain="evidence",
            scope=self.scope,
            resource_type="evidence_revision",
            resource_id=self.revision_id,
            revision=self.revision_id,
        )


class EvidenceSupport(ContractModel):
    support_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    target: ResourceRef
    evidence_refs: tuple[ResourceRef, ...] = Field(min_length=1)
    status: Literal["active", "revoked"] = "active"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvidenceHit(ContractModel):
    revision: EvidenceRevision
    score: float


class EvidenceSearchResult(ContractModel):
    hits: tuple[EvidenceHit, ...]
    policy_fingerprint: str
    execution_profile_fingerprint: str
    algorithm: EvidenceAlgorithmIdentity
    authority_watermark: int = Field(ge=0)


class EvidenceRevisionPage(ContractModel):
    items: tuple[EvidenceRevision, ...]
    next_cursor: str | None = None
    authority_watermark: int = Field(ge=0)


class EvidenceOperationReceipt(ContractModel):
    operation_id: str = Field(min_length=1, max_length=240)
    command_digest: str = Field(min_length=1)
    action: str = Field(min_length=1, max_length=120)
    result_kind: Literal["revision", "support"]
    resource_id: str = Field(min_length=1, max_length=240)
    committed_at: datetime
    authority_watermark: int = Field(ge=1)


class EvidenceRetrievalStatus(ContractModel):
    state: Literal["ready"] = "ready"
    algorithm: EvidenceAlgorithmIdentity
    policy_fingerprint: str
    execution_profile_fingerprint: str
    authority_watermark: int = Field(ge=0)
