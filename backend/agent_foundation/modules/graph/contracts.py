from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from agent_foundation.kernel.primitives import ContractModel, ResourceRef, ScopeRef, stable_fingerprint


class GraphPolicy(ContractModel):
    policy_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    require_review: bool = True
    require_schema: bool = False
    fingerprint: str = ""

    @model_validator(mode="after")
    def set_fingerprint(self) -> "GraphPolicy":
        expected = stable_fingerprint(self)
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("Graph Policy fingerprint mismatch")
        object.__setattr__(self, "fingerprint", expected)
        return self


class GraphAlgorithmIdentity(ContractModel):
    algorithm_id: str = Field(min_length=1, max_length=160)
    implementation_version: str = Field(min_length=1, max_length=120)


class GraphNode(ContractModel):
    node_id: str = Field(min_length=1, max_length=240)
    scope: ScopeRef
    revision: int = Field(default=1, ge=1)
    node_type: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=1_000)
    properties: dict[str, Any] = Field(default_factory=dict)
    source_refs: tuple[ResourceRef, ...] = Field(min_length=1)
    status: Literal["active", "deleted"] = "active"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def ref(self) -> ResourceRef:
        return ResourceRef(
            domain="graph",
            scope=self.scope,
            resource_type="graph_node",
            resource_id=self.node_id,
            revision=str(self.revision),
        )


class GraphTemporalMetadata(ContractModel):
    valid_from: datetime
    valid_until: datetime | None = None
    source_authority: Literal["owner_explicit", "verified_tool", "agent_inference"]
    lifecycle: Literal["confirmed", "invalidated", "superseded"] = "confirmed"
    cardinality: Literal["many", "single"] = "many"
    predecessor_ref: ResourceRef | None = None
    successor_ref: ResourceRef | None = None
    audit_reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_temporal_state(self) -> "GraphTemporalMetadata":
        if self.valid_from.tzinfo is None or self.valid_from.utcoffset() is None:
            raise ValueError("Temporal valid_from must be timezone aware")
        if self.valid_until is not None:
            if self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None:
                raise ValueError("Temporal valid_until must be timezone aware")
            if self.valid_until <= self.valid_from:
                raise ValueError("Temporal valid_until must be after valid_from")
        for ref in (self.predecessor_ref, self.successor_ref):
            if ref is not None and (ref.domain != "graph" or ref.resource_type != "graph_edge"):
                raise ValueError("Temporal lineage must reference a Graph edge revision")
        if self.lifecycle == "superseded" and self.successor_ref is None:
            raise ValueError("Superseded temporal relation needs a successor reference")
        return self


class GraphEdge(ContractModel):
    edge_id: str = Field(min_length=1, max_length=240)
    scope: ScopeRef
    revision: int = Field(default=1, ge=1)
    relation_type: str = Field(min_length=1, max_length=120)
    from_node_id: str = Field(min_length=1, max_length=240)
    to_node_id: str = Field(min_length=1, max_length=240)
    properties: dict[str, Any] = Field(default_factory=dict)
    temporal: GraphTemporalMetadata | None = None
    source_refs: tuple[ResourceRef, ...] = Field(min_length=1)
    status: Literal["active", "deleted"] = "active"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def ref(self) -> ResourceRef:
        return ResourceRef(
            domain="graph",
            scope=self.scope,
            resource_type="graph_edge",
            resource_id=self.edge_id,
            revision=str(self.revision),
        )


class GraphSchema(ContractModel):
    schema_id: str = Field(min_length=1, max_length=160)
    scope: ScopeRef
    version: int = Field(ge=1)
    node_types: tuple[str, ...] = Field(min_length=1)
    relation_types: tuple[str, ...] = Field(min_length=1)
    published_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GraphOperation(ContractModel):
    action: Literal["put_node", "put_edge", "delete_node", "delete_edge"]
    node: GraphNode | None = None
    edge: GraphEdge | None = None
    target_id: str | None = Field(default=None, max_length=240)
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def match_payload(self) -> "GraphOperation":
        if self.action == "put_node" and self.node is None:
            raise ValueError("put_node requires node")
        if self.action == "put_edge" and self.edge is None:
            raise ValueError("put_edge requires edge")
        if self.action.startswith("delete_") and not self.target_id:
            raise ValueError("delete operation requires target_id")
        return self


class GraphProposal(ContractModel):
    proposal_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: ScopeRef
    revision: int = Field(default=1, ge=1)
    operations: tuple[GraphOperation, ...] = Field(min_length=1, max_length=500)
    evidence_refs: tuple[ResourceRef, ...] = Field(min_length=1)
    status: Literal["pending", "approved", "rejected", "applied"] = "pending"
    proposed_by: str = Field(min_length=1, max_length=240)
    reviewed_by: str | None = Field(default=None, max_length=240)
    applied_by: str | None = Field(default=None, max_length=240)
    schema_id: str | None = None
    schema_version: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def complete_schema_ref(self) -> "GraphProposal":
        if (self.schema_id is None) != (self.schema_version is None):
            raise ValueError("Graph schema ID and version must be supplied together")
        return self


class GraphProposalPage(ContractModel):
    items: tuple[GraphProposal, ...]
    authority_watermark: int = Field(ge=0)


class GraphNodePage(ContractModel):
    items: tuple[GraphNode, ...]
    authority_watermark: int = Field(ge=0)


class GraphEdgePage(ContractModel):
    items: tuple[GraphEdge, ...]
    authority_watermark: int = Field(ge=0)


class GraphSchemaPage(ContractModel):
    items: tuple[GraphSchema, ...]
    authority_watermark: int = Field(ge=0)


class GraphQueryHit(ContractModel):
    node: GraphNode
    score: float = Field(ge=0)


class GraphExecutionRecord(ContractModel):
    request_id: str = Field(min_length=1, max_length=240)
    grant_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_refs: tuple[ResourceRef, ...] = ()
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class GraphQueryResult(ContractModel):
    hits: tuple[GraphQueryHit, ...]
    execution: GraphExecutionRecord
    algorithm: GraphAlgorithmIdentity
    authority_watermark: int = Field(ge=0)


class GraphTemporalPath(ContractModel):
    edge_refs: tuple[ResourceRef, ...] = Field(min_length=1, max_length=2)
    node_refs: tuple[ResourceRef, ...] = Field(min_length=2, max_length=3)
    score: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_path_shape(self) -> "GraphTemporalPath":
        if len(self.node_refs) != len(self.edge_refs) + 1:
            raise ValueError("Temporal path needs one more node than edge")
        return self


class GraphTemporalQueryResult(ContractModel):
    paths: tuple[GraphTemporalPath, ...]
    at_time: datetime
    max_hops: Literal[1, 2]
    max_items: int = Field(ge=1, le=16)
    execution: GraphExecutionRecord
    algorithm: GraphAlgorithmIdentity
    authority_watermark: int = Field(ge=0)


class GraphNeighborhood(ContractModel):
    center: GraphNode
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    authority_watermark: int = Field(ge=0)


class GraphOperationReceipt(ContractModel):
    operation_id: str = Field(min_length=1, max_length=240)
    command_digest: str = Field(min_length=1)
    action: str = Field(min_length=1, max_length=120)
    resource_ids: tuple[str, ...]
    committed_at: datetime
    authority_watermark: int = Field(ge=1)


class GraphApplyIntent(ContractModel):
    """Durable control-side identity for one cross-store Graph apply."""

    scope: ScopeRef
    proposal_id: str = Field(min_length=1, max_length=240)
    expected_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=240)
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    write_operation_id: str = Field(min_length=1, max_length=240)
    write_command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    applied_by: str = Field(min_length=1, max_length=240)
    grant_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
