from __future__ import annotations

import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_foundation.adapters.outbound.access_state import InMemoryAccessStateRepository
from agent_foundation.adapters.outbound.sqlite_context_v2 import SqliteContextAuthority
from agent_foundation.algorithms.context_v2 import (
    InterleavedContextCombination,
    ScoreContextCombination,
)
from agent_foundation.execution.contracts import ExecutionProfile
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import Principal, ResourceRef, ScopeRef
from agent_foundation.modules.access.application import AccessApplication
from agent_foundation.modules.access.contracts import AccessGrant
from agent_foundation.modules.context import ContextCandidate, ContextPolicy
from agent_foundation.modules.context.application import ContextApplication
from agent_foundation.host.configuration import HostConfiguration, StoreConfiguration
from agent_foundation.host.minimal import create_context_host
from agent_foundation.host.service_v2 import build_app


class MutableSource:
    def __init__(self, domain: str, values: list[ContextCandidate]) -> None:
        self.domain = domain
        self.values = values
        self.after_search = None

    def search(self, token: str, *, query: str, limit: int):
        del token, query
        result = tuple(self.values[:limit])
        if self.after_search is not None:
            self.after_search()
        return result

    def resolve(self, token: str, ref: ResourceRef):
        del token
        for item in self.values:
            if item.ref == ref:
                return item
        raise FoundationError("context_resource_not_found", "Resource is no longer current")


def _system(tmp_path, *, sources=(), algorithm=None, profile=None):
    access = AccessApplication(InMemoryAccessStateRepository())
    scope = ScopeRef(tenant_id="tenant", space_id="space")
    actions = (
        "assemble", "snapshot", "snapshot_read", "resident_read", "resident_replace"
    )
    tokens = {}
    for name in ("alice", "bob"):
        grant = AccessGrant(
            grant_id=f"grant-{name}", principal_id=name, scope=scope,
            domains=("context",), actions=actions,
            limits={"max_items": 8, "max_chars": 200},
        )
        access.put_grant(grant)
        tokens[name] = access.issue_session(
            Principal(principal_id=name, kind="user"), grant.grant_id
        ).access_token
    selected_profile = profile or ExecutionProfile(
        profile_id="context-combine", version=1, domain="context",
        algorithm_id="context.score-v2", implementation_version="1",
        budget={"max_items": 6, "max_chars": 120},
    )
    authority = SqliteContextAuthority(sqlite3.connect(tmp_path / "context.sqlite"))
    app = ContextApplication(
        authority, access, ContextPolicy(policy_id="context", version=1),
        selected_profile, algorithm or ScoreContextCombination(), tuple(sources),
    )
    return app, tokens, scope


def _candidate(domain: str, scope: ScopeRef, resource_id: str, content: str, score: float):
    return ContextCandidate(
        ref=ResourceRef(
            domain=domain, scope=scope, resource_type=f"{domain}_item",
            resource_id=resource_id, revision="1",
        ),
        content=content,
        score=score,
    )


def test_no_source_runtime_manages_snapshot_metadata_but_assemble_requires_selection(tmp_path) -> None:
    app, tokens, _scope = _system(tmp_path)
    with pytest.raises(FoundationError) as missing:
        app.assemble(tokens["alice"], query="q", source_domains=())
    assert missing.value.code == "context_sources_required"
    digest = hashlib.sha256(b"empty").hexdigest()
    snapshot = app.create_snapshot_metadata(
        tokens["alice"], assembly_fingerprint=digest, operation_id="metadata-1"
    )
    assert app.get_snapshot(tokens["alice"], snapshot.snapshot_id) == snapshot
    assert app.list_snapshots(tokens["alice"], limit=8).items == (snapshot,)
    app.close()


def test_single_source_combination_and_budget_never_expand(tmp_path) -> None:
    scope = ScopeRef(tenant_id="tenant", space_id="space")
    memory = MutableSource("memory", [
        _candidate("memory", scope, "m1", "alpha memory", 0.9),
        _candidate("memory", scope, "m2", "beta memory", 0.7),
    ])
    app, tokens, _scope = _system(tmp_path, sources=(memory,))
    result = app.assemble(
        tokens["alice"], query="alpha", source_domains=("memory",), max_items=1, max_chars=12
    )
    assert len(result.items) == 1
    assert len(result.model_context) > len(result.items[0].content)
    assert len(result.items[0].content) == 12
    with pytest.raises(FoundationError) as budget:
        app.assemble(tokens["alice"], query="q", source_domains=("memory",), max_items=9)
    assert budget.value.code == "context_budget_exceeded"
    app.close()


def test_multiple_sources_and_second_combination_algorithm(tmp_path) -> None:
    scope = ScopeRef(tenant_id="tenant", space_id="space")
    memory = MutableSource("memory", [
        _candidate("memory", scope, "m1", "memory", 0.9),
        _candidate("memory", scope, "m2", "memory two", 0.8),
    ])
    evidence = MutableSource("evidence", [
        _candidate("evidence", scope, "e1", "evidence", 0.7),
    ])
    profile = ExecutionProfile(
        profile_id="context-interleaved", version=1, domain="context",
        algorithm_id="context.interleaved-v2", implementation_version="1",
        budget={"max_items": 6, "max_chars": 120},
    )
    app, tokens, _scope = _system(
        tmp_path, sources=(memory, evidence), algorithm=InterleavedContextCombination(), profile=profile
    )
    result = app.assemble(
        tokens["alice"], query="q", source_domains=("memory", "evidence"), max_items=3,
        max_chars=120,
    )
    assert [item.ref.domain for item in result.items[:2]] == ["evidence", "memory"]
    app.close()


def test_candidate_and_pin_are_resolved_after_selection(tmp_path) -> None:
    scope = ScopeRef(tenant_id="tenant", space_id="space")
    item = _candidate("memory", scope, "m1", "memory", 1.0)
    source = MutableSource("memory", [item])
    source.after_search = source.values.clear
    app, tokens, _scope = _system(tmp_path, sources=(source,))
    assert app.assemble(tokens["alice"], query="q", source_domains=("memory",)).items == ()
    with pytest.raises(FoundationError) as missing:
        app.assemble(
            tokens["alice"], query="q", source_domains=("memory",), pinned_refs=(item.ref,)
        )
    assert missing.value.code == "context_resource_not_found"
    app.close()


def test_snapshot_replay_reauthorizes_and_rejects_source_invalidation(tmp_path) -> None:
    scope = ScopeRef(tenant_id="tenant", space_id="space")
    item = _candidate("memory", scope, "m1", "memory", 1.0)
    source = MutableSource("memory", [item])
    app, tokens, _scope = _system(tmp_path, sources=(source,))
    assembly = app.assemble(tokens["alice"], query="q", source_domains=("memory",))
    snapshot = app.create_snapshot(
        tokens["alice"], assembly, retention="provenance_only", operation_id="snapshot-1"
    )
    assert app.replay(tokens["alice"], snapshot.snapshot_id).items[0].ref == item.ref
    source.values.clear()
    with pytest.raises(FoundationError) as stale:
        app.replay(tokens["alice"], snapshot.snapshot_id)
    assert stale.value.code == "context_resource_not_found"
    app.close()


def test_cross_principal_assembly_cannot_be_snapshotted_or_resident(tmp_path) -> None:
    scope = ScopeRef(tenant_id="tenant", space_id="space")
    source = MutableSource("memory", [_candidate("memory", scope, "m1", "memory", 1.0)])
    app, tokens, _scope = _system(tmp_path, sources=(source,))
    assembly = app.assemble(tokens["alice"], query="q", source_domains=("memory",))
    with pytest.raises(FoundationError) as denied:
        app.create_snapshot(tokens["bob"], assembly, operation_id="cross-principal")
    assert denied.value.code == "context_assembly_authority_invalid"
    with pytest.raises(FoundationError):
        app.replace_resident(
            tokens["bob"], assembly, expected_revision=None, operation_id="cross-resident"
        )
    app.close()


def test_snapshot_and_resident_are_persistent_idempotent_and_versioned(tmp_path) -> None:
    scope = ScopeRef(tenant_id="tenant", space_id="space")
    source = MutableSource("memory", [_candidate("memory", scope, "m1", "memory", 1.0)])
    app, tokens, _scope = _system(tmp_path, sources=(source,))
    assembly = app.assemble(tokens["alice"], query="q", source_domains=("memory",))
    snapshot = app.create_snapshot(tokens["alice"], assembly, operation_id="snapshot-persist")
    assert app.create_snapshot(tokens["alice"], assembly, operation_id="snapshot-persist") == snapshot
    first = app.replace_resident(
        tokens["alice"], assembly, expected_revision=None, operation_id="resident-1"
    )
    assert first.revision == 1
    assert app.replace_resident(
        tokens["alice"], assembly, expected_revision=None, operation_id="resident-1"
    ) == first
    assert app.replay_resident(tokens["alice"]).items[0].ref == assembly.items[0].ref
    with pytest.raises(FoundationError) as conflict:
        app.replace_resident(
            tokens["alice"], assembly, expected_revision=None, operation_id="resident-2"
        )
    assert conflict.value.code == "context_revision_conflict"
    assert app.get_operation_receipt(tokens["alice"], "snapshot-persist").resource_id == snapshot.snapshot_id
    app.close()


def test_context_host_enforces_required_sources_and_context_only_store(tmp_path) -> None:
    context_db = tmp_path / "context-only.sqlite"
    configuration = HostConfiguration(
        configuration_id="context-only", version=1,
        enabled_modules=("access", "context"),
        stores={
            "context": StoreConfiguration(
                store_id="context", driver="sqlite", endpoint_ref=str(context_db)
            )
        },
        module_stores={"context": "context"},
    )
    host = create_context_host(configuration)
    assert host.context.source_domains == ()
    host.close()
    tables = {
        row[0] for row in sqlite3.connect(context_db).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "af_context_snapshots" in tables
    assert not any(name.startswith("af_memory_") or name.startswith("af_graph_") for name in tables)

    missing = configuration.model_copy(update={
        "configuration_id": "context-missing-memory",
        "module_dependencies": {"context": ("memory",)},
        "fingerprint": "",
    })
    with pytest.raises(FoundationError) as error:
        create_context_host(missing)
    assert error.value.code == "module_dependency_missing"


def test_real_memory_source_uses_same_session_and_exact_revision(tmp_path) -> None:
    configuration = HostConfiguration(
        configuration_id="context-memory", version=1,
        enabled_modules=("access", "memory", "context"),
        stores={
            "access": StoreConfiguration(
                store_id="access", driver="sqlite", endpoint_ref=str(tmp_path / "access.sqlite")
            ),
            "memory": StoreConfiguration(
                store_id="memory", driver="sqlite", endpoint_ref=str(tmp_path / "memory.sqlite")
            ),
            "context": StoreConfiguration(
                store_id="context", driver="sqlite", endpoint_ref=str(tmp_path / "context.sqlite")
            ),
        },
        module_stores={"access": "access", "memory": "memory", "context": "context"},
        module_dependencies={"context": ("memory",)},
    )
    host = create_context_host(configuration)
    scope = ScopeRef(tenant_id="tenant", space_id="space")
    grant = AccessGrant(
        grant_id="combined", principal_id="alice", scope=scope,
        domains=("memory", "context"),
        actions=(
            "propose", "review", "read", "assemble", "snapshot", "snapshot_read",
            "resident_read", "resident_replace",
        ),
        limits={"max_items": 8, "max_chars": 1_000},
    )
    host.access.put_grant(grant)
    token = host.access.issue_session(Principal(principal_id="alice", kind="user"), grant.grant_id).access_token
    candidate = host.memory.propose(token, content="durable teaching decision", operation_id="memory-propose")
    active = host.memory.review(
        token, candidate.memory_id, approve=True, expected_revision=1, operation_id="memory-review"
    )
    assembly = host.context.assemble(token, query="teaching", source_domains=("memory",))
    assert assembly.items[0].ref == active.ref()
    snapshot = host.context.create_snapshot(token, assembly, operation_id="combined-snapshot")
    host.close()

    restarted = create_context_host(configuration)
    assert restarted.context.get_snapshot(token, snapshot.snapshot_id) == snapshot
    assert restarted.context.replay(token, snapshot.snapshot_id).items[0].ref == active.ref()
    restarted.close()


def test_context_http_uses_thin_public_routes(tmp_path) -> None:
    configuration = HostConfiguration(
        configuration_id="context-http", version=1,
        enabled_modules=("access", "memory", "context"),
        stores={
            name: StoreConfiguration(
                store_id=name, driver="sqlite", endpoint_ref=str(tmp_path / f"{name}.sqlite")
            )
            for name in ("access", "memory", "context")
        },
        module_stores={name: name for name in ("access", "memory", "context")},
        module_dependencies={"context": ("memory",)},
    )
    path = tmp_path / "host.json"
    path.write_text(configuration.model_dump_json(), encoding="utf-8")
    scope = {"tenant_id": "tenant", "space_id": "space", "project_id": None}
    grant = {
        "grant_id": "http-grant", "principal_id": "alice", "scope": scope,
        "domains": ["memory", "context"],
        "actions": [
            "propose", "review", "read", "assemble", "snapshot", "snapshot_read",
            "resident_read", "resident_replace",
        ],
        "limits": {"max_items": 8, "max_chars": 1000},
    }
    with TestClient(build_app(path, bootstrap_token="bootstrap")) as client:
        admin = {"Authorization": "Bearer bootstrap"}
        assert client.post("/api/foundation/v1/access/grants", json=grant, headers=admin).status_code == 200
        session = client.post(
            "/api/foundation/v1/access/sessions",
            json={"principal_id": "alice", "principal_kind": "user", "grant_id": "http-grant"},
            headers=admin,
        ).json()
        headers = {"Authorization": f"Bearer {session['access_token']}"}
        memory = client.post(
            "/api/foundation/v1/memory/entries",
            json={"operation_id": "http-memory", "content": "HTTP context source", "source_refs": []},
            headers=headers,
        ).json()
        reviewed = client.post(
            f"/api/foundation/v1/memory/entries/{memory['memory_id']}/review",
            json={"operation_id": "http-review", "expected_revision": 1, "approve": True},
            headers=headers,
        )
        assert reviewed.status_code == 200
        assembled = client.post(
            "/api/foundation/v1/context/assemblies",
            json={"query": "HTTP", "source_domains": ["memory"]}, headers=headers,
        )
        assert assembled.status_code == 200, assembled.text
        assembly = assembled.json()
        assert assembly["items"][0]["ref"]["resource_id"] == memory["memory_id"]
        snapshot = client.post(
            "/api/foundation/v1/context/snapshots",
            json={"operation_id": "http-snapshot", "assembly": assembly}, headers=headers,
        )
        assert snapshot.status_code == 200, snapshot.text
        replay = client.post(
            f"/api/foundation/v1/context/snapshots/{snapshot.json()['snapshot_id']}/replay",
            headers=headers,
        )
        assert replay.status_code == 200, replay.text
