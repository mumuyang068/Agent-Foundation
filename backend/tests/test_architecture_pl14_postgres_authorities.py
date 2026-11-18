from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_foundation.adapters.outbound.postgres_evidence_v2 import PostgresEvidenceAuthority
from agent_foundation.adapters.outbound.postgres_memory_v2 import (
    PostgresMemoryAuthority,
    _PostgresConnectionFacade,
)
from agent_foundation.adapters.outbound.postgres_graph_control_v2 import (
    PostgresGraphControlStore,
)
from agent_foundation.adapters.outbound.relational_memory_v2 import (
    RelationalMemoryAuthority,
)
from agent_foundation.host.configuration import HostConfiguration, StoreConfiguration
from agent_foundation.host import minimal
from agent_foundation.kernel.primitives import ResourceRef, ScopeRef
from agent_foundation.modules.graph.contracts import (
    GraphNode,
    GraphOperation,
    GraphOperationReceipt,
    GraphProposal,
    GraphSchema,
)
from agent_foundation.modules.memory.contracts import MemoryEntry


POSTGRES_DSN = os.environ.get("AF_PL14_POSTGRES_DSN") or os.environ.get("AF_PL09_POSTGRES_DSN")


class _AuthorityStub:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _configuration(module: str) -> HostConfiguration:
    return HostConfiguration(
        configuration_id=f"postgres-{module}",
        version=1,
        enabled_modules=("access", module),
        stores={
            "authority": StoreConfiguration(
                store_id="authority",
                driver="postgres",
                endpoint_ref=f"postgresql:///{module}",
            )
        },
        module_stores={module: "authority"},
    )


def test_host_opens_postgres_authorities_from_each_module_store(monkeypatch) -> None:
    sentinel_connections = []

    def connect(endpoint):
        connection = object()
        sentinel_connections.append((endpoint, connection))
        return connection

    import psycopg

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(minimal, "PostgresEvidenceAuthority", _AuthorityStub)
    monkeypatch.setattr(minimal, "PostgresMemoryAuthority", _AuthorityStub)

    evidence = minimal._evidence_authority(_configuration("evidence"))
    memory = minimal._memory_authority(_configuration("memory"))

    assert sentinel_connections[0][0] == "postgresql:///evidence"
    assert sentinel_connections[1][0] == "postgresql:///memory"
    assert evidence.connection is sentinel_connections[0][1]
    assert memory.connection is sentinel_connections[1][1]
    assert evidence.connection is not memory.connection


def test_host_combines_postgres_graph_control_with_neo4j_write(monkeypatch) -> None:
    control_connection = object()
    graph_driver = object()

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda endpoint: control_connection)
    monkeypatch.setattr(minimal, "PostgresGraphControlStore", _AuthorityStub)

    class WriteStub:
        def __init__(self, driver, *, database):
            self.driver = driver
            self.database = database

        def close(self):
            pass

    monkeypatch.setattr(minimal, "Neo4jGraphWriteAuthority", WriteStub)
    monkeypatch.setitem(
        sys.modules,
        "neo4j",
        SimpleNamespace(
            GraphDatabase=SimpleNamespace(driver=lambda endpoint, auth: graph_driver)
        ),
    )
    configuration = HostConfiguration(
        configuration_id="postgres-control-neo4j-write",
        version=1,
        enabled_modules=("access", "evidence", "graph"),
        stores={
            "control": StoreConfiguration(
                store_id="control",
                driver="postgres",
                endpoint_ref="postgresql:///graph-control",
            ),
            "write": StoreConfiguration(
                store_id="write",
                driver="neo4j",
                endpoint_ref="bolt://graph.invalid:7687",
                database="foundation",
            ),
        },
        module_stores={"graph": "control"},
        module_write_stores={"graph": "write"},
    )

    control, writer = minimal._graph_authorities(configuration)
    try:
        assert control.connection is control_connection
        assert writer.driver is graph_driver
        assert writer.database == "foundation"
    finally:
        control.close()
        writer.close()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "SELECT json_extract(r.payload, '$.status') FROM x WHERE scope_key=? LIMIT ?",
            "SELECT (r.payload::jsonb->>'status') FROM x WHERE scope_key=%s LIMIT %s",
        ),
        (
            "SELECT json_extract(payload, '$.applied_operation_id') FROM x WHERE plan_id=?",
            "SELECT (payload::jsonb->>'applied_operation_id') FROM x WHERE plan_id=%s",
        ),
    ],
)
def test_memory_postgres_dialect_removes_sqlite_specific_syntax(source, expected) -> None:
    assert _PostgresConnectionFacade._translate(source) == expected


def test_postgres_memory_inherits_neutral_relational_behavior() -> None:
    assert PostgresMemoryAuthority.__mro__[1] is RelationalMemoryAuthority
    assert RelationalMemoryAuthority.__module__.endswith("relational_memory_v2")
    assert all("sqlite_memory_v2" not in base.__module__ for base in PostgresMemoryAuthority.__mro__)


def test_postgres_evidence_authority_lifecycle_and_restart() -> None:
    psycopg = pytest.importorskip("psycopg")
    if not POSTGRES_DSN:
        pytest.skip("AF_PL14_POSTGRES_DSN not configured")
    scope = ScopeRef(tenant_id="pl14", space_id=f"evidence-{uuid4().hex}")
    first_connection = psycopg.connect(POSTGRES_DSN)
    authority = PostgresEvidenceAuthority(first_connection)
    try:
        shared_operation_id = f"shared-evidence-operation-{uuid4().hex}"
        first = authority.admit(
            scope,
            source_id="document",
            content="first",
            source_range=None,
            operation_id=f"{scope.space_id}:first",
            command_digest="first",
        )
        retried = authority.admit(
            scope,
            source_id="document",
            content="first",
            source_range=None,
            operation_id=f"{scope.space_id}:first",
            command_digest="first",
        )
        second = authority.admit(
            scope,
            source_id="document",
            content="second",
            source_range=None,
            operation_id=f"{scope.space_id}:second",
            command_digest="second",
        )
        other_scope = ScopeRef(tenant_id="pl14", space_id=f"evidence-{uuid4().hex}")
        other = authority.admit(
            other_scope,
            source_id="document",
            content="other scope",
            source_range=None,
            operation_id=shared_operation_id,
            command_digest="other-scope",
        )
        same_id_here = authority.admit(
            scope,
            source_id="shared-operation-document",
            content="same operation in first scope",
            source_range=None,
            operation_id=shared_operation_id,
            command_digest="first-scope",
        )
        assert retried == first
        assert (first.source_version, second.source_version) == (1, 2)
        assert other.scope == other_scope
        assert same_id_here.scope == scope
        assert not authority.is_current(first.ref())
        assert authority.is_current(second.ref())
        assert authority.get_operation_receipt(scope, f"{scope.space_id}:second").authority_watermark == 2
    finally:
        authority.close()

    reopened = PostgresEvidenceAuthority(psycopg.connect(POSTGRES_DSN))
    try:
        assert reopened.get_revision(scope, second.revision_id) == second
        assert reopened.candidates(scope, source_ids=(), revision_ids=(), limit=8) == (second,)
    finally:
        reopened.close()


def test_postgres_graph_control_lifecycle_and_restart() -> None:
    psycopg = pytest.importorskip("psycopg")
    if not POSTGRES_DSN:
        pytest.skip("AF_PL14_POSTGRES_DSN not configured")
    scope = ScopeRef(tenant_id="pl14", space_id=f"graph-control-{uuid4().hex}")
    source = ResourceRef(
        domain="evidence",
        scope=scope,
        resource_type="evidence_revision",
        resource_id="source",
        revision="source",
    )
    proposal = GraphProposal(
        scope=scope,
        operations=(
            GraphOperation(
                action="put_node",
                node=GraphNode(
                    node_id="node",
                    scope=scope,
                    node_type="fact",
                    label="Node",
                    source_refs=(source,),
                ),
            ),
        ),
        evidence_refs=(source,),
        proposed_by="author",
    )
    authority = PostgresGraphControlStore(psycopg.connect(POSTGRES_DSN))
    try:
        assert authority.save_proposal(
            proposal, operation_id="propose", command_digest="propose"
        ) == proposal
        assert authority.save_proposal(
            proposal, operation_id="propose", command_digest="propose"
        ) == proposal
        approved = proposal.model_copy(
            update={"revision": 2, "status": "approved", "reviewed_by": "reviewer"}
        )
        authority.review_proposal(
            approved,
            expected_revision=1,
            operation_id="review",
            command_digest="review",
        )
        intent = authority.prepare_apply(
            scope,
            proposal.proposal_id,
            expected_revision=2,
            operation_id="apply",
            command_digest="a" * 64,
            write_operation_id="write",
            write_command_digest="w" * 64,
            applied_by="executor",
            grant_fingerprint="g" * 64,
            policy_fingerprint="p" * 64,
        )
        assert authority.get_apply_intent(scope, proposal.proposal_id) == intent
        receipt = GraphOperationReceipt(
            operation_id="write",
            command_digest="w" * 64,
            action="write",
            resource_ids=("node",),
            committed_at=intent.created_at,
            authority_watermark=1,
        )
        applied = authority.complete_apply(
            scope,
            proposal.proposal_id,
            expected_revision=2,
            operation_id="apply",
            command_digest="a" * 64,
            write_receipt=receipt,
        )
        assert (applied.status, applied.revision, applied.applied_by) == (
            "applied",
            3,
            "executor",
        )
        schema = GraphSchema(
            schema_id="facts",
            scope=scope,
            version=1,
            node_types=("fact",),
            relation_types=("related",),
        )
        assert authority.publish_schema(
            schema, operation_id="schema", command_digest="schema"
        ) == schema
    finally:
        authority.close()

    reopened = PostgresGraphControlStore(psycopg.connect(POSTGRES_DSN))
    try:
        assert reopened.get_proposal(scope, proposal.proposal_id) == applied
        assert reopened.get_schema(scope, "facts", 1) == schema
        assert reopened.get_operation_receipt(scope, "apply").resource_ids == (
            proposal.proposal_id,
            "node",
        )
    finally:
        reopened.close()


def test_postgres_memory_authority_lifecycle_and_restart() -> None:
    psycopg = pytest.importorskip("psycopg")
    if not POSTGRES_DSN:
        pytest.skip("AF_PL14_POSTGRES_DSN not configured")
    scope = ScopeRef(tenant_id="pl14", space_id=f"memory-{uuid4().hex}")
    authority = PostgresMemoryAuthority(psycopg.connect(POSTGRES_DSN))
    entry = MemoryEntry(scope=scope, content="candidate")
    try:
        shared_operation_id = f"shared-memory-operation-{uuid4().hex}"
        assert authority.create(entry, operation_id=f"{scope.space_id}:create", command_digest="create") == entry
        assert authority.create(entry, operation_id=f"{scope.space_id}:create", command_digest="create") == entry
        active = entry.model_copy(update={"revision": 2, "status": "active"})
        assert authority.update(
            active,
            expected_revision=1,
            operation_id=f"{scope.space_id}:activate",
            command_digest="activate",
            action="review",
        ) == active
        assert authority.candidates(scope, limit=8) == (active,)
        assert authority.authority_watermark(scope) == 2
        other_scope = ScopeRef(tenant_id="pl14", space_id=f"memory-{uuid4().hex}")
        other = MemoryEntry(scope=other_scope, content="other scope")
        same_id_here = MemoryEntry(scope=scope, content="same operation in first scope")
        assert authority.create(
            other,
            operation_id=shared_operation_id,
            command_digest="other-scope",
        ) == other
        assert authority.create(
            same_id_here,
            operation_id=shared_operation_id,
            command_digest="first-scope",
        ) == same_id_here
    finally:
        authority.close()

    reopened = PostgresMemoryAuthority(psycopg.connect(POSTGRES_DSN))
    try:
        assert reopened.get_current(scope, entry.memory_id) == active
        assert reopened.history(scope, entry.memory_id) == (entry, active)
        assert reopened.get_operation_receipt(scope, f"{scope.space_id}:activate").authority_watermark == 2
    finally:
        reopened.close()
