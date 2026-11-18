from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent_foundation.adapters.outbound.access_state import SqliteAccessStateRepository
from agent_foundation.adapters.outbound.sqlite_graph_v2 import SqliteGraphAuthority
from agent_foundation.adapters.outbound.projected_candidates_v2 import (
    AuthorityGraphCandidateSource,
)
from agent_foundation.algorithms.graph_v2 import LexicalGraphCandidates, RecentGraphCandidates
from agent_foundation.execution.contracts import ExecutionProfile
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import Principal, ResourceRef, ScopeRef
from agent_foundation.modules.access.application import AccessApplication
from agent_foundation.modules.access.contracts import AccessGrant
from agent_foundation.modules.graph.application import GraphApplication
from agent_foundation.modules.graph.contracts import (
    GraphEdge,
    GraphNode,
    GraphOperation,
    GraphPolicy,
    GraphSchema,
)


class MutableCurrentness:
    def __init__(self) -> None:
        self.current: set[ResourceRef] = set()

    def is_current(self, ref: ResourceRef) -> bool:
        return ref in self.current


@pytest.fixture
def graph_system(tmp_path):
    access = AccessApplication(SqliteAccessStateRepository(sqlite3.connect(
        tmp_path / "access.sqlite3", check_same_thread=False
    )))
    scope = ScopeRef(tenant_id="tenant", space_id="graph")
    for principal_id, actions in (
        ("proposer", ("propose", "review", "read", "schema_publish")),
        ("reviewer", ("review", "read")),
        ("applier", ("apply", "read")),
    ):
        access.put_grant(AccessGrant(
            grant_id=f"{principal_id}-grant",
            principal_id=principal_id,
            scope=scope,
            domains=("graph",),
            actions=actions,
            limits={"max_items": 20},
        ))
    tokens = {
        principal_id: access.issue_session(
            Principal(principal_id=principal_id, kind="service"), f"{principal_id}-grant"
        ).access_token
        for principal_id in ("proposer", "reviewer", "applier")
    }
    policy = GraphPolicy(policy_id="graph", version=1)
    profile = ExecutionProfile(
        profile_id="graph-search",
        version=1,
        domain="graph",
        algorithm_id="graph.lexical-v2",
        implementation_version="1",
        budget={"max_items": 20},
    )
    evidence = MutableCurrentness()
    database = tmp_path / "graph.sqlite3"

    def create(algorithm=None, selected_profile=None):
        authority = SqliteGraphAuthority(
            sqlite3.connect(database, check_same_thread=False)
        )
        return GraphApplication(
            authority,
            access,
            policy,
            selected_profile or profile,
            algorithm or LexicalGraphCandidates(),
            evidence,
            write_authority=authority,
            reader=authority,
            candidate_source=AuthorityGraphCandidateSource(authority),
        )

    app = create()
    try:
        yield app, create, tokens, scope, evidence
    finally:
        app.close()
        access.close()


def evidence_ref(scope: ScopeRef, name: str = "source") -> ResourceRef:
    return ResourceRef(
        domain="evidence",
        scope=scope,
        resource_type="evidence_revision",
        resource_id=name,
        revision=name,
    )


def proposal_operations(scope: ScopeRef, source: ResourceRef):
    return (
        GraphOperation(action="put_node", node=GraphNode(
            node_id="alice", scope=scope, node_type="person", label="Alice", source_refs=(source,)
        )),
        GraphOperation(action="put_node", node=GraphNode(
            node_id="bob", scope=scope, node_type="person", label="Bob", source_refs=(source,)
        )),
        GraphOperation(action="put_edge", edge=GraphEdge(
            edge_id="alice-knows-bob",
            scope=scope,
            relation_type="knows",
            from_node_id="alice",
            to_node_id="bob",
            source_refs=(source,),
        )),
    )


def apply_initial(app, tokens, scope, evidence):
    source = evidence_ref(scope)
    evidence.current.add(source)
    proposal = app.propose(
        tokens["proposer"],
        operations=proposal_operations(scope, source),
        evidence_refs=(source,),
        operation_id="initial-proposal",
    )
    reviewed = app.review(
        tokens["reviewer"],
        proposal.proposal_id,
        approve=True,
        expected_revision=proposal.revision,
        operation_id="initial-review",
    )
    applied = app.apply(
        tokens["applier"],
        proposal.proposal_id,
        expected_revision=reviewed.revision,
        operation_id="initial-apply",
    )
    assert app.apply(
        tokens["applier"],
        proposal.proposal_id,
        expected_revision=reviewed.revision,
        operation_id="initial-apply",
    ) == applied
    return source, proposal, applied


def test_proposal_review_apply_query_neighborhood_and_restart(graph_system) -> None:
    app, create, tokens, scope, evidence = graph_system
    _source, proposal, applied = apply_initial(app, tokens, scope, evidence)
    assert (proposal.status, applied.status, applied.applied_by) == ("pending", "applied", "applier")
    assert app.get_node(tokens["proposer"], "alice").revision == 1
    assert app.get_edge(tokens["proposer"], "alice-knows-bob").revision == 1
    assert app.get_edge_revision(tokens["proposer"], "alice-knows-bob", 1).relation_type == "knows"
    assert {node.node_id for node in app.list_nodes(tokens["proposer"]).items} == {"alice", "bob"}
    assert app.list_edges(tokens["proposer"]).items[0].edge_id == "alice-knows-bob"
    assert app.list_proposals(tokens["proposer"], states=("applied",)).items == (applied,)
    result = app.query(tokens["proposer"], query="Alice")
    assert result.hits[0].node.node_id == "alice"
    assert result.hits[0].node.ref().revision == "1"
    neighborhood = app.neighborhood(tokens["proposer"], "alice")
    assert [node.node_id for node in neighborhood.nodes] == ["bob"]
    app.close()
    reopened = create()
    try:
        assert reopened.get_proposal(tokens["proposer"], proposal.proposal_id) == applied
        assert reopened.get_operation_receipt(tokens["proposer"], "initial-apply").resource_ids == (
            proposal.proposal_id,
            "alice",
            "bob",
            "alice-knows-bob",
        )
    finally:
        reopened.close()


def test_review_is_separate_and_retries_are_idempotent(graph_system) -> None:
    app, _create, tokens, scope, evidence = graph_system
    source = evidence_ref(scope)
    evidence.current.add(source)
    proposal = app.propose(
        tokens["proposer"],
        operations=proposal_operations(scope, source),
        evidence_refs=(source,),
        operation_id="separation-proposal",
    )
    assert app.propose(
        tokens["proposer"],
        operations=proposal_operations(scope, source),
        evidence_refs=(source,),
        operation_id="separation-proposal",
    ) == proposal
    with pytest.raises(FoundationError) as denied:
        app.review(
            tokens["proposer"],
            proposal.proposal_id,
            approve=True,
            expected_revision=1,
            operation_id="self-review",
        )
    assert denied.value.code == "graph_review_separation_required"
    reviewed = app.review(
        tokens["reviewer"], proposal.proposal_id, approve=True,
        expected_revision=1, operation_id="separation-review"
    )
    assert app.review(
        tokens["reviewer"], proposal.proposal_id, approve=True,
        expected_revision=1, operation_id="separation-review"
    ) == reviewed


def test_evidence_revocation_filters_direct_query_and_neighborhood(graph_system) -> None:
    app, _create, tokens, scope, evidence = graph_system
    source, _proposal, _applied = apply_initial(app, tokens, scope, evidence)
    evidence.current.remove(source)
    with pytest.raises(FoundationError):
        app.get_node(tokens["proposer"], "alice")
    with pytest.raises(FoundationError):
        app.get_edge(tokens["proposer"], "alice-knows-bob")
    assert app.query(tokens["proposer"], query="Alice").hits == ()


def test_candidate_is_rechecked_after_algorithm_execution(graph_system) -> None:
    app, _create, tokens, scope, evidence = graph_system
    source, _proposal, _applied = apply_initial(app, tokens, scope, evidence)

    class RevokingCandidates(LexicalGraphCandidates):
        def rank(self, nodes, *, query, limit):
            ranked = super().rank(nodes, query=query, limit=limit)
            evidence.current.remove(source)
            return ranked

    app._algorithm = RevokingCandidates()
    assert app.query(tokens["proposer"], query="Alice").hits == ()


def test_schema_and_exact_graph_revision_are_authoritative(graph_system) -> None:
    app, _create, tokens, scope, evidence = graph_system
    source, _proposal, _applied = apply_initial(app, tokens, scope, evidence)
    schema = GraphSchema(
        schema_id="people",
        scope=scope,
        version=1,
        node_types=("person",),
        relation_types=("knows",),
    )
    assert app.publish_schema(
        tokens["proposer"], schema=schema, operation_id="publish-schema"
    ) == schema
    assert app.get_schema(tokens["proposer"], "people", 1) == schema
    assert app.list_schemas(tokens["proposer"], schema_id="people").items == (schema,)
    old_ref = app.get_node(tokens["proposer"], "alice").ref()
    updated_node = GraphNode(
        node_id="alice",
        scope=scope,
        node_type="person",
        label="Alice Updated",
        source_refs=(source,),
    )
    proposal = app.propose(
        tokens["proposer"],
        operations=(GraphOperation(action="put_node", node=updated_node, expected_revision=1),),
        evidence_refs=(source,),
        schema_id="people",
        schema_version=1,
        operation_id="update-proposal",
    )
    reviewed = app.review(
        tokens["reviewer"], proposal.proposal_id, approve=True,
        expected_revision=1, operation_id="update-review"
    )
    app.apply(
        tokens["applier"], proposal.proposal_id,
        expected_revision=reviewed.revision, operation_id="update-apply"
    )
    assert app.get_node(tokens["proposer"], "alice").revision == 2
    assert not app.is_current(old_ref)
    assert app.get_node_revision(tokens["proposer"], "alice", 1).label == "Alice"


def test_second_candidate_algorithm_replaces_query_without_authority_change(graph_system) -> None:
    app, create, tokens, scope, evidence = graph_system
    apply_initial(app, tokens, scope, evidence)
    app.close()
    profile = ExecutionProfile(
        profile_id="graph-recent",
        version=1,
        domain="graph",
        algorithm_id="graph.recent-v2",
        implementation_version="1",
        budget={"max_items": 20},
    )
    recent = create(RecentGraphCandidates(), profile)
    try:
        result = recent.query(tokens["proposer"], query="ignored", limit=2)
        assert {hit.node.node_id for hit in result.hits} == {"alice", "bob"}
    finally:
        recent.close()


def test_advanced_graph_algorithms_only_create_reviewable_proposals(graph_system) -> None:
    app, _create, tokens, scope, evidence = graph_system
    source, _proposal, _applied = apply_initial(app, tokens, scope, evidence)

    def apply(proposal, name):
        assert proposal.status == "pending"
        reviewed = app.review(
            tokens["reviewer"], proposal.proposal_id,
            approve=True, expected_revision=proposal.revision, operation_id=f"{name}-review"
        )
        return app.apply(
            tokens["applier"], proposal.proposal_id,
            expected_revision=reviewed.revision, operation_id=f"{name}-apply"
        )

    normalized = app.propose_entity_normalization(
        tokens["proposer"],
        node_ids=("bob", "alice"),
        evidence_refs=(source,),
        operation_id="normalize",
    )
    assert "canonical_node_id" not in app.get_node(tokens["proposer"], "alice").properties
    apply(normalized, "normalize")
    assert app.get_node(tokens["proposer"], "bob").properties["canonical_node_id"] == "alice"

    now = datetime.now(timezone.utc)
    temporal = app.propose_temporal_relation(
        tokens["proposer"],
        edge=GraphEdge(
            edge_id="alice-collaborates-bob",
            scope=scope,
            relation_type="collaborates",
            from_node_id="alice",
            to_node_id="bob",
            source_refs=(source,),
        ),
        valid_from=now,
        valid_to=now + timedelta(days=1),
        evidence_refs=(source,),
        operation_id="temporal",
    )
    apply(temporal, "temporal")
    stored_temporal = app.get_edge(tokens["proposer"], "alice-collaborates-bob").temporal
    assert stored_temporal is not None
    assert stored_temporal.valid_from == now
    assert stored_temporal.lifecycle == "confirmed"

    reinforced = app.propose_relation_reinforcement(
        tokens["proposer"], edge_id="alice-knows-bob", value=0.8,
        evidence_refs=(source,), operation_id="reinforce"
    )
    apply(reinforced, "reinforce")
    assert app.get_edge(tokens["proposer"], "alice-knows-bob").properties[
        "feedback_count"
    ] == 1
    evolved = app.propose_relation_evolution(
        tokens["proposer"], edge_id="alice-knows-bob", relation_type="trusts",
        evidence_refs=(source,), operation_id="evolve"
    )
    apply(evolved, "evolve")
    assert app.get_edge(tokens["proposer"], "alice-knows-bob").relation_type == "trusts"

    topology = app.propose_community_topology(
        tokens["proposer"], evidence_refs=(source,), operation_id="topology"
    )
    apply(topology, "topology")
    assert app.get_node(tokens["proposer"], "alice").properties["community_id"] == "community:alice"
    assert app.get_node(tokens["proposer"], "bob").properties["community_id"] == "community:alice"


def test_node_delete_cascades_incident_edges_in_one_apply(graph_system) -> None:
    app, _create, tokens, scope, evidence = graph_system
    source, _proposal, _applied = apply_initial(app, tokens, scope, evidence)
    proposal = app.propose(
        tokens["proposer"],
        operations=(GraphOperation(
            action="delete_node", target_id="alice", expected_revision=1
        ),),
        evidence_refs=(source,),
        operation_id="delete-proposal",
    )
    reviewed = app.review(
        tokens["reviewer"], proposal.proposal_id, approve=True,
        expected_revision=1, operation_id="delete-review"
    )
    app.apply(
        tokens["applier"], proposal.proposal_id,
        expected_revision=reviewed.revision, operation_id="delete-apply"
    )
    with pytest.raises(FoundationError):
        app.get_node(tokens["proposer"], "alice")
    with pytest.raises(FoundationError):
        app.get_edge(tokens["proposer"], "alice-knows-bob")
    receipt = app.get_operation_receipt(tokens["proposer"], "delete-apply")
    assert set(receipt.resource_ids) == {
        proposal.proposal_id,
        "alice",
        "alice-knows-bob",
    }
