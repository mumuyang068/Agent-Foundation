from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import FastAPI, Header
from pydantic import Field

from agent_foundation.kernel.primitives import ResourceRef
from agent_foundation.modules.graph.contracts import GraphEdge, GraphOperation, GraphSchema
from agent_foundation.modules.graph.ports import GraphService

from .access_http import RequestModel
from .memory_http import bearer


class ProposeRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    operations: tuple[GraphOperation, ...] = Field(min_length=1, max_length=500)
    evidence_refs: tuple[ResourceRef, ...] = Field(min_length=1)
    schema_id: str | None = Field(default=None, max_length=160)
    schema_version: int | None = Field(default=None, ge=1)


class ReviewRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    expected_revision: int = Field(ge=1)
    approve: bool


class ApplyRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    expected_revision: int = Field(ge=1)


class QueryRequest(RequestModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1)


class PublishSchemaRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    schema: GraphSchema


class EntityNormalizationRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    node_ids: tuple[str, ...] = Field(min_length=2)
    evidence_refs: tuple[ResourceRef, ...] = Field(min_length=1)


class TemporalRelationRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    edge: GraphEdge
    valid_from: datetime
    valid_until: datetime | None = None
    cardinality: Literal["many", "single"] = "many"
    audit_reason: str | None = Field(default=None, max_length=2_000)
    evidence_refs: tuple[ResourceRef, ...] = Field(min_length=1)


class TemporalInvalidationRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    evidence_refs: tuple[ResourceRef, ...] = Field(min_length=1)
    audit_reason: str = Field(min_length=1, max_length=2_000)


class TemporalSupersessionRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    successor: GraphEdge
    valid_from: datetime
    valid_until: datetime | None = None
    evidence_refs: tuple[ResourceRef, ...] = Field(min_length=1)
    audit_reason: str = Field(min_length=1, max_length=2_000)


class TemporalQueryRequest(RequestModel):
    query: str = Field(min_length=1)
    at_time: datetime | None = None
    max_hops: Literal[1, 2] = 1
    max_items: int = Field(default=8, ge=1, le=16)


class RelationReinforcementRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    edge_id: str = Field(min_length=1, max_length=240)
    value: float = Field(ge=-1, le=1)
    evidence_refs: tuple[ResourceRef, ...] = Field(min_length=1)


class RelationEvolutionRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    edge_id: str = Field(min_length=1, max_length=240)
    relation_type: str = Field(min_length=1, max_length=120)
    evidence_refs: tuple[ResourceRef, ...] = Field(min_length=1)


class CommunityTopologyRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    evidence_refs: tuple[ResourceRef, ...] = Field(min_length=1)


def add_graph_routes(app: FastAPI, graph: GraphService) -> None:
    @app.post("/api/foundation/v1/graph/proposals")
    def propose(payload: ProposeRequest, authorization: str | None = Header(default=None)):
        return graph.propose(
            bearer(authorization),
            operations=payload.operations,
            evidence_refs=payload.evidence_refs,
            schema_id=payload.schema_id,
            schema_version=payload.schema_version,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/graph/proposals/{proposal_id}")
    def get_proposal(proposal_id: str, authorization: str | None = Header(default=None)):
        return graph.get_proposal(bearer(authorization), proposal_id)

    @app.get("/api/foundation/v1/graph/proposals")
    def list_proposals(
        states: str = "pending",
        limit: int = 8,
        authorization: str | None = Header(default=None),
    ):
        return graph.list_proposals(
            bearer(authorization),
            states=tuple(item.strip() for item in states.split(",") if item.strip()),
            limit=limit,
        )

    @app.post("/api/foundation/v1/graph/proposals/{proposal_id}/review")
    def review(
        proposal_id: str,
        payload: ReviewRequest,
        authorization: str | None = Header(default=None),
    ):
        return graph.review(
            bearer(authorization),
            proposal_id,
            approve=payload.approve,
            expected_revision=payload.expected_revision,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/graph/proposals/{proposal_id}/apply")
    def apply(
        proposal_id: str,
        payload: ApplyRequest,
        authorization: str | None = Header(default=None),
    ):
        return graph.apply(
            bearer(authorization),
            proposal_id,
            expected_revision=payload.expected_revision,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/graph/nodes/{node_id}")
    def get_node(node_id: str, authorization: str | None = Header(default=None)):
        return graph.get_node(bearer(authorization), node_id)

    @app.get("/api/foundation/v1/graph/nodes")
    def list_nodes(limit: int = 8, authorization: str | None = Header(default=None)):
        return graph.list_nodes(bearer(authorization), limit=limit)

    @app.get("/api/foundation/v1/graph/nodes/{node_id}/revisions/{revision}")
    def get_node_revision(
        node_id: str, revision: int, authorization: str | None = Header(default=None)
    ):
        return graph.get_node_revision(bearer(authorization), node_id, revision)

    @app.get("/api/foundation/v1/graph/nodes/{node_id}/neighborhood")
    def neighborhood(node_id: str, authorization: str | None = Header(default=None)):
        return graph.neighborhood(bearer(authorization), node_id)

    @app.get("/api/foundation/v1/graph/edges/{edge_id}")
    def get_edge(edge_id: str, authorization: str | None = Header(default=None)):
        return graph.get_edge(bearer(authorization), edge_id)

    @app.get("/api/foundation/v1/graph/edges/{edge_id}/revisions/{revision}")
    def get_edge_revision(
        edge_id: str, revision: int, authorization: str | None = Header(default=None)
    ):
        return graph.get_edge_revision(bearer(authorization), edge_id, revision)

    @app.get("/api/foundation/v1/graph/edges")
    def list_edges(limit: int = 8, authorization: str | None = Header(default=None)):
        return graph.list_edges(bearer(authorization), limit=limit)

    @app.post("/api/foundation/v1/graph/query")
    def query(payload: QueryRequest, authorization: str | None = Header(default=None)):
        return graph.query(bearer(authorization), query=payload.query, limit=payload.limit)

    @app.post("/api/foundation/v1/graph/temporal/query")
    def query_temporal(
        payload: TemporalQueryRequest, authorization: str | None = Header(default=None)
    ):
        return graph.query_temporal(
            bearer(authorization),
            query=payload.query,
            at_time=payload.at_time,
            max_hops=payload.max_hops,
            max_items=payload.max_items,
        )

    @app.post("/api/foundation/v1/graph/schemas")
    def publish_schema(
        payload: PublishSchemaRequest, authorization: str | None = Header(default=None)
    ):
        return graph.publish_schema(
            bearer(authorization), schema=payload.schema, operation_id=payload.operation_id
        )

    @app.get("/api/foundation/v1/graph/schemas/{schema_id}/versions/{version}")
    def get_schema(
        schema_id: str, version: int, authorization: str | None = Header(default=None)
    ):
        return graph.get_schema(bearer(authorization), schema_id, version)

    @app.get("/api/foundation/v1/graph/schemas")
    def list_schemas(
        schema_id: str | None = None,
        limit: int = 8,
        authorization: str | None = Header(default=None),
    ):
        return graph.list_schemas(
            bearer(authorization), schema_id=schema_id, limit=limit
        )

    @app.post("/api/foundation/v1/graph/proposals/entity-normalization")
    def propose_entity_normalization(
        payload: EntityNormalizationRequest, authorization: str | None = Header(default=None)
    ):
        return graph.propose_entity_normalization(
            bearer(authorization),
            node_ids=payload.node_ids,
            evidence_refs=payload.evidence_refs,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/graph/proposals/temporal-relation")
    def propose_temporal_relation(
        payload: TemporalRelationRequest, authorization: str | None = Header(default=None)
    ):
        return graph.propose_temporal_relation(
            bearer(authorization),
            edge=payload.edge,
            valid_from=payload.valid_from,
            valid_to=payload.valid_until,
            evidence_refs=payload.evidence_refs,
            operation_id=payload.operation_id,
            cardinality=payload.cardinality,
            audit_reason=payload.audit_reason,
        )

    @app.post("/api/foundation/v1/graph/temporal/{edge_id}/invalidate")
    def propose_temporal_invalidation(
        edge_id: str,
        payload: TemporalInvalidationRequest,
        authorization: str | None = Header(default=None),
    ):
        return graph.propose_temporal_invalidation(
            bearer(authorization),
            edge_id,
            evidence_refs=payload.evidence_refs,
            audit_reason=payload.audit_reason,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/graph/temporal/{edge_id}/supersede")
    def propose_temporal_supersession(
        edge_id: str,
        payload: TemporalSupersessionRequest,
        authorization: str | None = Header(default=None),
    ):
        return graph.propose_temporal_supersession(
            bearer(authorization),
            edge_id,
            successor=payload.successor,
            valid_from=payload.valid_from,
            valid_to=payload.valid_until,
            evidence_refs=payload.evidence_refs,
            audit_reason=payload.audit_reason,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/graph/proposals/relation-reinforcement")
    def propose_relation_reinforcement(
        payload: RelationReinforcementRequest,
        authorization: str | None = Header(default=None),
    ):
        return graph.propose_relation_reinforcement(
            bearer(authorization),
            edge_id=payload.edge_id,
            value=payload.value,
            evidence_refs=payload.evidence_refs,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/graph/proposals/relation-evolution")
    def propose_relation_evolution(
        payload: RelationEvolutionRequest, authorization: str | None = Header(default=None)
    ):
        return graph.propose_relation_evolution(
            bearer(authorization),
            edge_id=payload.edge_id,
            relation_type=payload.relation_type,
            evidence_refs=payload.evidence_refs,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/graph/proposals/community-topology")
    def propose_community_topology(
        payload: CommunityTopologyRequest, authorization: str | None = Header(default=None)
    ):
        return graph.propose_community_topology(
            bearer(authorization),
            evidence_refs=payload.evidence_refs,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/graph/operations/{operation_id}")
    def get_operation_receipt(
        operation_id: str, authorization: str | None = Header(default=None)
    ):
        return graph.get_operation_receipt(bearer(authorization), operation_id)
