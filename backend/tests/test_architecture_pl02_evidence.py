from __future__ import annotations

import sqlite3

import pytest

from agent_foundation.adapters.outbound.access_state import SqliteAccessStateRepository
from agent_foundation.adapters.outbound.sqlite_evidence_v2 import SqliteEvidenceAuthority
from agent_foundation.adapters.outbound.projected_candidates_v2 import (
    AuthorityEvidenceCandidateSource,
)
from agent_foundation.algorithms.evidence_v2 import LexicalEvidenceCandidates
from agent_foundation.execution.contracts import ExecutionProfile
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import Principal, ResourceRef, ScopeRef
from agent_foundation.modules.access.application import AccessApplication
from agent_foundation.modules.access.contracts import AccessGrant
from agent_foundation.modules.evidence.application import EvidenceApplication
from agent_foundation.modules.evidence.contracts import EvidencePolicy, EvidenceRevision, SourceRange


@pytest.fixture
def evidence_system(tmp_path):
    access_db = tmp_path / "access.sqlite3"
    evidence_db = tmp_path / "evidence.sqlite3"
    access = AccessApplication(SqliteAccessStateRepository(sqlite3.connect(access_db, check_same_thread=False)))
    scope = ScopeRef(tenant_id="tenant", space_id="evidence")
    access.put_grant(AccessGrant(
        grant_id="evidence-grant",
        principal_id="service",
        scope=scope,
        domains=("evidence",),
        actions=("admit", "read", "search", "revoke", "revalidate", "support"),
        limits={"max_items": 10},
    ))
    issued = access.issue_session(Principal(principal_id="service", kind="service"), "evidence-grant")
    policy = EvidencePolicy(policy_id="evidence", version=1)
    profile = ExecutionProfile(
        profile_id="evidence-search",
        version=1,
        domain="evidence",
        algorithm_id="evidence.lexical-v2",
        implementation_version="1",
        budget={"max_items": 8},
    )

    def create(algorithm=None, *, candidate_source=None):
        authority = SqliteEvidenceAuthority(sqlite3.connect(evidence_db, check_same_thread=False))
        return EvidenceApplication(
            authority,
            access,
            policy,
            profile,
            algorithm or LexicalEvidenceCandidates(),
            candidate_source=candidate_source or AuthorityEvidenceCandidateSource(authority),
        )

    app = create()
    try:
        yield app, create, issued.access_token, scope, evidence_db
    finally:
        app.close()
        access.close()


def test_evidence_versions_exact_read_and_restart(evidence_system) -> None:
    app, create, token, _scope, _database = evidence_system
    first = app.admit(token, source_id="document", content="alpha first", operation_id="admit-1")
    second = app.admit(
        token,
        source_id="document",
        content="alpha second",
        source_range=SourceRange(start=0, end=12),
        operation_id="admit-2",
    )
    assert (first.source_version, second.source_version) == (1, 2)
    assert app.get(token, first.revision_id).content == "alpha first"
    assert not app.is_current(first.ref())
    assert app.is_current(second.ref())
    app.close()
    reopened = create()
    try:
        assert reopened.get(token, second.revision_id).source_range == SourceRange(start=0, end=12)
        assert reopened.search(token, query="second").hits[0].revision.revision_id == second.revision_id
    finally:
        reopened.close()


def test_evidence_operation_identity_is_scoped() -> None:
    authority = SqliteEvidenceAuthority(sqlite3.connect(":memory:"))
    first_scope = ScopeRef(tenant_id="tenant", space_id="project-a")
    second_scope = ScopeRef(tenant_id="tenant", space_id="project-b")
    try:
        first = authority.admit(
            first_scope,
            source_id="document",
            content="first project",
            source_range=None,
            operation_id="same-import",
            command_digest="first-command",
        )
        second = authority.admit(
            second_scope,
            source_id="document",
            content="second project",
            source_range=None,
            operation_id="same-import",
            command_digest="second-command",
        )
        assert first.scope == first_scope
        assert second.scope == second_scope
    finally:
        authority.close()


def test_source_scope_is_applied_before_candidate_algorithm(evidence_system) -> None:
    app, _create, token, _scope, _database = evidence_system
    selected = app.admit(token, source_id="selected", content="needle selected", operation_id="selected")
    app.admit(token, source_id="outside", content="needle outside", operation_id="outside")
    seen = []

    class InspectingAlgorithm(LexicalEvidenceCandidates):
        def rank(self, revisions, *, query, limit):
            seen.extend(item.source_id for item in revisions)
            return super().rank(revisions, query=query, limit=limit)

    app._algorithm = InspectingAlgorithm()
    result = app.search(token, query="needle", source_ids=("selected",))
    assert seen == ["selected"]
    assert [hit.revision.revision_id for hit in result.hits] == [selected.revision_id]
    assert app.search(token, query="needle", source_ids=("missing",)).hits == ()


def test_revocation_during_candidate_ranking_is_filtered(evidence_system) -> None:
    app, _create, token, _scope, _database = evidence_system
    revision = app.admit(token, source_id="source", content="alpha", operation_id="admit")

    class RevokingAlgorithm(LexicalEvidenceCandidates):
        def rank(self, revisions, *, query, limit):
            ranked = super().rank(revisions, query=query, limit=limit)
            app.revoke(token, revision.revision_id, operation_id="during-ranking-revoke")
            return ranked

    app._algorithm = RevokingAlgorithm()
    assert app.search(token, query="alpha").hits == ()


def test_candidate_only_source_is_replaceable_and_authority_canonicalizes_output(evidence_system) -> None:
    app, create, token, scope, _database = evidence_system
    revision = app.admit(token, source_id="selected", content="authority value", operation_id="source")

    class CandidateOnly:
        def candidates(
            self, candidate_scope, *, query, source_ids, revision_ids, limit
        ):
            assert candidate_scope == scope
            assert query == "value"
            assert source_ids == ("selected",)
            assert revision_ids == ()
            assert limit == 4
            foreign = revision.ref().model_copy(update={
                "scope": ScopeRef(tenant_id="other", space_id=scope.space_id),
            })
            return revision.ref(), foreign

    reader = create(candidate_source=CandidateOnly())
    try:
        result = reader.search(token, query="value", limit=1, source_ids=("selected",))
        assert [hit.revision.content for hit in result.hits] == ["authority value"]
    finally:
        reader.close()


def test_operation_id_retries_are_idempotent_and_conflicts_are_rejected(evidence_system) -> None:
    app, _create, token, _scope, _database = evidence_system
    first = app.admit(token, source_id="source", content="same", operation_id="stable-op")
    retried = app.admit(token, source_id="source", content="same", operation_id="stable-op")
    assert retried == first
    with pytest.raises(FoundationError) as conflict:
        app.admit(token, source_id="source", content="different", operation_id="stable-op")
    assert conflict.value.code == "operation_id_conflict"


def test_support_becomes_unavailable_when_its_evidence_is_revoked(evidence_system) -> None:
    app, _create, token, scope, _database = evidence_system
    revision = app.admit(token, source_id="source", content="support", operation_id="admit-support")
    support = app.support(
        token,
        target=ResourceRef(
            domain="graph", scope=scope, resource_type="graph_node", resource_id="node", revision="1"
        ),
        evidence_refs=(revision.ref(),),
        operation_id="support",
    )
    assert app.get_support(token, support.support_id) == support
    app.revoke(token, revision.revision_id, operation_id="revoke-support-source")
    with pytest.raises(FoundationError) as stale:
        app.get_support(token, support.support_id)
    assert stale.value.code == "evidence_support_stale"


def test_cross_scope_exact_read_does_not_leak(evidence_system) -> None:
    app, _create, token, scope, _database = evidence_system
    revision = app.admit(token, source_id="source", content="private", operation_id="private")
    wrong = ResourceRef(
        domain="evidence",
        scope=ScopeRef(tenant_id=scope.tenant_id, space_id="other"),
        resource_type="evidence_revision",
        resource_id=revision.revision_id,
        revision=revision.revision_id,
    )
    assert not app.is_current(wrong)


def test_revision_listing_is_paginated_bound_and_snapshot_consistent(evidence_system) -> None:
    app, _create, token, _scope, _database = evidence_system
    first = app.admit(token, source_id="document", content="one", operation_id="page-1")
    second = app.admit(token, source_id="document", content="two", operation_id="page-2")
    third = app.admit(token, source_id="other", content="three", operation_id="page-3")

    page = app.list(token, states=("active", "superseded"), limit=1)
    assert page.items == (first.model_copy(update={"status": "superseded"}),)
    assert page.next_cursor is not None
    next_page = app.list(
        token,
        states=("superseded", "active"),
        limit=1,
        cursor=page.next_cursor,
    )
    assert next_page.items == (second,)
    assert app.list(token, source_id="other", limit=2).items == (third,)

    with pytest.raises(FoundationError) as mismatch:
        app.list(token, source_id="other", limit=1, cursor=page.next_cursor)
    assert mismatch.value.code == "evidence_cursor_invalid"

    app.admit(token, source_id="later", content="four", operation_id="page-4")
    with pytest.raises(FoundationError) as stale:
        app.list(token, states=("active", "superseded"), limit=1, cursor=page.next_cursor)
    assert stale.value.code == "evidence_cursor_stale"


def test_operation_receipt_and_watermark_are_durable_and_scope_bound(evidence_system) -> None:
    app, create, token, scope, _database = evidence_system
    assert app.retrieval_watermark(token) == 0
    revision = app.admit(token, source_id="receipt", content="receipt", operation_id="receipt-op")
    receipt = app.get_operation_receipt(token, "receipt-op")
    assert receipt.resource_id == revision.revision_id
    assert receipt.action == "admit"
    assert receipt.authority_watermark == app.retrieval_watermark(token) == 1

    other_scope = ScopeRef(tenant_id=scope.tenant_id, space_id="other")
    app._access.put_grant(AccessGrant(
        grant_id="other-grant",
        principal_id="other-service",
        scope=other_scope,
        domains=("evidence",),
        actions=("read",),
        limits={"max_items": 10},
    ))
    other_token = app._access.issue_session(
        Principal(principal_id="other-service", kind="service"), "other-grant"
    ).access_token
    with pytest.raises(FoundationError) as hidden:
        app.get_operation_receipt(other_token, "receipt-op")
    assert hidden.value.code == "evidence_receipt_not_found"

    app.close()
    reopened = create()
    try:
        assert reopened.get_operation_receipt(token, "receipt-op") == receipt
        assert reopened.retrieval_watermark(token) == 1
    finally:
        reopened.close()


def test_retrieval_status_identifies_algorithm_configuration_and_authority(evidence_system) -> None:
    app, _create, token, _scope, _database = evidence_system
    before = app.retrieval_status(token)
    assert before.state == "ready"
    assert before.algorithm.algorithm_id == "evidence.lexical-v2"
    assert before.authority_watermark == 0
    app.admit(token, source_id="status", content="status", operation_id="status-op")
    after = app.retrieval_status(token)
    assert after.policy_fingerprint == before.policy_fingerprint
    assert after.execution_profile_fingerprint == before.execution_profile_fingerprint
    assert after.authority_watermark == 1
def test_lexical_evidence_candidates_match_short_cjk_query_inside_sentence():
    algorithm = LexicalEvidenceCandidates()
    revision = EvidenceRevision(
        scope=ScopeRef(tenant_id="tenant", space_id="space"),
        source_id="lesson",
        source_version=1,
        content="学生容易把分子与分母的位置混淆",
        status="active",
    )
    assert algorithm.rank((revision,), query="分子 分母", limit=8)[0][0] == revision.revision_id
