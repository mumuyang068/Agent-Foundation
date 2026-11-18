from __future__ import annotations

import sqlite3

import pytest

from agent_foundation.adapters.outbound.access_state import InMemoryAccessStateRepository
from agent_foundation.adapters.outbound.filesystem_management_effects_v2 import (
    FilesystemIndexEffect,
    FilesystemReleaseEffect,
    FilesystemTransferEffect,
)
from agent_foundation.adapters.outbound.sqlite_management_v2 import SqliteManagementAuthority
from agent_foundation.execution.extractors import ExtractorRuntime, JsonPointerExtractor
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import Principal, ResourceRef, ScopeRef
from agent_foundation.modules.access.application import AccessApplication
from agent_foundation.modules.access.contracts import AccessGrant
from agent_foundation.modules.management.application import ManagementApplication
from agent_foundation.modules.management.contracts import ExtractorFixture, ManagementPolicy


ACTIONS = (
    "release_stage", "release_approve", "release_activate", "read",
    "extractor_certify", "extractor_revoke", "extractor_execute",
)
RELEASE_KINDS = (
    "domain_policy", "execution_profile", "extractor", "provider",
    "host_configuration", "graph_schema",
)


class RecordingSink:
    def __init__(self, scope: ScopeRef) -> None:
        self.scope = scope
        self.calls: list[tuple[str, object]] = []

    def propose(self, _token, _extractor, *, event_id, value, source_refs):
        self.calls.append((event_id, value))
        return ResourceRef(
            domain="memory", scope=self.scope, resource_type="memory",
            resource_id=f"memory:{event_id}", revision="1",
        )


def _fixture(tmp_path):
    access = AccessApplication(InMemoryAccessStateRepository())
    scope = ScopeRef(tenant_id="tenant", space_id="extractors")
    tokens = {}
    for principal_id in ("stager", "approver", "operator"):
        access.put_grant(AccessGrant(
            grant_id=principal_id, principal_id=principal_id, scope=scope,
            domains=("management",), actions=ACTIONS, limits={"max_items": 100},
        ))
        tokens[principal_id] = access.issue_session(
            Principal(principal_id=principal_id, kind="operator"), principal_id
        ).access_token
    effect_root = tmp_path / "effects"
    implementation = JsonPointerExtractor()
    sink = RecordingSink(scope)
    app = ManagementApplication(
        SqliteManagementAuthority(sqlite3.connect(
            tmp_path / "management.sqlite", check_same_thread=False
        )),
        access,
        ManagementPolicy(policy_id="management", version=1),
        release_effects={
            kind: FilesystemReleaseEffect(effect_root, kind) for kind in RELEASE_KINDS
        },
        index_effect=FilesystemIndexEffect(effect_root),
        transfer_effect=FilesystemTransferEffect(tmp_path / "transfers"),
        extractor_runtime=ExtractorRuntime((implementation,)),
        extraction_sink=sink,
    )
    payload = {
        "extractor_id": "person-name", "version": 1,
        "input_schema": {"type": "object"}, "output_schema": {"type": "string"},
        "implementation_id": implementation.implementation_id,
        "implementation_digest": implementation.implementation_digest,
        "schema_id": "person", "schema_version": 3, "max_input_bytes": 200,
        "configuration": {"path": ["person", "name"], "target_domain": "memory"},
    }
    staged = app.stage_release(
        tokens["stager"], kind="extractor", artifact_id="person-name", version=1,
        payload=payload, operation_id="release-stage",
    )
    approved = app.approve_release(
        tokens["approver"], staged.release_id, expected_revision=staged.revision,
        operation_id="release-approve",
    )
    release = app.activate_release(
        tokens["operator"], staged.release_id, expected_revision=approved.revision,
        operation_id="release-activate",
    )
    return app, tokens, scope, sink, release


def _fixtures():
    return (
        ExtractorFixture(
            fixture_id="candidate", input_payload={"person": {"name": "Ada"}},
            expected_status="candidate", expected_value="Ada",
        ),
        ExtractorFixture(
            fixture_id="none", input_payload={"person": {}},
            expected_status="no_candidate",
        ),
    )


def test_certification_execution_replay_revocation_and_restart(tmp_path) -> None:
    app, tokens, scope, sink, release = _fixture(tmp_path)
    certificate = app.certify_extractor(
        tokens["operator"], release.release_id, fixtures=_fixtures(),
        reason="Conformance fixtures passed", operation_id="certify",
    )
    assert certificate.status == "conformant"
    assert app.get_extractor_certificate(
        tokens["operator"], certificate.certificate_id
    ) == certificate

    source = ResourceRef(
        domain="evidence", scope=scope, resource_type="evidence_revision",
        resource_id="source", revision="1",
    )
    receipt = app.execute_extractor(
        tokens["operator"], release.release_id, event_id="event-1",
        schema_id="person", schema_version=3,
        input_payload={"person": {"name": "Grace"}}, source_refs=(source,),
        operation_id="event-1",
    )
    assert receipt.status == "proposal_created"
    assert receipt.proposal_ref is not None
    assert sink.calls == [("event-1", "Grace")]
    assert app.execute_extractor(
        tokens["operator"], release.release_id, event_id="event-1",
        schema_id="person", schema_version=3,
        input_payload={"person": {"name": "Grace"}}, source_refs=(source,),
        operation_id="event-1",
    ) == receipt
    assert sink.calls == [("event-1", "Grace")]
    with pytest.raises(FoundationError) as conflict:
        app.execute_extractor(
            tokens["operator"], release.release_id, event_id="event-1",
            schema_id="person", schema_version=3,
            input_payload={"person": {"name": "Different"}}, source_refs=(source,),
            operation_id="event-1",
        )
    assert conflict.value.code == "operation_id_conflict"

    revocation = app.revoke_extractor_certificate(
        tokens["operator"], certificate.certificate_id,
        expected_fingerprint=certificate.fingerprint,
        reason="Implementation was withdrawn", operation_id="revoke",
    )
    assert revocation.certificate_id == certificate.certificate_id
    with pytest.raises(FoundationError) as revoked:
        app.execute_extractor(
            tokens["operator"], release.release_id, event_id="event-2",
            schema_id="person", schema_version=3,
            input_payload={"person": {"name": "Katherine"}}, source_refs=(source,),
            operation_id="event-2",
        )
    assert revoked.value.code == "extractor_not_conformant"
    # A completed event remains a stable receipt even after certificate revocation.
    assert app.execute_extractor(
        tokens["operator"], release.release_id, event_id="event-1",
        schema_id="person", schema_version=3,
        input_payload={"person": {"name": "Grace"}}, source_refs=(source,),
        operation_id="event-1",
    ) == receipt
    app.close()


def test_certification_coverage_budget_and_non_candidate_outcomes(tmp_path) -> None:
    app, tokens, scope, sink, release = _fixture(tmp_path)
    with pytest.raises(FoundationError) as coverage:
        app.certify_extractor(
            tokens["operator"], release.release_id,
            fixtures=(_fixtures()[0], _fixtures()[0].model_copy(update={"fixture_id": "two"})),
            reason="Missing no candidate fixture", operation_id="bad-coverage",
        )
    assert coverage.value.code == "extractor_fixture_coverage_incomplete"
    app.certify_extractor(
        tokens["operator"], release.release_id, fixtures=_fixtures(),
        reason="Conformance fixtures passed", operation_id="certify",
    )
    source = ResourceRef(
        domain="evidence", scope=scope, resource_type="evidence_revision",
        resource_id="source", revision="1",
    )
    no_candidate = app.execute_extractor(
        tokens["operator"], release.release_id, event_id="event-none",
        schema_id="person", schema_version=3, input_payload={"person": {}},
        source_refs=(source,), operation_id="event-none",
    )
    assert no_candidate.status == "no_candidate"
    assert not sink.calls
    invalid = app.execute_extractor(
        tokens["operator"], release.release_id, event_id="event-invalid",
        schema_id="person", schema_version=3,
        input_payload={"person": {"name": {"nested": True}}},
        source_refs=(source,), operation_id="event-invalid",
    )
    assert invalid.status == "invalid"
    assert not sink.calls
    with pytest.raises(FoundationError) as budget:
        app.execute_extractor(
            tokens["operator"], release.release_id, event_id="event-large",
            schema_id="person", schema_version=3,
            input_payload={"person": {"name": "x" * 400}}, source_refs=(source,),
            operation_id="event-large",
        )
    assert budget.value.code == "extractor_input_budget_exceeded"
    app.close()
