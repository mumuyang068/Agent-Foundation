from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import pytest

from agent_foundation.adapters.outbound.access_state import InMemoryAccessStateRepository
from agent_foundation.adapters.outbound.sqlite_jobs_v2 import SqliteJobAuthority
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import Principal, ScopeRef
from agent_foundation.modules.access.application import AccessApplication
from agent_foundation.modules.access.contracts import AccessGrant
from agent_foundation.modules.jobs.application import JobsApplication
from agent_foundation.modules.jobs.contracts import JobPolicy, JobStepResult
from agent_foundation.adapters.outbound.sqlite_application_turns_v2 import SqliteTurnAuthority
from agent_foundation.adapters.outbound.sqlite_memory_v2 import SqliteMemoryAuthority
from agent_foundation.algorithms.memory_v2 import LexicalMemoryCandidates
from agent_foundation.execution.contracts import ExecutionProfile
from agent_foundation.modules.applications.contracts import ApplicationPolicy
from agent_foundation.modules.memory.application import MemoryApplication
from agent_foundation.modules.memory.contracts import MemoryPolicy
from agent_foundation.workflows.agent_turns import AgentTurnWorkflow
from agent_foundation.workflows.domain_jobs import (
    GraphApplyWorkflow,
    GraphBulkProposalWorkflow,
    MemoryMaintenanceWorkflow,
)
from agent_foundation.kernel.primitives import ResourceRef
from agent_foundation.host.configuration import HostConfiguration
from agent_foundation.host.minimal import create_workflow_host


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 15, tzinfo=timezone.utc)

    def now(self):
        return self.value


def _fixture(tmp_path, handler, *, clock=None):
    access = AccessApplication(InMemoryAccessStateRepository())
    scope = ScopeRef(tenant_id="tenant", space_id="jobs")
    grant = AccessGrant(
        grant_id="jobs", principal_id="worker", scope=scope, domains=("jobs",),
        actions=("plan", "authorize", "execute", "cancel", "read", "outbox_ack"),
        limits={"max_items": 100},
    )
    access.put_grant(grant)
    token = access.issue_session(Principal(principal_id="worker", kind="service"), grant.grant_id).access_token
    database = tmp_path / "jobs.sqlite"
    selected_clock = clock or Clock()

    def create():
        return JobsApplication(
            SqliteJobAuthority(sqlite3.connect(database, check_same_thread=False, timeout=10)),
            access,
            JobPolicy(policy_id="jobs", version=1),
            {"test": handler},
            now=selected_clock.now,
        )

    return create, access, token, scope, selected_clock


def _planned(app, token):
    job = app.plan(token, kind="test", payload={"value": 1}, operation_id="plan")
    return app.authorize_job(
        token, job.job_id, expected_revision=job.revision, operation_id="authorize"
    )


def test_job_plan_authorize_run_restart_and_outbox_are_durable(tmp_path) -> None:
    def handler(payload, cursor, fence):
        fence.check()
        return JobStepResult(done=True, cursor="1", result={"value": payload["value"]})

    create, _access, token, _scope, _clock = _fixture(tmp_path, handler)
    app = create()
    authorized = _planned(app, token)
    assert app.plan(token, kind="test", payload={"value": 1}, operation_id="plan").job_id == authorized.job_id
    completed = app.run_next(token, authorized.job_id, worker_id="worker-a")
    assert completed.status == "succeeded"
    events = app.list_outbox(token)
    assert [event.kind for event in events] == ["job.planned", "job.authorized", "job.claimed", "job.completed"]
    app.acknowledge_outbox(token, events[0].event_id)
    app.close()
    restarted = create()
    assert restarted.get(token, authorized.job_id) == completed
    assert restarted.get_operation_receipt(token, "plan").job_id == authorized.job_id
    assert len(restarted.list_outbox(token)) == 3
    restarted.close()


def test_two_workers_cannot_execute_same_active_lease(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def handler(_payload, _cursor, fence):
        entered.set()
        release.wait(timeout=5)
        fence.check()
        return JobStepResult(done=True)

    create, _access, token, _scope, _clock = _fixture(tmp_path, handler)
    first, second = create(), create()
    job = _planned(first, token)
    output = []
    thread = threading.Thread(
        target=lambda: output.append(first.run_next(token, job.job_id, worker_id="one"))
    )
    thread.start()
    assert entered.wait(timeout=2)
    with pytest.raises(FoundationError) as leased:
        second.run_next(token, job.job_id, worker_id="two")
    assert leased.value.code == "job_leased"
    release.set()
    thread.join(timeout=5)
    assert output[0].status == "succeeded"
    first.close()
    second.close()


def test_expired_lease_is_reclaimed_with_new_fencing_token(tmp_path) -> None:
    def handler(_payload, _cursor, fence):
        fence.check()
        return JobStepResult(done=True)

    clock = Clock()
    create, _access, token, scope, _clock = _fixture(tmp_path, handler, clock=clock)
    crashed = create()
    job = _planned(crashed, token)
    claimed = crashed._authority.claim(
        scope, job.job_id, worker_id="crashed", lease_seconds=10,
        now=clock.now(), max_attempts=5,
    )
    crashed.close()
    clock.value += timedelta(seconds=11)
    recovered = create()
    completed = recovered.run_next(token, job.job_id, worker_id="recovered")
    assert completed.status == "succeeded"
    assert completed.attempts == 2
    assert claimed.lease_token != completed.lease_token
    recovered.close()


def test_cancelled_running_job_cannot_commit_a_late_step(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def handler(_payload, _cursor, fence):
        entered.set()
        release.wait(timeout=5)
        fence.check()
        return JobStepResult(done=True)

    create, _access, token, _scope, _clock = _fixture(tmp_path, handler)
    runner, controller = create(), create()
    job = _planned(runner, token)
    output = []
    thread = threading.Thread(
        target=lambda: output.append(runner.run_next(token, job.job_id, worker_id="runner"))
    )
    thread.start()
    assert entered.wait(timeout=2)
    cancelled = controller.cancel(token, job.job_id, operation_id="cancel")
    release.set()
    thread.join(timeout=5)
    assert cancelled.status == output[0].status == "cancelled"
    assert output[0].result == {}
    runner.close()
    controller.close()


def test_revoked_authority_is_checked_before_owned_handler_effect(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()
    effects = []

    def handler(_payload, _cursor, fence):
        entered.set()
        release.wait(timeout=5)
        fence.check()
        effects.append("effect")
        return JobStepResult(done=True)

    create, access, token, _scope, _clock = _fixture(tmp_path, handler)
    app = create()
    job = _planned(app, token)
    output = []
    thread = threading.Thread(
        target=lambda: output.append(app.run_next(token, job.job_id, worker_id="runner"))
    )
    thread.start()
    assert entered.wait(timeout=2)
    access.revoke_grant("jobs")
    release.set()
    thread.join(timeout=5)
    assert effects == []
    assert output[0].status == "failed"
    assert output[0].error_code == "grant_inactive"
    app.close()


def test_operation_ids_reject_different_job_content(tmp_path) -> None:
    create, _access, token, _scope, _clock = _fixture(
        tmp_path, lambda _payload, _cursor, _fence: JobStepResult(done=True)
    )
    app = create()
    app.plan(token, kind="test", payload={"value": 1}, operation_id="same")
    with pytest.raises(FoundationError) as conflict:
        app.plan(token, kind="test", payload={"value": 2}, operation_id="same")
    assert conflict.value.code == "operation_id_conflict"
    app.close()


def _turn_fixture(tmp_path, *, context=None, authority_wrapper=None):
    access = AccessApplication(InMemoryAccessStateRepository())
    scope = ScopeRef(tenant_id="tenant", space_id="turns")
    tokens = {}
    for principal_id in ("alice", "bob"):
        grant = AccessGrant(
            grant_id=f"turn-{principal_id}", principal_id=principal_id, scope=scope,
            domains=("applications", "memory"),
            actions=("prepare", "observe", "outcome", "read", "propose", "feedback"),
            limits={"max_items": 20},
        )
        access.put_grant(grant)
        tokens[principal_id] = access.issue_session(
            Principal(principal_id=principal_id, kind="agent"), grant.grant_id
        ).access_token
    memory = MemoryApplication(
        SqliteMemoryAuthority(sqlite3.connect(tmp_path / "turn-memory.sqlite")),
        access,
        MemoryPolicy(policy_id="memory", version=1, auto_accept=True),
        ExecutionProfile(
            profile_id="memory", version=1, domain="memory",
            algorithm_id="memory.lexical-v2", implementation_version="1",
            budget={"max_items": 20},
        ),
        LexicalMemoryCandidates(),
        None,
    )
    authority = SqliteTurnAuthority(sqlite3.connect(tmp_path / "turns.sqlite"))
    if authority_wrapper:
        authority = authority_wrapper(authority)
    workflow = AgentTurnWorkflow(
        authority, access, ApplicationPolicy(policy_id="applications", version=1),
        memory=memory, context=context,
    )
    return workflow, memory, tokens


def test_direct_memory_agent_turn_observe_feedback_and_restart_state(tmp_path) -> None:
    workflow, memory, tokens = _turn_fixture(tmp_path)
    turn, assembly = workflow.prepare(
        tokens["alice"], application_id="neutral-agent", query="q", use_context=False,
        operation_id="prepare-turn",
    )
    assert assembly is None and turn.context_fingerprint is None
    observed, entry = workflow.observe(
        tokens["alice"], turn.turn_id, content="remember result", operation_id="observe-turn"
    )
    assert observed.memory_id == entry.memory_id and entry.status == "active"
    completed = workflow.record_outcome(
        tokens["alice"], turn.turn_id, value=0.8,
        expected_memory_revision=entry.revision, operation_id="outcome-turn",
    )
    assert completed.state == "completed"
    assert memory.get(tokens["alice"], entry.memory_id).feedback_count == 1
    with pytest.raises(FoundationError) as denied:
        workflow.get(tokens["bob"], turn.turn_id)
    assert denied.value.code == "application_turn_authority_invalid"
    workflow.close()
    memory.close()


def test_memory_success_turn_write_failure_recovers_without_duplicate_memory(tmp_path) -> None:
    class FailOnce:
        def __init__(self, inner):
            self.inner = inner
            self.failed = False
        def __getattr__(self, name):
            return getattr(self.inner, name)
        def record_observation(self, *args, **kwargs):
            if not self.failed:
                self.failed = True
                raise FoundationError("turn_store_unavailable", "injected", retryable=True)
            return self.inner.record_observation(*args, **kwargs)

    workflow, memory, tokens = _turn_fixture(tmp_path, authority_wrapper=FailOnce)
    turn, _ = workflow.prepare(
        tokens["alice"], application_id="neutral-agent", query="q", use_context=False,
        operation_id="prepare-recovery",
    )
    with pytest.raises(FoundationError):
        workflow.observe(
            tokens["alice"], turn.turn_id, content="one durable memory",
            operation_id="observe-recovery",
        )
    observed, entry = workflow.observe(
        tokens["alice"], turn.turn_id, content="one durable memory",
        operation_id="observe-recovery",
    )
    assert observed.memory_id == entry.memory_id
    assert len(memory.list(tokens["alice"], limit=20).items) == 1
    workflow.close()
    memory.close()


def test_optional_context_uses_same_token_and_is_explicit(tmp_path) -> None:
    class Assembly:
        fingerprint = "a" * 64
        items = ()
    class Context:
        def __init__(self):
            self.tokens = []
        def assemble(self, token, **kwargs):
            self.tokens.append((token, kwargs))
            return Assembly()

    context = Context()
    workflow, memory, tokens = _turn_fixture(tmp_path, context=context)
    turn, assembly = workflow.prepare(
        tokens["alice"], application_id="neutral-agent", query="q", use_context=True,
        source_domains=("memory",), operation_id="prepare-context",
    )
    assert assembly is not None and turn.context_fingerprint == "a" * 64
    assert context.tokens == [(tokens["alice"], {
        "query": "q", "source_domains": ("memory",), "pinned_refs": (),
    })]
    workflow.close()
    memory.close()


def test_owned_domain_job_handlers_use_fenced_worker_identity_and_stable_commands() -> None:
    scope = ScopeRef(tenant_id="tenant", space_id="workflow")
    source = ResourceRef(
        domain="evidence", scope=scope, resource_type="evidence_revision",
        resource_id="source", revision="source",
    )

    class Fence:
        token = "worker-token"
        checks = 0
        def check(self):
            self.checks += 1

    class Proposal:
        proposal_id = "proposal"
        status = "pending"
    class Graph:
        calls = []
        def propose(self, token, **kwargs):
            self.calls.append((token, kwargs))
            return Proposal()
    graph = Graph()
    fence = Fence()
    operation = {
        "action": "put_node",
        "node": {
            "node_id": "node", "scope": scope.model_dump(mode="json"),
            "node_type": "concept", "label": "Node", "source_refs": [source.model_dump(mode="json")],
        },
    }
    bulk = GraphBulkProposalWorkflow(graph)
    result = bulk({
        "bulk_id": "bulk", "operations": [operation],
        "evidence_refs": [source.model_dump(mode="json")], "chunk_size": 1,
    }, None, fence)
    assert result.done and graph.calls[0][0] == "worker-token"
    assert graph.calls[0][1]["operation_id"] == "bulk:bulk:0"
    assert fence.checks == 1

    class AppliedProposal:
        proposal_id = "proposal"
        revision = 3
        status = "applied"

    class ApplyingGraph:
        calls = []

        def apply(self, token, proposal_id, **kwargs):
            self.calls.append((token, proposal_id, kwargs))
            return AppliedProposal()

    applying_graph = ApplyingGraph()
    graph_apply = GraphApplyWorkflow(applying_graph)({
        "proposal_id": "proposal", "expected_revision": 2, "job_key": "job-key",
    }, None, fence)
    assert graph_apply.done and graph_apply.cursor == "applied"
    assert applying_graph.calls == [(
        "worker-token",
        "proposal",
        {"expected_revision": 2, "operation_id": "graph-apply-job:job-key"},
    )]

    class Entry:
        def ref(self):
            return ResourceRef(
                domain="memory", scope=scope, resource_type="memory", resource_id="m", revision="2"
            )
    class Change:
        entries = (Entry(),)
        action_receipts = ()
    class Memory:
        calls = []
        def apply_maintenance(self, token, plan_id, *, operation_id):
            self.calls.append((token, plan_id, operation_id))
            return Change()
    memory = Memory()
    maintained = MemoryMaintenanceWorkflow(memory)({
        "plan_id": "plan", "job_key": "job-key"
    }, None, fence)
    assert maintained.done
    assert memory.calls == [("worker-token", "plan", "maintenance-job:job-key")]


def test_memory_maintenance_job_waits_for_graph_and_cancel_stops_retry(tmp_path) -> None:
    class Receipt:
        action_id = "link-action"
        status = "graph_pending"

    class Change:
        entries = ()
        action_receipts = (Receipt(),)

    class Memory:
        def __init__(self):
            self.calls = []

        def apply_maintenance(self, token, plan_id, *, operation_id):
            self.calls.append((token, plan_id, operation_id))
            return Change()

    memory = Memory()
    workflow = MemoryMaintenanceWorkflow(memory)
    create, _access, token, _scope, _clock = _fixture(tmp_path, workflow)
    app = create()
    planned = app.plan(
        token,
        kind="test",
        payload={"plan_id": "plan", "job_key": "stable-job"},
        operation_id="plan",
    )
    authorized = app.authorize_job(
        token,
        planned.job_id,
        expected_revision=planned.revision,
        operation_id="authorize",
    )
    waiting = app.run_next(token, authorized.job_id, worker_id="worker")
    assert waiting.status == "authorized"
    assert waiting.cursor == "graph_pending"
    assert waiting.result == {
        "plan_id": "plan",
        "memory_refs": [],
        "pending_graph_action_ids": ["link-action"],
        "status": "graph_pending",
    }

    cancelled = app.cancel(token, authorized.job_id, operation_id="cancel")
    assert cancelled.status == "cancelled"
    with pytest.raises(FoundationError) as stopped:
        app.run_next(token, authorized.job_id, worker_id="worker-2")
    assert stopped.value.code == "job_not_runnable"
    assert memory.calls == [(token, "plan", "maintenance-job:stable-job")]
    app.close()


def _workflow_configuration(tmp_path, modules, *, dependencies=None):
    stores = {}
    module_stores = {}
    for module in modules:
        if module == "access":
            continue
        store_id = f"{module}-store"
        stores[store_id] = {
            "store_id": store_id,
            "driver": "sqlite",
            "endpoint_ref": str(tmp_path / f"{module}.sqlite"),
        }
        module_stores[module] = store_id
    return HostConfiguration(
        configuration_id="pl06-host",
        version=1,
        enabled_modules=tuple(modules),
        stores=stores,
        module_stores=module_stores,
        module_dependencies=dependencies or {},
    )


def test_workflow_host_constructs_memory_jobs_and_applications_without_context(tmp_path) -> None:
    configuration = _workflow_configuration(
        tmp_path, ("access", "memory", "jobs", "applications")
    )
    host = create_workflow_host(configuration)
    assert host.memory is not None
    assert host.jobs is not None
    assert host.applications is not None
    assert host.context is None
    assert not (tmp_path / "context.sqlite").exists()
    assert set(host.enabled_modules) == {"access", "memory", "jobs", "applications"}
    host.close()


def test_workflow_host_constructs_explicit_optional_context(tmp_path) -> None:
    configuration = _workflow_configuration(
        tmp_path,
        ("access", "memory", "context", "applications"),
        dependencies={"context": ("memory",), "applications": ("memory", "context")},
    )
    host = create_workflow_host(configuration)
    assert host.context is not None
    assert host.applications is not None
    assert (tmp_path / "context.sqlite").exists()
    host.close()


def test_workflow_host_rejects_applications_without_memory(tmp_path) -> None:
    configuration = _workflow_configuration(tmp_path, ("access", "applications"))
    with pytest.raises(FoundationError) as missing:
        create_workflow_host(configuration)
    assert missing.value.code == "module_dependency_missing"
