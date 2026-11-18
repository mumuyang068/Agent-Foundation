from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_foundation.algorithms.graph_v2 import BoundedTemporalTraversal
from agent_foundation.host.configuration import HostConfiguration
from agent_foundation.host.minimal import create_graph_host
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import Principal, ScopeRef
from agent_foundation.modules.access.contracts import AccessGrant
from agent_foundation.modules.graph.contracts import GraphEdge, GraphNode, GraphOperation


@pytest.fixture
def temporal_system(tmp_path):
    scope = ScopeRef(tenant_id="tenant", space_id="temporal")
    configuration = HostConfiguration(
        configuration_id="temporal-graph",
        version=1,
        enabled_modules=("access", "evidence", "graph"),
        stores={
            name: {
                "store_id": name,
                "driver": "sqlite",
                "endpoint_ref": str(tmp_path / f"{name}.sqlite3"),
            }
            for name in ("access", "evidence", "graph")
        },
        module_stores={name: name for name in ("access", "evidence", "graph")},
    )
    hosts = []

    def create():
        host = create_graph_host(configuration)
        hosts.append(host)
        return host

    host = create()
    definitions = {
        "owner": ("user", ("read", "propose", "apply")),
        "agent": ("agent", ("read", "propose")),
        "reviewer": ("operator", ("read", "review")),
        "service": ("service", ("read", "admit", "revoke", "propose", "apply")),
    }
    tokens = {}
    for name, (kind, actions) in definitions.items():
        grant_id = f"grant:{name}"
        host.access.put_grant(AccessGrant(
            grant_id=grant_id,
            principal_id=name,
            scope=scope,
            domains=("evidence", "graph"),
            actions=actions,
            limits={"max_items": 16, "max_scan_items": 64},
        ))
        tokens[name] = host.access.issue_session(
            Principal(principal_id=name, kind=kind), grant_id
        ).access_token
    try:
        yield host, create, tokens, scope
    finally:
        for item in reversed(hosts):
            item.close()


def _apply(host, tokens, proposal, name):
    reviewed = host.graph.review(
        tokens["reviewer"],
        proposal.proposal_id,
        approve=True,
        expected_revision=proposal.revision,
        operation_id=f"{name}:review",
    )
    return host.graph.apply(
        tokens["service"],
        proposal.proposal_id,
        expected_revision=reviewed.revision,
        operation_id=f"{name}:apply",
    )


def _foundation_graph(host, tokens, scope):
    source = host.evidence.admit(
        tokens["service"],
        source_id="temporal-source",
        content="Alpha then Beta then Gamma",
        operation_id="source:admit",
    )
    nodes = tuple(
        GraphNode(
            node_id=node_id,
            scope=scope,
            node_type="entity",
            label=label,
            source_refs=(source.ref(),),
        )
        for node_id, label in (("a", "Alpha"), ("b", "Beta"), ("c", "Gamma"))
    )
    proposal = host.graph.propose(
        tokens["owner"],
        operations=tuple(GraphOperation(action="put_node", node=node) for node in nodes),
        evidence_refs=(source.ref(),),
        operation_id="nodes:propose",
    )
    _apply(host, tokens, proposal, "nodes")
    return source


def _temporal_proposal(host, tokens, scope, source, *, edge_id, start, end, actor="owner",
                       from_node="a", to_node="b", cardinality="many", operation_id=None):
    return host.graph.propose_temporal_relation(
        tokens[actor],
        edge=GraphEdge(
            edge_id=edge_id,
            scope=scope,
            relation_type="located_at",
            from_node_id=from_node,
            to_node_id=to_node,
            source_refs=(source.ref(),),
        ),
        valid_from=start,
        valid_to=end,
        evidence_refs=(source.ref(),),
        operation_id=operation_id or f"{edge_id}:assert",
        cardinality=cardinality,
        audit_reason="source assertion",
    )


def test_temporal_point_query_is_bounded_and_left_closed_right_open(temporal_system):
    host, _create, tokens, scope = temporal_system
    source = _foundation_graph(host, tokens, scope)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    first = _temporal_proposal(
        host, tokens, scope, source, edge_id="a-b", start=start, end=end
    )
    _apply(host, tokens, first, "a-b")
    second = _temporal_proposal(
        host, tokens, scope, source, edge_id="b-c", start=start, end=end,
        from_node="b", to_node="c",
    )
    _apply(host, tokens, second, "b-c")

    at_start = host.graph.query_temporal(
        tokens["owner"], query="Alpha", at_time=start, max_hops=2, max_items=16
    )
    assert [len(path.edge_refs) for path in at_start.paths] == [1, 2]
    assert at_start.paths[1].edge_refs == (
        host.graph.get_edge(tokens["owner"], "a-b").ref(),
        host.graph.get_edge(tokens["owner"], "b-c").ref(),
    )
    assert host.graph.query_temporal(
        tokens["owner"], query="Alpha", at_time=end, max_hops=2, max_items=16
    ).paths == ()
    with pytest.raises(FoundationError, match="timezone aware"):
        host.graph.query_temporal(
            tokens["owner"], query="Alpha", at_time=datetime(2026, 1, 1)
        )


def test_agent_inference_keeps_its_source_class_and_is_not_read_as_trusted(temporal_system):
    host, _create, tokens, scope = temporal_system
    source = _foundation_graph(host, tokens, scope)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    proposal = _temporal_proposal(
        host, tokens, scope, source, edge_id="agent-edge", start=start, end=None,
        actor="agent",
    )
    _apply(host, tokens, proposal, "agent-edge")
    stored = host.graph.get_edge(tokens["owner"], "agent-edge")
    assert stored.temporal is not None
    assert stored.temporal.source_authority == "agent_inference"
    assert host.graph.query_temporal(
        tokens["owner"], query="Alpha", at_time=start, max_items=16
    ).paths == ()


def test_supersession_is_atomic_and_single_cardinality_survives_restart(temporal_system):
    host, create, tokens, scope = temporal_system
    source = _foundation_graph(host, tokens, scope)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    original = _temporal_proposal(
        host, tokens, scope, source, edge_id="single-old", start=start, end=None,
        cardinality="single",
    )
    _apply(host, tokens, original, "single-old")
    successor = GraphEdge(
        edge_id="single-new",
        scope=scope,
        relation_type="located_at",
        from_node_id="a",
        to_node_id="c",
        source_refs=(source.ref(),),
    )
    proposed = host.graph.propose_temporal_supersession(
        tokens["owner"],
        "single-old",
        successor=successor,
        valid_from=start + timedelta(days=1),
        valid_to=None,
        evidence_refs=(source.ref(),),
        audit_reason="corrected destination",
        operation_id="single:supersede",
    )
    _apply(host, tokens, proposed, "single:supersede")
    old = host.graph.get_edge(tokens["owner"], "single-old")
    new = host.graph.get_edge(tokens["owner"], "single-new")
    assert old.temporal is not None and old.temporal.lifecycle == "superseded"
    assert old.temporal.successor_ref == new.ref()
    assert new.temporal is not None and new.temporal.predecessor_ref.resource_id == "single-old"

    conflicting = _temporal_proposal(
        host, tokens, scope, source, edge_id="single-conflict",
        start=start + timedelta(days=2), end=None, cardinality="single",
    )
    with pytest.raises(FoundationError) as conflict:
        _apply(host, tokens, conflicting, "single-conflict")
    assert conflict.value.code == "temporal_cardinality_conflict"
    with pytest.raises(FoundationError):
        host.graph.get_edge(tokens["owner"], "single-conflict")

    host.close()
    reopened = create()
    result = reopened.graph.query_temporal(
        tokens["owner"], query="Alpha", at_time=start + timedelta(days=2), max_items=16
    )
    assert [path.edge_refs[0].resource_id for path in result.paths] == ["single-new"]


def test_temporal_path_is_rechecked_after_algorithm_execution(temporal_system):
    host, create, tokens, scope = temporal_system
    source = _foundation_graph(host, tokens, scope)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    proposal = _temporal_proposal(
        host, tokens, scope, source, edge_id="stale-edge", start=start, end=None
    )
    _apply(host, tokens, proposal, "stale-edge")
    writer = create()

    class RevokingTraversal(BoundedTemporalTraversal):
        def traverse(self, *args, **kwargs):
            paths = super().traverse(*args, **kwargs)
            writer.evidence.revoke(
                tokens["service"], source.revision_id, operation_id="source:revoke-during-query"
            )
            return paths

    host.graph._temporal_algorithm = RevokingTraversal()
    result = host.graph.query_temporal(
        tokens["owner"], query="Alpha", at_time=start, max_items=16
    )
    assert result.paths == ()
    assert result.execution.diagnostics["stale_algorithm_items"] == 1
