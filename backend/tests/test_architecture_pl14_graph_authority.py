from __future__ import annotations

from itertools import repeat
import sqlite3

import pytest

from agent_foundation.algorithms.graph_v2 import LexicalGraphCandidates
from agent_foundation.adapters.outbound.sqlite_jobs_v2 import SqliteJobAuthority
from agent_foundation.execution.contracts import ExecutionProfile
from agent_foundation.host.configuration import HostConfiguration
from agent_foundation.host.minimal import create_graph_host
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import Principal, ScopeRef
from agent_foundation.modules.access.contracts import AccessGrant
from agent_foundation.modules.graph.contracts import GraphEdge, GraphNode, GraphOperation
from agent_foundation.modules.jobs.application import JobsApplication
from agent_foundation.modules.jobs.contracts import JobPolicy
from agent_foundation.workflows.domain_jobs import GraphApplyWorkflow


@pytest.fixture
def system(tmp_path):
    scope = ScopeRef(tenant_id="tenant", space_id="pl14")
    configuration = HostConfiguration(
        configuration_id="graph-authority", version=1,
        enabled_modules=("access", "evidence", "graph"),
        stores={name: {"store_id": name, "driver": "sqlite", "endpoint_ref": str(tmp_path / f"{name}.sqlite3")}
                for name in ("access", "evidence", "graph")},
        module_stores={name: name for name in ("access", "evidence", "graph")},
    )
    hosts = []

    def create(scan=16):
        profile = ExecutionProfile(
            profile_id="graph-query", version=1, domain="graph",
            algorithm_id="graph.lexical-v2", implementation_version="1",
            budget={"max_items": 8, "max_scan_items": scan},
        )
        host = create_graph_host(configuration, profile=profile)
        hosts.append(host)
        return host

    host = create()
    actions = {
        "writer": ("admit", "revoke", "read", "propose", "apply"),
        "reconciler": ("read", "apply"),
        "reviewer": ("review", "read"),
        "reader": ("read",),
    }
    tokens = {}
    for principal, allowed in actions.items():
        host.access.put_grant(AccessGrant(
            grant_id=principal, principal_id=principal, scope=scope,
            domains=("evidence", "graph"), actions=allowed,
            limits={"max_items": 8, "max_scan_items": 12},
        ))
        tokens[principal] = host.access.issue_session(
            Principal(principal_id=principal, kind="service"), principal,
        ).access_token
    try:
        yield host, create, tokens, scope
    finally:
        for instance in reversed(hosts):
            instance.close()


def apply(host, tokens, operations, refs, name):
    proposal = host.graph.propose(
        tokens["writer"], operations=operations, evidence_refs=refs, operation_id=f"{name}:propose"
    )
    reviewed = host.graph.review(
        tokens["reviewer"], proposal.proposal_id, approve=True,
        expected_revision=proposal.revision, operation_id=f"{name}:review",
    )
    return host.graph.apply(
        tokens["writer"], proposal.proposal_id, expected_revision=reviewed.revision,
        operation_id=f"{name}:apply",
    )


def test_host_assembles_physically_separate_sqlite_graph_roles(tmp_path):
    control_path = tmp_path / "graph-control.sqlite3"
    write_path = tmp_path / "graph-write.sqlite3"
    configuration = HostConfiguration(
        configuration_id="graph-split-sqlite",
        version=1,
        enabled_modules=("access", "evidence", "graph"),
        stores={
            "access": {
                "store_id": "access", "driver": "sqlite",
                "endpoint_ref": str(tmp_path / "access.sqlite3"),
            },
            "evidence": {
                "store_id": "evidence", "driver": "sqlite",
                "endpoint_ref": str(tmp_path / "evidence.sqlite3"),
            },
            "graph-control": {
                "store_id": "graph-control", "driver": "sqlite",
                "endpoint_ref": str(control_path),
            },
            "graph-write": {
                "store_id": "graph-write", "driver": "sqlite",
                "endpoint_ref": str(write_path),
            },
        },
        module_stores={
            "access": "access", "evidence": "evidence", "graph": "graph-control",
        },
        module_write_stores={"graph": "graph-write"},
    )
    host = create_graph_host(configuration)
    try:
        assert not hasattr(host.graph._proposal_store, "apply_operations")
        assert not hasattr(host.graph._write_authority, "save_proposal")
    finally:
        host.close()

    with sqlite3.connect(control_path) as control:
        control_tables = {
            row[0] for row in control.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    with sqlite3.connect(write_path) as write:
        write_tables = {
            row[0] for row in write.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "af_graph_proposals" in control_tables
    assert "af_graph_nodes" not in control_tables
    assert "af_graph_nodes" in write_tables
    assert "af_graph_proposals" not in write_tables


def add_node(host, tokens, scope, name):
    revision = host.evidence.admit(
        tokens["writer"], source_id=name, content=f"needle {name}", operation_id=f"{name}:source"
    )
    node = GraphNode(node_id=name, scope=scope, node_type="person", label=f"needle {name}", source_refs=(revision.ref(),))
    apply(host, tokens, (GraphOperation(action="put_node", node=node),), node.source_refs, name)
    return host.graph.get_node(tokens["reader"], name), revision


class FailFirstApplyCompletion:
    def __init__(self, delegate):
        self._delegate = delegate
        self.failed = False

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def complete_apply(self, *args, **kwargs):
        if not self.failed:
            self.failed = True
            raise RuntimeError("proposal state unavailable after graph commit")
        return self._delegate.complete_apply(*args, **kwargs)


class RetryableFailFirstApplyCompletion(FailFirstApplyCompletion):
    def complete_apply(self, *args, **kwargs):
        if not self.failed:
            self.failed = True
            raise FoundationError(
                "proposal_store_unavailable",
                "proposal state unavailable after graph commit",
                retryable=True,
            )
        return self._delegate.complete_apply(*args, **kwargs)


def test_graph_write_receipt_allows_apply_reconciliation_without_duplicate_revision(system):
    host, _create, tokens, scope = system
    source = host.evidence.admit(
        tokens["writer"],
        source_id="recoverable",
        content="recoverable graph write",
        operation_id="recoverable:source",
    )
    node = GraphNode(
        node_id="recoverable",
        scope=scope,
        node_type="fact",
        label="recoverable",
        source_refs=(source.ref(),),
    )
    proposal = host.graph.propose(
        tokens["writer"],
        operations=(GraphOperation(action="put_node", node=node),),
        evidence_refs=node.source_refs,
        operation_id="recoverable:propose",
    )
    reviewed = host.graph.review(
        tokens["reviewer"],
        proposal.proposal_id,
        approve=True,
        expected_revision=proposal.revision,
        operation_id="recoverable:review",
    )
    proposal_store = host.graph._proposal_store
    host.graph._proposal_store = FailFirstApplyCompletion(proposal_store)

    with pytest.raises(RuntimeError, match="proposal state unavailable"):
        host.graph.apply(
            tokens["writer"],
            proposal.proposal_id,
            expected_revision=reviewed.revision,
            operation_id="recoverable:apply",
        )

    assert proposal_store.get_proposal(scope, proposal.proposal_id).status == "approved"
    intent = proposal_store.get_apply_intent(scope, proposal.proposal_id)
    assert intent is not None
    assert host.graph._write_authority.get_operation_receipt(
        scope, intent.write_operation_id
    ).resource_ids == ("recoverable",)
    assert host.graph.get_node(tokens["reader"], "recoverable").revision == 1
    with pytest.raises(FoundationError) as competing:
        host.graph.apply(
            tokens["writer"],
            proposal.proposal_id,
            expected_revision=reviewed.revision,
            operation_id="recoverable:competing-apply",
        )
    assert competing.value.code == "graph_apply_intent_conflict"

    applied = host.graph.apply(
        tokens["writer"],
        proposal.proposal_id,
        expected_revision=reviewed.revision,
        operation_id="recoverable:apply",
    )
    assert applied.status == "applied"
    assert host.graph.get_node_revision(tokens["reader"], "recoverable", 1).revision == 1
    with pytest.raises(FoundationError) as absent_revision:
        host.graph.get_node_revision(tokens["reader"], "recoverable", 2)
    assert absent_revision.value.code == "graph_node_not_found"


def test_committed_graph_write_reconciles_after_source_and_original_grant_revocation(system):
    host, _create, tokens, scope = system
    source = host.evidence.admit(
        tokens["writer"], source_id="revoked-after-write", content="durable write",
        operation_id="revoked-after-write:source",
    )
    node = GraphNode(
        node_id="revoked-after-write", scope=scope, node_type="fact", label="durable",
        source_refs=(source.ref(),),
    )
    proposal = host.graph.propose(
        tokens["writer"], operations=(GraphOperation(action="put_node", node=node),),
        evidence_refs=node.source_refs, operation_id="revoked-after-write:propose",
    )
    reviewed = host.graph.review(
        tokens["reviewer"], proposal.proposal_id, approve=True,
        expected_revision=proposal.revision, operation_id="revoked-after-write:review",
    )
    control = host.graph._proposal_store
    host.graph._proposal_store = FailFirstApplyCompletion(control)
    with pytest.raises(RuntimeError):
        host.graph.apply(
            tokens["writer"], proposal.proposal_id,
            expected_revision=reviewed.revision, operation_id="revoked-after-write:apply",
        )

    host.evidence.revoke(
        tokens["writer"], source.revision_id, operation_id="revoked-after-write:revoke"
    )
    host.access.revoke_grant("writer")
    applied = host.graph.apply(
        tokens["reconciler"], proposal.proposal_id,
        expected_revision=reviewed.revision, operation_id="revoked-after-write:apply",
    )
    assert applied.status == "applied"
    assert applied.applied_by == "writer"
    assert host.graph._write_authority.get_node(scope, node.node_id).revision == 1
    with pytest.raises(FoundationError) as unavailable:
        host.graph.get_node(tokens["reconciler"], node.node_id)
    assert unavailable.value.code == "graph_node_not_found"


def test_proposal_store_and_graph_write_authority_are_physically_separable(tmp_path):
    control_path = tmp_path / "separate-graph-control.sqlite3"
    write_path = tmp_path / "separate-graph-writes.sqlite3"
    configuration = HostConfiguration(
        configuration_id="separate-graph-authorities",
        version=1,
        enabled_modules=("access", "evidence", "graph"),
        stores={
            "access": {
                "store_id": "access", "driver": "sqlite",
                "endpoint_ref": str(tmp_path / "separate-access.sqlite3"),
            },
            "evidence": {
                "store_id": "evidence", "driver": "sqlite",
                "endpoint_ref": str(tmp_path / "separate-evidence.sqlite3"),
            },
            "graph-control": {
                "store_id": "graph-control", "driver": "sqlite",
                "endpoint_ref": str(control_path),
            },
            "graph-write": {
                "store_id": "graph-write", "driver": "sqlite",
                "endpoint_ref": str(write_path),
            },
        },
        module_stores={
            "access": "access", "evidence": "evidence", "graph": "graph-control",
        },
        module_write_stores={"graph": "graph-write"},
    )
    host = create_graph_host(configuration)
    scope = ScopeRef(tenant_id="tenant", space_id="separate")
    tokens = {}
    for principal, actions in {
        "writer": ("admit", "read", "propose", "apply"),
        "reviewer": ("review", "read"),
    }.items():
        host.access.put_grant(AccessGrant(
            grant_id=principal,
            principal_id=principal,
            scope=scope,
            domains=("evidence", "graph"),
            actions=actions,
            limits={"max_items": 8},
        ))
        tokens[principal] = host.access.issue_session(
            Principal(principal_id=principal, kind="service"), principal
        ).access_token
    try:
        source = host.evidence.admit(
            tokens["writer"], source_id="separated",
            content="separate proposal and write stores", operation_id="separated:source",
        )
        node = GraphNode(
            node_id="separated", scope=scope, node_type="fact", label="separated",
            source_refs=(source.ref(),),
        )
        proposal = host.graph.propose(
            tokens["writer"], operations=(GraphOperation(action="put_node", node=node),),
            evidence_refs=node.source_refs, operation_id="separated:propose",
        )
        reviewed = host.graph.review(
            tokens["reviewer"], proposal.proposal_id, approve=True,
            expected_revision=proposal.revision, operation_id="separated:review",
        )
        applied = host.graph.apply(
            tokens["writer"], proposal.proposal_id,
            expected_revision=reviewed.revision, operation_id="separated:apply",
        )
        assert host.graph.get_proposal(tokens["writer"], proposal.proposal_id) == applied
        assert host.graph.get_node(tokens["writer"], "separated").revision == 1
    finally:
        host.close()

    with sqlite3.connect(control_path) as control:
        control_tables = {
            row[0] for row in control.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    with sqlite3.connect(write_path) as write:
        write_tables = {
            row[0] for row in write.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "af_graph_nodes" not in control_tables
    assert "af_graph_proposals" not in write_tables


def test_graph_apply_job_reconciles_retryable_cross_store_failure(system, tmp_path):
    host, _create, tokens, scope = system
    host.access.put_grant(AccessGrant(
        grant_id="graph-job-runner",
        principal_id="graph-job-runner",
        scope=scope,
        domains=("evidence", "graph", "jobs"),
        actions=("plan", "authorize", "execute", "read", "apply"),
        limits={"max_items": 8},
    ))
    job_token = host.access.issue_session(
        Principal(principal_id="graph-job-runner", kind="service"),
        "graph-job-runner",
    ).access_token
    source = host.evidence.admit(
        tokens["writer"],
        source_id="job-recovery",
        content="job recovery",
        operation_id="job-recovery:source",
    )
    node = GraphNode(
        node_id="job-recovery",
        scope=scope,
        node_type="fact",
        label="job recovery",
        source_refs=(source.ref(),),
    )
    proposal = host.graph.propose(
        tokens["writer"],
        operations=(GraphOperation(action="put_node", node=node),),
        evidence_refs=node.source_refs,
        operation_id="job-recovery:propose",
    )
    reviewed = host.graph.review(
        tokens["reviewer"],
        proposal.proposal_id,
        approve=True,
        expected_revision=proposal.revision,
        operation_id="job-recovery:review",
    )
    host.graph._proposal_store = RetryableFailFirstApplyCompletion(
        host.graph._proposal_store
    )
    jobs = JobsApplication(
        SqliteJobAuthority(sqlite3.connect(
            tmp_path / "graph-apply-jobs.sqlite3", check_same_thread=False
        )),
        host.access,
        JobPolicy(policy_id="jobs", version=1),
        {"graph.apply": GraphApplyWorkflow(host.graph)},
    )
    try:
        planned = jobs.plan(
            job_token,
            kind="graph.apply",
            payload={
                "proposal_id": proposal.proposal_id,
                "expected_revision": reviewed.revision,
                "job_key": "job-recovery",
            },
            operation_id="job-recovery:plan",
        )
        authorized = jobs.authorize_job(
            job_token,
            planned.job_id,
            expected_revision=planned.revision,
            operation_id="job-recovery:authorize",
        )
        first = jobs.run_next(
            job_token, authorized.job_id, worker_id="worker-a"
        )
        assert first.status == "failed" and first.retryable is True
        assert first.error_code == "proposal_store_unavailable"
        recovered = jobs.run_next(
            job_token, authorized.job_id, worker_id="worker-b"
        )
        assert recovered.status == "succeeded"
        assert recovered.result["operation_id"] == "graph-apply-job:job-recovery"
        assert host.graph.get_node(tokens["reader"], "job-recovery").revision == 1
        with pytest.raises(FoundationError):
            host.graph.get_node_revision(tokens["reader"], "job-recovery", 2)
    finally:
        jobs.close()


@pytest.mark.parametrize("limit", [1, 3])
def test_faulty_algorithm_cannot_duplicate_expand_or_return_invalid_scores(system, limit):
    host, _create, tokens, scope = system
    for name in ("a", "b", "c"):
        add_node(host, tokens, scope, name)

    class Faulty(LexicalGraphCandidates):
        def rank(self, nodes, *, query, limit):
            assert {node.node_id for node in nodes} == {"a", "b", "c"}
            return (("a", float("nan")), ("a", float("inf")), ("a", 10 ** 10000), ("outside-scope", 9),
                    ("a", 3), ("a", 2), ("b", 1), ("c", .5), ("c", .4))

    host.graph._algorithm = Faulty()
    result = host.graph.query(tokens["reader"], query="needle", limit=limit)
    assert [hit.node.node_id for hit in result.hits] == ["a", "b", "c"][:limit]
    assert len(set(result.execution.output_refs)) == len(result.hits) <= limit
    assert result.execution.diagnostics["invalid_algorithm_items"] == 4
    if limit == 3:
        assert result.execution.diagnostics["duplicate_algorithm_items"] == 1


def test_candidate_only_source_is_replaceable_and_authority_canonicalizes_output(system):
    host, _create, tokens, scope = system
    current, _source = add_node(host, tokens, scope, "a")

    class CandidateOnly:
        def candidates(self, candidate_scope, *, query, limit):
            assert candidate_scope == scope
            assert query == "needle"
            foreign = current.ref().model_copy(update={
                "scope": ScopeRef(tenant_id="other", space_id=scope.space_id),
            })
            return current.ref(), foreign

    host.graph._candidate_source = CandidateOnly()
    result = host.graph.query(tokens["reader"], query="needle", limit=1)
    assert [hit.node.label for hit in result.hits] == ["needle a"]


def test_algorithm_output_inspection_is_bounded_even_for_nonterminating_duplicates(system):
    host, create, tokens, scope = system
    add_node(host, tokens, scope, "a")

    class Faulty(LexicalGraphCandidates):
        def rank(self, nodes, *, query, limit):
            return repeat(("a", 1.0))

    bounded = create(scan=5)
    bounded.graph._algorithm = Faulty()
    result = bounded.graph.query(tokens["reader"], query="needle", limit=2)
    assert [hit.node.node_id for hit in result.hits] == ["a"]
    assert result.execution.diagnostics["algorithm_items_inspected"] == 5
    assert result.execution.diagnostics["algorithm_item_limit_reached"] is True


def test_lazy_algorithm_finishes_before_rechecking_previously_yielded_candidates(system):
    host, create, tokens, scope = system
    _node, source_a = add_node(host, tokens, scope, "a")
    add_node(host, tokens, scope, "b")
    writer = create()

    class LazyRevoking(LexicalGraphCandidates):
        def rank(self, nodes, *, query, limit):
            yield "a", 2.0
            writer.evidence.revoke(
                tokens["writer"], source_a.revision_id, operation_id="revoke:between-yields"
            )
            yield "b", 1.0

    host.graph._algorithm = LazyRevoking()
    result = host.graph.query(tokens["reader"], query="needle", limit=2)
    assert not writer.evidence.is_current(source_a.ref())
    assert [hit.node.node_id for hit in result.hits] == ["b"]
    assert result.execution.diagnostics["algorithm_items_materialized"] == 2
    assert result.execution.diagnostics["stale_algorithm_items"] == 1


def test_revoked_prefix_does_not_consume_result_quota_and_scan_limit_is_explicit(system):
    host, create, tokens, scope = system
    add_node(host, tokens, scope, "valid")
    for number in range(4):
        _node, source = add_node(host, tokens, scope, f"revoked-{number}")
        host.evidence.revoke(tokens["writer"], source.revision_id, operation_id=f"revoke:{number}")

    class Observing(LexicalGraphCandidates):
        def rank(self, nodes, *, query, limit):
            assert [node.node_id for node in nodes] == ["valid"]
            return super().rank(nodes, query=query, limit=limit)

    host.graph._algorithm = Observing()
    result = host.graph.query(tokens["reader"], query="needle", limit=1)
    assert [hit.node.node_id for hit in result.hits] == ["valid"]
    assert result.execution.diagnostics["scan_limit"] == 12  # Grant is tighter than Profile.
    assert result.execution.diagnostics["scanned"] == 5
    limited = create(scan=4)
    exhausted = limited.graph.query(tokens["reader"], query="needle", limit=1)
    assert exhausted.hits == ()
    assert exhausted.execution.diagnostics["scan_limit"] == 4  # Profile is tighter than Grant.
    assert exhausted.execution.diagnostics["scan_limit_reached"] is True
    host.close()
    reopened = create()
    assert reopened.graph.query(tokens["reader"], query="needle", limit=1).hits[0].node.node_id == "valid"


@pytest.mark.parametrize("mutation", ["source", "node", "grant"])
def test_candidates_recheck_real_concurrent_source_revision_and_grant(system, mutation):
    host, create, tokens, scope = system
    original, source = add_node(host, tokens, scope, "a")
    writer = create()

    class Changing(LexicalGraphCandidates):
        def rank(self, nodes, *, query, limit):
            ranked = super().rank(nodes, query=query, limit=limit)
            if mutation == "source":
                writer.evidence.revoke(tokens["writer"], source.revision_id, operation_id="revoke:during-query")
            elif mutation == "node":
                updated = original.model_copy(update={"label": "new revision"})
                apply(writer, tokens, (GraphOperation(action="put_node", node=updated, expected_revision=original.revision),),
                      updated.source_refs, "update:during-query")
            else:
                writer.access.revoke_grant("reader")
            return ranked

    host.graph._algorithm = Changing()
    if mutation == "grant":
        with pytest.raises(FoundationError):
            host.graph.query(tokens["reader"], query="needle", limit=1)
    else:
        result = host.graph.query(tokens["reader"], query="needle", limit=1)
        assert result.hits == ()
        assert result.execution.diagnostics["stale_algorithm_items"] == 1


def test_edge_currentness_includes_endpoint_evidence_across_all_live_reads_and_restart(system):
    host, create, tokens, scope = system
    _a, source_a = add_node(host, tokens, scope, "a")
    add_node(host, tokens, scope, "b")
    edge_source = host.evidence.admit(tokens["writer"], source_id="edge", content="a knows b", operation_id="edge:source")
    edge = GraphEdge(edge_id="edge", scope=scope, relation_type="knows", from_node_id="a", to_node_id="b",
                     source_refs=(edge_source.ref(),))
    apply(host, tokens, (GraphOperation(action="put_edge", edge=edge),), edge.source_refs, "edge")
    assert host.graph.is_current(edge.ref())
    host.evidence.revoke(tokens["writer"], source_a.revision_id, operation_id="revoke:endpoint")
    for graph in (host.graph, create().graph):
        with pytest.raises(FoundationError, match="Graph edge is unavailable"):
            graph.get_edge(tokens["reader"], "edge")
        assert not graph.is_current(edge.ref())
        assert graph.list_edges(tokens["reader"]).items == ()
        neighborhood = graph.neighborhood(tokens["reader"], "b")
        assert neighborhood.edges == neighborhood.nodes == ()
        assert graph.get_node(tokens["reader"], "b").node_id == "b"
