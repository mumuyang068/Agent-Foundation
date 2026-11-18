from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest

from agent_foundation.adapters.outbound.access_state import PostgresAccessStateRepository
from agent_foundation.adapters.outbound.projection_v2 import (
    Neo4jResourceProjection,
    ProjectedResource,
    QdrantResourceProjection,
)
from agent_foundation.adapters.outbound.neo4j_graph_v2 import Neo4jGraphWriteAuthority
from agent_foundation.algorithms.vector_v2 import DeterministicHashVectorizer
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef, ScopeRef
from agent_foundation.modules.graph.contracts import GraphEdge, GraphNode, GraphOperation
from agent_foundation.modules.access.domain import AccessState


POSTGRES_DSN = os.environ.get("AF_PL09_POSTGRES_DSN")
NEO4J_URI = os.environ.get("AF_PL09_NEO4J_URI")
QDRANT_URL = os.environ.get("AF_PL09_QDRANT_URL")


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {"result": {}}

    def json(self):
        return self._payload


class _FakeQdrantClient:
    def __init__(self) -> None:
        self.collection = None
        self.points: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict]] = []
        self.closed = False

    def put(self, path: str, *, json: dict):
        self.collection = (path, json)
        return _FakeResponse()

    @staticmethod
    def _matches(point: dict, filters: list[dict]) -> bool:
        payload = point["payload"]
        for condition in filters:
            expected = condition["match"]
            actual = payload.get(condition["key"])
            if "value" in expected and actual != expected["value"]:
                return False
            if "any" in expected and actual not in expected["any"]:
                return False
        return True

    def request(self, method: str, path: str, *, json: dict):
        self.requests.append((method, path, json))
        if path.endswith("/points/delete?wait=true"):
            filters = json["filter"]["must"]
            self.points = {
                point_id: point
                for point_id, point in self.points.items()
                if not self._matches(point, filters)
            }
            return _FakeResponse()
        if path.endswith("/points?wait=true"):
            self.points.update({point["id"]: point for point in json["points"]})
            return _FakeResponse()
        filters = json["filter"]["must"]
        selected = [
            point for point in self.points.values() if self._matches(point, filters)
        ]
        if path.endswith("/points/query"):
            query = json["query"]
            selected.sort(
                key=lambda point: -sum(
                    left * right for left, right in zip(query, point["vector"], strict=True)
                )
            )
        return _FakeResponse(
            payload={"result": {"points": selected[: json["limit"]]}}
        )

    def close(self) -> None:
        self.closed = True


def test_postgres_access_authority_persists_and_rejects_competing_cas():
    psycopg = pytest.importorskip("psycopg")
    if not POSTGRES_DSN:
        pytest.skip("AF_PL09_POSTGRES_DSN not configured")
    first = PostgresAccessStateRepository(psycopg.connect(POSTGRES_DSN))
    second = PostgresAccessStateRepository(psycopg.connect(POSTGRES_DSN))
    try:
        initial = first.read()
        proposed = AccessState()

        def update(repository):
            try:
                return repository.compare_and_swap(initial.revision, proposed).revision
            except FoundationError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(update, (first, second)))
        assert set(str(value) for value in outcomes) == {
            "access_state_conflict", str(initial.revision + 1)
        }
        revision = first.read().revision
    finally:
        first.close()
        second.close()

    reopened = PostgresAccessStateRepository(psycopg.connect(POSTGRES_DSN))
    try:
        assert reopened.read().revision == revision
    finally:
        reopened.close()


def _resources(scope: ScopeRef, generation: str):
    return (
        ProjectedResource(
            ref=ResourceRef(
                domain="graph",
                scope=scope,
                resource_type="graph_node",
                resource_id=f"node-{generation}",
                revision="1",
            ),
            text=f"candidate text {generation}",
        ),
    )


def _exercise_projection(projection):
    scope = ScopeRef(tenant_id="tenant-a", space_id="space-a")
    other = ScopeRef(tenant_id="tenant-b", space_id="space-a")
    assert projection.replace_generation(
        scope, domain="graph", generation_id="g1", resources=_resources(scope, "g1")
    ) == 1
    assert projection.replace_generation(
        other, domain="graph", generation_id="g1", resources=_resources(other, "other")
    ) == 1
    projection.activate_generation(scope, domain="graph", generation_id="g1")
    assert [
        ref.resource_id
        for ref in projection.candidates(
            scope, domain="graph", query="candidate", limit=10
        )
    ] == ["node-g1"]
    assert projection.candidates(
        other, domain="graph", query="candidate", limit=10
    ) == ()
    assert projection.replace_generation(
        scope, domain="graph", generation_id="g2", resources=_resources(scope, "g2")
    ) == 1
    assert [
        ref.resource_id
        for ref in projection.candidates(
            scope, domain="graph", query="candidate", limit=10
        )
    ] == ["node-g1"]
    projection.activate_generation(scope, domain="graph", generation_id="g2")
    assert [
        ref.resource_id
        for ref in projection.candidates(
            scope, domain="graph", query="candidate", limit=10
        )
    ] == ["node-g2"]


def test_neo4j_projection_enforces_scope_and_generation_switch():
    neo4j = pytest.importorskip("neo4j")
    if not NEO4J_URI:
        pytest.skip("AF_PL09_NEO4J_URI not configured")
    projection = Neo4jResourceProjection(
        neo4j.GraphDatabase.driver(NEO4J_URI, auth=("neo4j", "af_pl09_local"))
    )
    try:
        _exercise_projection(projection)
    finally:
        projection.close()

    authority = Neo4jGraphWriteAuthority(
        neo4j.GraphDatabase.driver(NEO4J_URI, auth=("neo4j", "af_pl09_local"))
    )
    authority_scope = ScopeRef(
        tenant_id="pl14-neo4j-authority", space_id=str(uuid4())
    )
    source = ResourceRef(
        domain="evidence",
        scope=authority_scope,
        resource_type="evidence_revision",
        resource_id="source",
        revision="1",
    )
    operations = (
        GraphOperation(action="put_node", node=GraphNode(
            node_id="a", scope=authority_scope, node_type="fact", label="A",
            source_refs=(source,),
        )),
        GraphOperation(action="put_node", node=GraphNode(
            node_id="b", scope=authority_scope, node_type="fact", label="B",
            source_refs=(source,),
        )),
        GraphOperation(action="put_edge", edge=GraphEdge(
            edge_id="a-b", scope=authority_scope, relation_type="related",
            from_node_id="a", to_node_id="b", source_refs=(source,),
        )),
    )
    try:
        receipt = authority.apply_operations(
            authority_scope,
            operations,
            operation_id="create",
            command_digest="d" * 64,
        )
        assert authority.apply_operations(
            authority_scope,
            operations,
            operation_id="create",
            command_digest="d" * 64,
        ) == receipt
        assert {node.node_id for node in authority.list_nodes(
            authority_scope, limit=8
        )} == {"a", "b"}
        assert authority.get_edge(authority_scope, "a-b").revision == 1
        assert authority.authority_watermark(authority_scope) == 1
    finally:
        authority.close()


def test_qdrant_projection_enforces_scope_and_generation_switch():
    if not QDRANT_URL:
        pytest.skip("AF_PL09_QDRANT_URL not configured")
    projection = QdrantResourceProjection(
        httpx.Client(base_url=QDRANT_URL, timeout=20, trust_env=False),
        collection="af_pl09_graph_v2",
        vectorizer=DeterministicHashVectorizer(),
    )
    try:
        _exercise_projection(projection)
    finally:
        projection.close()


def test_qdrant_projection_uses_content_and_query_vectors_before_limit():
    client = _FakeQdrantClient()
    vectorizer = DeterministicHashVectorizer(dimensions=64)
    projection = QdrantResourceProjection(
        client, collection="candidate-test", vectorizer=vectorizer
    )
    scope = ScopeRef(tenant_id="tenant", space_id="space")
    resources = (
        ProjectedResource(
            ref=ResourceRef(
                domain="evidence",
                scope=scope,
                resource_type="evidence_revision",
                resource_id="weather",
                revision="1",
            ),
            text="rain clouds weather forecast",
        ),
        ProjectedResource(
            ref=ResourceRef(
                domain="evidence",
                scope=scope,
                resource_type="evidence_revision",
                resource_id="citrus",
                revision="1",
            ),
            text="orange citrus fruit nutrition",
        ),
    )
    assert projection.replace_generation(
        scope,
        domain="evidence",
        generation_id="g1",
        resources=resources,
    ) == 2
    stored_vectors = [
        point["vector"]
        for point in client.points.values()
        if point["payload"]["generation_id"] == "g1"
    ]
    assert stored_vectors[0] != stored_vectors[1]
    projection.activate_generation(scope, domain="evidence", generation_id="g1")

    ranked = projection.candidates(
        scope, domain="evidence", query="orange citrus", limit=1
    )
    assert [ref.resource_id for ref in ranked] == ["citrus"]
    exact = projection.candidates(
        scope,
        domain="evidence",
        query="orange citrus",
        limit=1,
        resource_ids=("weather",),
    )
    assert [ref.resource_id for ref in exact] == ["weather"]

    query_requests = [
        request for request in client.requests if request[1].endswith("/points/query")
    ]
    assert len(query_requests) == 2
    assert query_requests[0][2]["query"] == list(vectorizer.vectorize("orange citrus"))
    assert query_requests[1][2]["filter"]["must"][-1] == {
        "key": "resource_id",
        "match": {"any": ["weather"]},
    }
    projection.close()
    assert client.closed is True
