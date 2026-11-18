from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from agent_foundation.kernel.primitives import (
    ContractModel,
    DomainName,
    ResourceRef,
    ScopeRef,
    stable_fingerprint,
)


ReleaseKind = Literal[
    "domain_policy", "execution_profile", "extractor", "provider",
    "host_configuration", "graph_schema",
]


class ManagementPolicy(ContractModel):
    policy_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    require_distinct_approver: bool = True
    fingerprint: str = ""

    @model_validator(mode="after")
    def set_fingerprint(self) -> "ManagementPolicy":
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Management Policy fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self


class ManagedRelease(ContractModel):
    release_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    kind: ReleaseKind
    artifact_id: str = Field(min_length=1, max_length=240)
    version: int = Field(ge=1)
    payload: dict[str, Any]
    content_digest: str = ""
    status: Literal["staged", "approved", "active", "failed", "retired", "cancelled"] = "staged"
    revision: int = Field(default=1, ge=1)
    staged_by: str = Field(min_length=1, max_length=240)
    approved_by: str | None = Field(default=None, max_length=240)
    activated_by: str | None = Field(default=None, max_length=240)
    effect_receipt: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=160)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def set_digest(self) -> "ManagedRelease":
        expected = stable_fingerprint(self.payload)
        if self.content_digest and self.content_digest != expected:
            raise ValueError("Release content digest mismatch")
        object.__setattr__(self, "content_digest", expected)
        return self


class ExtractorFixture(ContractModel):
    fixture_id: str = Field(min_length=1, max_length=160)
    input_payload: dict[str, Any]
    expected_status: Literal["candidate", "no_candidate"]
    expected_value: Any = None

    @model_validator(mode="after")
    def validate_expected_value(self) -> "ExtractorFixture":
        if self.expected_status == "no_candidate" and self.expected_value is not None:
            raise ValueError("no_candidate fixture cannot declare an expected value")
        if self.expected_status == "candidate" and self.expected_value is None:
            raise ValueError("candidate fixture needs an expected value")
        return self


class ExtractorFixtureResult(ContractModel):
    fixture_id: str = Field(min_length=1, max_length=160)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_status: Literal["candidate", "no_candidate"]
    observed_status: Literal["candidate", "no_candidate", "invalid"]
    expected_value_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_value_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    passed: bool


class ExtractorCertificate(ContractModel):
    certificate_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    release_id: str = Field(min_length=1, max_length=240)
    release_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_id: str = Field(min_length=1, max_length=240)
    schema_version: int = Field(ge=1)
    implementation_id: str = Field(min_length=1, max_length=240)
    status: Literal["conformant", "nonconformant"]
    fixture_results: tuple[ExtractorFixtureResult, ...] = Field(min_length=2, max_length=8)
    fixture_set_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=8, max_length=500)
    certified_by: str = Field(min_length=1, max_length=240)
    certified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    fingerprint: str = ""

    @model_validator(mode="after")
    def validate_certificate(self) -> "ExtractorCertificate":
        expected_status = (
            "conformant" if all(item.passed for item in self.fixture_results) else "nonconformant"
        )
        if self.status != expected_status:
            raise ValueError("Extractor certificate status does not match fixture results")
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Extractor certificate fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self


class ExtractorCertificateRevocation(ContractModel):
    revocation_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    certificate_id: str = Field(min_length=1, max_length=240)
    certificate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_id: str = Field(min_length=1, max_length=240)
    release_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=8, max_length=500)
    revoked_by: str = Field(min_length=1, max_length=240)
    revoked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ExtractorExecutionReceipt(ContractModel):
    event_id: str = Field(min_length=1, max_length=240)
    scope: ScopeRef
    release_id: str = Field(min_length=1, max_length=240)
    certificate_id: str = Field(min_length=1, max_length=240)
    status: Literal["no_candidate", "invalid", "proposal_created"]
    input_bytes: int = Field(ge=0)
    candidate_count: int = Field(ge=0, le=1)
    value_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    proposal_ref: ResourceRef | None = None
    execution_fingerprint: str = ""

    @model_validator(mode="after")
    def set_execution_fingerprint(self) -> "ExtractorExecutionReceipt":
        if self.status == "proposal_created" and self.proposal_ref is None:
            raise ValueError("proposal_created needs a proposal reference")
        expected = stable_fingerprint(
            self.model_dump(mode="json", exclude={"execution_fingerprint"})
        )
        if self.execution_fingerprint and self.execution_fingerprint != expected:
            raise ValueError("Extractor execution fingerprint mismatch")
        object.__setattr__(self, "execution_fingerprint", expected)
        return self


class IndexGeneration(ContractModel):
    generation_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    domain: DomainName
    execution_profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str = Field(min_length=1, max_length=240)
    artifact_path: str = Field(min_length=1, max_length=1000)
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["building", "verified", "active", "failed", "retired"] = "building"
    revision: int = Field(default=1, ge=1)
    planned_by: str = Field(min_length=1, max_length=240)
    activated_by: str | None = Field(default=None, max_length=240)
    effect_receipt: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=160)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvaluationReport(ContractModel):
    evaluation_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    subject_ref: str = Field(min_length=1, max_length=300)
    cases: int = Field(ge=1)
    passed: int = Field(ge=0)
    metrics: dict[str, float] = Field(default_factory=dict)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_by: str = Field(min_length=1, max_length=240)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TransferPlan(ContractModel):
    transfer_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    kind: Literal["handoff", "migration"]
    destination: str = Field(min_length=1, max_length=500)
    resource_refs: tuple[ResourceRef, ...] = Field(min_length=1)
    payload: dict[str, Any]
    payload_digest: str = ""
    status: Literal["proposed", "approved", "completed", "failed", "cancelled"] = "proposed"
    revision: int = Field(default=1, ge=1)
    planned_by: str = Field(min_length=1, max_length=240)
    approved_by: str | None = Field(default=None, max_length=240)
    executed_by: str | None = Field(default=None, max_length=240)
    effect_receipt: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=160)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_transfer(self) -> "TransferPlan":
        if any(ref.scope != self.scope for ref in self.resource_refs):
            raise ValueError("Transfer resource scopes must match")
        expected = stable_fingerprint(self.payload)
        if self.payload_digest and self.payload_digest != expected:
            raise ValueError("Transfer payload digest mismatch")
        object.__setattr__(self, "payload_digest", expected)
        return self


class ManagementOperationReceipt(ContractModel):
    operation_id: str = Field(min_length=1, max_length=240)
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: str = Field(min_length=1, max_length=120)
    resource_id: str = Field(min_length=1, max_length=240)
    resource_revision: int = Field(ge=1)
    committed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ManagementPage(ContractModel):
    items: tuple[dict[str, Any], ...]
