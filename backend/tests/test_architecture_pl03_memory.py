from __future__ import annotations

import sqlite3

import pytest

from agent_foundation.adapters.outbound.access_state import SqliteAccessStateRepository
from agent_foundation.adapters.outbound.sqlite_memory_v2 import SqliteMemoryAuthority
from agent_foundation.adapters.outbound.projected_candidates_v2 import (
    AuthorityMemoryCandidateSource,
)
from agent_foundation.algorithms.memory_v2 import LexicalMemoryCandidates, RecencyMemoryCandidates
from agent_foundation.execution.contracts import ExecutionProfile
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import Principal, ResourceRef, ScopeRef
from agent_foundation.modules.access.application import AccessApplication
from agent_foundation.modules.access.contracts import AccessGrant
from agent_foundation.modules.memory.application import MemoryApplication
from agent_foundation.modules.memory.contracts import (
    MemoryEntry,
    MemoryMaintenanceAction,
    MemoryPolicy,
)


class MutableCurrentness:
    def __init__(self) -> None:
        self.current: set[ResourceRef] = set()

    def is_current(self, ref: ResourceRef) -> bool:
        return ref in self.current


@pytest.fixture
def memory_system(tmp_path):
    access_db = tmp_path / "access.sqlite3"
    memory_db = tmp_path / "memory.sqlite3"
    access = AccessApplication(SqliteAccessStateRepository(sqlite3.connect(access_db, check_same_thread=False)))
    scope = ScopeRef(tenant_id="tenant", space_id="memory")
    access.put_grant(AccessGrant(
        grant_id="memory-grant",
        principal_id="service",
        scope=scope,
        domains=("memory",),
        actions=("propose", "review", "read", "correct", "revoke", "feedback", "maintain_plan", "maintain_apply"),
        limits={"max_items": 10},
    ))
    token = access.issue_session(
        Principal(principal_id="service", kind="service"), "memory-grant"
    ).access_token
    policy = MemoryPolicy(policy_id="memory", version=1)
    profile = ExecutionProfile(
        profile_id="memory-recall",
        version=1,
        domain="memory",
        algorithm_id="memory.lexical-v2",
        implementation_version="1",
        budget={"max_items": 8},
    )
    sources = MutableCurrentness()

    def create(algorithm=None, selected_profile=None, *, link_port=None, candidate_source=None):
        authority = SqliteMemoryAuthority(sqlite3.connect(memory_db, check_same_thread=False))
        return MemoryApplication(
            authority,
            access,
            policy,
            selected_profile or profile,
            algorithm or LexicalMemoryCandidates(),
            sources,
            candidate_source=candidate_source or AuthorityMemoryCandidateSource(authority),
            link_port=link_port,
        )

    app = create()
    try:
        yield app, create, token, scope, sources
    finally:
        app.close()
        access.close()


def activate(app: MemoryApplication, token: str, content: str, operation: str, source_refs=()):
    candidate = app.propose(
        token,
        content=content,
        source_refs=source_refs,
        operation_id=f"{operation}-propose",
    )
    return app.review(
        token,
        candidate.memory_id,
        approve=True,
        expected_revision=candidate.revision,
        operation_id=f"{operation}-review",
    )


def test_formation_review_exact_revision_and_restart(memory_system) -> None:
    app, create, token, _scope, _sources = memory_system
    candidate = app.propose(token, content="remember alpha", operation_id="form")
    assert candidate.status == "candidate"
    assert app.formation_status(token).model_dump(exclude={"authority_watermark"}) == {
        "candidates": 1,
        "active": 0,
        "rejected": 0,
    }
    active = app.review(
        token,
        candidate.memory_id,
        approve=True,
        expected_revision=1,
        operation_id="review",
    )
    assert (active.status, active.revision) == ("active", 2)
    assert app.formation_status(token).active == 1
    assert app.get_revision(token, active.memory_id, 1) == candidate
    assert app.get(token, active.memory_id) == active
    app.close()
    reopened = create()
    try:
        assert reopened.get(token, active.memory_id) == active
        assert reopened.get_operation_receipt(token, "review").resource_ids == (active.memory_id,)
    finally:
        reopened.close()


def test_direct_read_and_recall_share_source_currentness(memory_system) -> None:
    app, _create, token, scope, sources = memory_system
    source = ResourceRef(
        domain="evidence",
        scope=scope,
        resource_type="evidence_revision",
        resource_id="evidence-1",
        revision="evidence-1",
    )
    sources.current.add(source)
    active = activate(app, token, "source backed alpha", "source", (source,))
    assert app.get(token, active.memory_id) == active
    assert app.recall(token, query="alpha").hits[0].entry == active
    sources.current.remove(source)
    with pytest.raises(FoundationError) as unavailable:
        app.get(token, active.memory_id)
    assert unavailable.value.code == "memory_not_found"
    assert app.recall(token, query="alpha").hits == ()
    assert not app.is_current(active.ref())


def test_candidate_is_rechecked_after_algorithm_execution(memory_system) -> None:
    app, _create, token, _scope, _sources = memory_system
    active = activate(app, token, "volatile alpha", "volatile")

    class RevokingCandidates(LexicalMemoryCandidates):
        def rank(self, entries, *, query, limit):
            ranked = super().rank(entries, query=query, limit=limit)
            app.revoke(
                token,
                active.memory_id,
                expected_revision=active.revision,
                operation_id="during-recall-revoke",
            )
            return ranked

    app._algorithm = RevokingCandidates()
    assert app.recall(token, query="alpha").hits == ()


def test_candidate_only_source_is_replaceable_and_authority_canonicalizes_output(memory_system) -> None:
    app, create, token, scope, _sources = memory_system
    active = activate(app, token, "authority memory", "candidate-port")

    class CandidateOnly:
        def candidates(self, candidate_scope, *, query, limit):
            assert candidate_scope == scope
            assert query == "memory"
            assert limit == 4
            foreign = active.ref().model_copy(update={
                "scope": ScopeRef(tenant_id="other", space_id=scope.space_id),
            })
            return active.ref(), foreign

    reader = create(candidate_source=CandidateOnly())
    try:
        result = reader.recall(token, query="memory", limit=1)
        assert [hit.entry.content for hit in result.hits] == ["authority memory"]
    finally:
        reader.close()


def test_operation_retries_and_version_conflicts_do_not_duplicate_side_effects(memory_system) -> None:
    app, _create, token, _scope, _sources = memory_system
    candidate = app.propose(token, content="stable", operation_id="stable-propose")
    assert app.propose(token, content="stable", operation_id="stable-propose") == candidate
    with pytest.raises(FoundationError) as operation_conflict:
        app.propose(token, content="different", operation_id="stable-propose")
    assert operation_conflict.value.code == "operation_id_conflict"

    active = app.review(
        token,
        candidate.memory_id,
        approve=True,
        expected_revision=1,
        operation_id="stable-review",
    )
    assert app.review(
        token,
        candidate.memory_id,
        approve=True,
        expected_revision=1,
        operation_id="stable-review",
    ) == active
    feedback = app.feedback(
        token,
        active.memory_id,
        value=1,
        expected_revision=active.revision,
        operation_id="stable-feedback",
    )
    assert app.feedback(
        token,
        active.memory_id,
        value=1,
        expected_revision=active.revision,
        operation_id="stable-feedback",
    ) == feedback
    with pytest.raises(FoundationError) as version_conflict:
        app.feedback(
            token,
            active.memory_id,
            value=-1,
            expected_revision=active.revision,
            operation_id="new-feedback",
        )
    assert version_conflict.value.code == "memory_version_conflict"
    assert app.get(token, active.memory_id).feedback_count == 1


def test_correction_creates_successor_and_preserves_history(memory_system) -> None:
    app, _create, token, _scope, _sources = memory_system
    active = activate(app, token, "wrong preference", "correct")
    successor = app.correct(
        token,
        active.memory_id,
        content="correct preference",
        expected_revision=active.revision,
        operation_id="correct-op",
    )
    retried = app.correct(
        token,
        active.memory_id,
        content="correct preference",
        expected_revision=active.revision,
        operation_id="correct-op",
    )
    assert retried == successor
    assert app.get(token, successor.memory_id) == successor
    with pytest.raises(FoundationError):
        app.get(token, active.memory_id)
    superseded = app.get_revision(token, active.memory_id, active.revision + 1)
    assert superseded.status == "superseded"
    assert superseded.successor_id == successor.memory_id


def test_maintenance_plan_is_persistent_atomic_idempotent_and_stale_guarded(memory_system) -> None:
    app, create, token, _scope, _sources = memory_system
    first = activate(app, token, "first", "maintain-first")
    second = activate(app, token, "second", "maintain-second")
    window = app.freeze_dreaming_window(
        token,
        memory_ids=(first.memory_id, second.memory_id),
        operation_id="dream-window",
    )
    assert app.freeze_dreaming_window(
        token,
        memory_ids=(first.memory_id, second.memory_id),
        operation_id="dream-window",
    ) == window
    assert app.get_dreaming_window(token, window.window_id) == window
    assert app.list_dreaming_windows(token).items == (window,)
    stale = app.plan_dreaming_maintenance(
        token,
        window_id=window.window_id,
        replacements={first.memory_id: "first rewritten", second.memory_id: "second rewritten"},
        rationale="consolidate",
        operation_id="stale-plan",
    )
    app.feedback(
        token,
        first.memory_id,
        value=1,
        expected_revision=first.revision,
        operation_id="make-plan-stale",
    )
    with pytest.raises(FoundationError) as conflict:
        app.apply_maintenance(token, stale.plan.plan_id, operation_id="stale-apply")
    assert conflict.value.code == "memory_maintenance_stale"
    assert app.get(token, second.memory_id).content == "second"

    current_first = app.get(token, first.memory_id)
    plan = app.plan_rewrite_maintenance(
        token,
        kind="revision_set",
        replacements={current_first.memory_id: "first fixed", second.memory_id: "second fixed"},
        rationale="approved set",
        operation_id="revision-set-plan",
    )
    assert app.plan_rewrite_maintenance(
        token,
        kind="revision_set",
        replacements={current_first.memory_id: "first fixed", second.memory_id: "second fixed"},
        rationale="approved set",
        operation_id="revision-set-plan",
    ) == plan
    app.close()
    app = create()
    assert app.get_dreaming_window(token, window.window_id) == window
    assert app.get_maintenance_plan(token, plan.plan.plan_id) == plan
    assert app.list_maintenance_plans(token).items[0] == plan
    applied = app.apply_maintenance(token, plan.plan.plan_id, operation_id="revision-set")
    assert {entry.content for entry in applied.entries} == {"first fixed", "second fixed"}
    assert app.apply_maintenance(token, plan.plan.plan_id, operation_id="revision-set") == applied
    applied_plan = app.get_maintenance_plan(token, plan.plan.plan_id)
    assert (applied_plan.state, applied_plan.applied_operation_id) == ("applied", "revision-set")
    assert app.plan_rewrite_maintenance(
        token,
        kind="revision_set",
        replacements={current_first.memory_id: "first fixed", second.memory_id: "second fixed"},
        rationale="approved set",
        operation_id="revision-set-plan",
    ) == applied_plan
    rolled_back = app.rollback_maintenance(
        token, "revision-set", operation_id="revision-set-rollback"
    )
    assert {entry.memory_id for entry in rolled_back.entries if entry.status == "active"} == {
        first.memory_id,
        second.memory_id,
    }
    assert app.rollback_maintenance(
        token, "revision-set", operation_id="revision-set-rollback"
    ) == rolled_back
    rolled_back_plan = app.get_maintenance_plan(token, plan.plan.plan_id)
    assert (rolled_back_plan.state, rolled_back_plan.rollback_operation_id) == (
        "rolled_back",
        "revision-set-rollback",
    )
    assert app.get(token, first.memory_id).content == "first"
    assert app.get(token, second.memory_id).content == "second"
    app.close()


def test_dreaming_v1_five_actions_dispositions_and_rollback(memory_system) -> None:
    app, _create, token, scope, _sources = memory_system
    entries = tuple(
        activate(app, token, f"memory {index}", f"dream-five-{index}")
        for index in range(5)
    )
    window = app.freeze_dreaming_window(
        token,
        memory_ids=tuple(item.memory_id for item in entries),
        operation_id="dream-five-window",
    )
    graph_target = ResourceRef(
        domain="graph", scope=scope, resource_type="graph_node",
        resource_id="topic:architecture", revision="4",
    )
    actions = (
        MemoryMaintenanceAction(
            action_id="create", action="create", disposition="auto_apply",
            replacement_content="new synthesized memory", new_memory_id="dream-created",
        ),
        MemoryMaintenanceAction(
            action_id="update", action="update", disposition="owner_decision",
            memory_id=entries[0].memory_id, expected_revision=entries[0].revision,
            replacement_content="memory zero updated",
        ),
        MemoryMaintenanceAction(
            action_id="merge", action="merge", disposition="owner_decision",
            memory_ids=(entries[1].memory_id, entries[2].memory_id),
            expected_revisions={
                entries[1].memory_id: entries[1].revision,
                entries[2].memory_id: entries[2].revision,
            },
            replacement_content="merged memory", new_memory_id="dream-merged",
        ),
        MemoryMaintenanceAction(
            action_id="archive", action="archive", disposition="auto_apply",
            memory_id=entries[3].memory_id, expected_revision=entries[3].revision,
        ),
        MemoryMaintenanceAction(
            action_id="link", action="link", disposition="auto_apply",
            memory_id=entries[4].memory_id, expected_revision=entries[4].revision,
            link_target=graph_target, link_relation="supports",
        ),
    )
    plan = app.plan_dreaming_maintenance(
        token,
        window_id=window.window_id,
        actions=actions,
        rationale="exercise all Dreaming V1 actions",
        planner_id="external-planner-v1",
        planner_output={"model": "external", "actions": 5},
        draft={"reviewed": True},
        operation_id="dream-five-plan",
    )
    assert plan.plan.planner_id == "external-planner-v1"
    applied = app.apply_maintenance(
        token, plan.plan.plan_id, operation_id="dream-five-apply"
    )
    assert {item.status for item in applied.action_receipts} == {
        "applied", "graph_pending"
    }
    stored = app.get_maintenance_plan(token, plan.plan.plan_id)
    assert stored.state == "graph_pending"
    assert app.get(token, entries[0].memory_id).content == "memory zero updated"
    assert app.get(token, "dream-created").content == "new synthesized memory"
    assert app.get(token, "dream-merged").content == "merged memory"
    with pytest.raises(FoundationError):
        app.get(token, entries[1].memory_id)
    with pytest.raises(FoundationError):
        app.get(token, entries[3].memory_id)
    assert app.get(token, entries[4].memory_id).status == "active"

    app.rollback_maintenance(
        token, "dream-five-apply", operation_id="dream-five-rollback"
    )
    for index, entry in enumerate(entries):
        assert app.get(token, entry.memory_id).content == f"memory {index}"
    with pytest.raises(FoundationError):
        app.get(token, "dream-created")
    with pytest.raises(FoundationError):
        app.get(token, "dream-merged")

    fresh = app.freeze_dreaming_window(
        token,
        memory_ids=(entries[0].memory_id, entries[1].memory_id),
        operation_id="proposal-only-window",
    )
    current = app.get(token, entries[0].memory_id)
    proposal = app.plan_dreaming_maintenance(
        token,
        window_id=fresh.window_id,
        actions=(MemoryMaintenanceAction(
            action_id="plan-only-update", action="update",
            memory_id=current.memory_id, expected_revision=current.revision,
            replacement_content="must not apply",
        ),),
        rationale="proposal permission mode",
        proposal_only=True,
        operation_id="proposal-only-plan",
    )
    assert proposal.plan.actions[0].disposition == "plan_only"
    with pytest.raises(FoundationError) as plan_only:
        app.apply_maintenance(
            token, proposal.plan.plan_id, operation_id="proposal-only-apply"
        )
    assert plan_only.value.code == "memory_maintenance_plan_only"
    app.close()


def test_dreaming_link_uses_graph_port_and_resumes_with_stable_action_id(memory_system) -> None:
    app, create, token, scope, _sources = memory_system
    source = activate(app, token, "linked memory", "link-source")
    other = activate(app, token, "window peer", "link-peer")
    app.close()

    class LinkPort:
        def __init__(self):
            self.calls = []

        def propose_link(self, _token, entry, action, *, operation_id):
            self.calls.append((entry.memory_id, action.action_id, operation_id))
            return (
                "graph_pending" if len(self.calls) == 1 else "applied",
                ResourceRef(
                    domain="graph", scope=scope, resource_type="graph_proposal",
                    resource_id="link-proposal", revision=str(len(self.calls)),
                ),
            )

    link_port = LinkPort()
    app = create(link_port=link_port)
    try:
        window = app.freeze_dreaming_window(
            token, memory_ids=(source.memory_id, other.memory_id),
            operation_id="link-window",
        )
        current = app.get(token, source.memory_id)
        plan = app.plan_dreaming_maintenance(
            token,
            window_id=window.window_id,
            actions=(MemoryMaintenanceAction(
                action_id="stable-link", action="link", disposition="auto_apply",
                memory_id=current.memory_id, expected_revision=current.revision,
                link_target=ResourceRef(
                    domain="graph", scope=scope, resource_type="graph_node",
                    resource_id="target", revision="1",
                ),
                link_relation="supports",
            ),),
            rationale="link through Graph public port",
            operation_id="link-plan",
        )
        first = app.apply_maintenance(
            token, plan.plan.plan_id, operation_id="link-apply"
        )
        assert first.action_receipts[0].status == "graph_pending"
        assert first.action_receipts[0].resource_refs[-1].resource_id == "link-proposal"
        second = app.apply_maintenance(
            token, plan.plan.plan_id, operation_id="link-apply"
        )
        assert second.action_receipts[0].status == "applied"
        assert app.get_maintenance_plan(token, plan.plan.plan_id).state == "applied"
        assert link_port.calls == [
            (source.memory_id, "stable-link", "link-apply:stable-link"),
            (source.memory_id, "stable-link", "link-apply:stable-link"),
        ]
    finally:
        app.close()


def test_second_candidate_algorithm_replaces_recall_without_authority_change(memory_system) -> None:
    app, create, token, _scope, _sources = memory_system
    older = activate(app, token, "older", "older")
    newer = activate(app, token, "newer", "newer")
    app.close()
    profile = ExecutionProfile(
        profile_id="memory-recall-recency",
        version=1,
        domain="memory",
        algorithm_id="memory.recency-v2",
        implementation_version="1",
        budget={"max_items": 8},
    )
    recency = create(RecencyMemoryCandidates(), profile)
    try:
        result = recency.recall(token, query="ignored", limit=2)
        assert [hit.entry.memory_id for hit in result.hits] == [newer.memory_id, older.memory_id]
    finally:
        recency.close()


def test_list_trace_and_controlled_restore_preserve_revision_history(memory_system) -> None:
    app, _create, token, _scope, _sources = memory_system
    active = activate(app, token, "recoverable", "restore")
    revoked = app.revoke(
        token,
        active.memory_id,
        expected_revision=active.revision,
        operation_id="restore-revoke",
    )
    assert app.list(token, states=("active",)).items == ()
    assert app.list(token, states=("revoked",)).items == (revoked,)
    trace = app.trace(token, active.memory_id)
    assert [entry.status for entry in trace.revisions] == ["candidate", "active", "revoked"]
    restored = app.restore(
        token,
        active.memory_id,
        expected_revision=revoked.revision,
        operation_id="restore-apply",
    )
    assert restored.revision == revoked.revision + 1
    assert app.restore(
        token,
        active.memory_id,
        expected_revision=revoked.revision,
        operation_id="restore-apply",
    ) == restored
    assert app.get(token, active.memory_id) == restored
    activation = app.activation(token, active.memory_id)
    assert activation.status == "active"
    assert activation.sources_current
    assert activation.feedback_count == 0


def test_reinforcement_events_reconcile_memory_projection_transactionally(memory_system) -> None:
    app, _create, token, _scope, _sources = memory_system
    active = activate(app, token, "reinforced", "reinforce")
    projected = app.feedback(
        token,
        active.memory_id,
        value=1,
        expected_revision=active.revision,
        operation_id="reinforcement-observe",
    )
    event = app.list_feedback(token, memory_id=active.memory_id).items[0]
    assert event.status == "active"
    assert (projected.feedback_total, projected.feedback_count) == (1, 1)

    contested = app.transition_feedback(
        token,
        event.event_id,
        status="contested",
        reason="disputed signal",
        expected_revision=event.revision,
        operation_id="reinforcement-contest",
    )
    assert app.transition_feedback(
        token,
        event.event_id,
        status="contested",
        reason="disputed signal",
        expected_revision=event.revision,
        operation_id="reinforcement-contest",
    ) == contested
    after_contest = app.get(token, active.memory_id)
    assert (after_contest.feedback_total, after_contest.feedback_count) == (0, 0)
    diagnostic = app.reinforcement_diagnostic(token, active.memory_id)
    assert (diagnostic.active_count, diagnostic.contested_count) == (0, 1)

    verified = app.transition_feedback(
        token,
        event.event_id,
        status="active",
        reason="verified signal",
        expected_revision=contested.revision,
        operation_id="reinforcement-verify",
    )
    assert app.get(token, active.memory_id).feedback_total == 1
    retracted = app.transition_feedback(
        token,
        event.event_id,
        status="retracted",
        reason="source withdrew signal",
        expected_revision=verified.revision,
        operation_id="reinforcement-retract",
    )
    assert retracted.status == "retracted"
    assert app.get(token, active.memory_id).feedback_count == 0
def test_lexical_memory_candidates_match_short_cjk_query_inside_sentence():
    algorithm = LexicalMemoryCandidates()
    entry = MemoryEntry(
        scope=ScopeRef(tenant_id="tenant", space_id="space"),
        content="学生容易把分子与分母的位置混淆",
        status="active",
    )
    assert algorithm.rank((entry,), query="分子 分母", limit=8)[0][0] == entry.memory_id


def test_memory_operation_identity_is_scoped() -> None:
    authority = SqliteMemoryAuthority(sqlite3.connect(":memory:"))
    first_scope = ScopeRef(tenant_id="tenant", space_id="student-a")
    second_scope = ScopeRef(tenant_id="tenant", space_id="student-b")
    try:
        first = authority.create(
            MemoryEntry(
                memory_id="shared-memory-id",
                scope=first_scope,
                content="first student",
                status="active",
            ),
            operation_id="same-observation",
            command_digest="first-command",
        )
        second = authority.create(
            MemoryEntry(
                memory_id="shared-memory-id",
                scope=second_scope,
                content="second student",
                status="active",
            ),
            operation_id="same-observation",
            command_digest="second-command",
        )
        assert first.scope == first_scope
        assert second.scope == second_scope
    finally:
        authority.close()
