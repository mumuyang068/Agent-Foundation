from __future__ import annotations

from typing import Protocol

from agent_foundation.kernel.primitives import ResourceRef, ScopeRef

from .contracts import (
    GraphApplyIntent,
    GraphAlgorithmIdentity,
    GraphEdge,
    GraphNode,
    GraphOperation,
    GraphOperationReceipt,
    GraphProposal,
    GraphQueryResult,
    GraphSchema,
    GraphTemporalPath,
    GraphTemporalQueryResult,
)


class GraphResourceCurrentness(Protocol):
    def is_current(self, ref: ResourceRef) -> bool: ...


class GraphCandidateAlgorithm(Protocol):
    descriptor: GraphAlgorithmIdentity

    def rank(
        self, nodes: tuple[GraphNode, ...], *, query: str, limit: int
    ) -> tuple[tuple[str, float], ...]: ...


class GraphTemporalAlgorithm(Protocol):
    descriptor: GraphAlgorithmIdentity

    def traverse(
        self,
        nodes: tuple[GraphNode, ...],
        edges: tuple[GraphEdge, ...],
        *,
        query: str,
        max_hops: int,
        max_items: int,
    ) -> tuple[GraphTemporalPath, ...]: ...


class GraphProposalStore(Protocol):
    def save_proposal(
        self, proposal: GraphProposal, *, operation_id: str, command_digest: str
    ) -> GraphProposal: ...

    def get_proposal(self, scope: ScopeRef, proposal_id: str) -> GraphProposal | None: ...

    def list_proposals(
        self, scope: ScopeRef, *, states: tuple[str, ...], limit: int
    ) -> tuple[GraphProposal, ...]: ...

    def review_proposal(
        self,
        proposal: GraphProposal,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
    ) -> GraphProposal: ...

    def prepare_apply(
        self,
        scope: ScopeRef,
        proposal_id: str,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
        write_operation_id: str,
        write_command_digest: str,
        applied_by: str,
        grant_fingerprint: str,
        policy_fingerprint: str,
    ) -> GraphApplyIntent: ...

    def get_apply_intent(
        self, scope: ScopeRef, proposal_id: str
    ) -> GraphApplyIntent | None: ...

    def complete_apply(
        self,
        scope: ScopeRef,
        proposal_id: str,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
        write_receipt: GraphOperationReceipt,
    ) -> GraphProposal: ...

    def get_operation_receipt(
        self, scope: ScopeRef, operation_id: str
    ) -> GraphOperationReceipt | None: ...


class GraphAuthorityReader(Protocol):

    def get_node(self, scope: ScopeRef, node_id: str) -> GraphNode | None: ...

    def list_nodes(self, scope: ScopeRef, *, limit: int) -> tuple[GraphNode, ...]: ...

    def get_node_revision(
        self, scope: ScopeRef, node_id: str, revision: int
    ) -> GraphNode | None: ...

    def get_edge(self, scope: ScopeRef, edge_id: str) -> GraphEdge | None: ...

    def list_edges(self, scope: ScopeRef, *, limit: int) -> tuple[GraphEdge, ...]: ...

    def get_edge_revision(
        self, scope: ScopeRef, edge_id: str, revision: int
    ) -> GraphEdge | None: ...

    def incident_edges(self, scope: ScopeRef, node_id: str) -> tuple[GraphEdge, ...]: ...

    def authority_watermark(self, scope: ScopeRef) -> int: ...


class GraphWriteAuthority(GraphAuthorityReader, Protocol):
    def apply_operations(
        self,
        scope: ScopeRef,
        operations: tuple[GraphOperation, ...],
        *,
        operation_id: str,
        command_digest: str,
    ) -> GraphOperationReceipt: ...

    def get_operation_receipt(
        self, scope: ScopeRef, operation_id: str
    ) -> GraphOperationReceipt | None: ...

    def close(self) -> None: ...


class GraphCandidateSource(Protocol):

    def candidates(
        self, scope: ScopeRef, *, query: str, limit: int
    ) -> tuple[ResourceRef, ...]: ...


class GraphSchemaStore(Protocol):

    def publish_schema(
        self, schema: GraphSchema, *, operation_id: str, command_digest: str
    ) -> GraphSchema: ...

    def get_schema(self, scope: ScopeRef, schema_id: str, version: int) -> GraphSchema | None: ...

    def list_schemas(
        self, scope: ScopeRef, *, schema_id: str | None, limit: int
    ) -> tuple[GraphSchema, ...]: ...


class GraphControlAuthority(
    GraphProposalStore,
    GraphSchemaStore,
    Protocol,
):
    def close(self) -> None: ...


class GraphAuthority(
    GraphControlAuthority,
    GraphWriteAuthority,
    Protocol,
): ...


class GraphService(Protocol):
    def propose(self, token: str, **kwargs) -> GraphProposal: ...

    def review(self, token: str, proposal_id: str, **kwargs) -> GraphProposal: ...

    def apply(self, token: str, proposal_id: str, **kwargs) -> GraphProposal: ...

    def get_proposal(self, token: str, proposal_id: str) -> GraphProposal: ...

    def get_node(self, token: str, node_id: str) -> GraphNode: ...

    def get_node_revision(self, token: str, node_id: str, revision: int) -> GraphNode: ...

    def get_edge(self, token: str, edge_id: str) -> GraphEdge: ...

    def get_edge_revision(self, token: str, edge_id: str, revision: int) -> GraphEdge: ...

    def neighborhood(self, token: str, node_id: str): ...

    def query(self, token: str, **kwargs) -> GraphQueryResult: ...

    def query_temporal(self, token: str, **kwargs) -> GraphTemporalQueryResult: ...

    def publish_schema(self, token: str, **kwargs) -> GraphSchema: ...

    def get_schema(self, token: str, schema_id: str, version: int) -> GraphSchema: ...

    def list_schemas(self, token: str, **kwargs): ...

    def get_operation_receipt(self, token: str, operation_id: str) -> GraphOperationReceipt: ...
