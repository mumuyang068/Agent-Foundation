from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from agent_foundation.adapters.outbound.access_state import InMemoryAccessStateRepository
from agent_foundation.adapters.outbound.filesystem_management_effects_v2 import (
    FilesystemIndexEffect,
    FilesystemReleaseEffect,
    FilesystemTransferEffect,
)
from agent_foundation.adapters.outbound.sqlite_management_v2 import SqliteManagementAuthority
from agent_foundation.host.backup_v2 import create_sqlite_backup, restore_sqlite_backup
from agent_foundation.host.configuration import HostConfiguration
from agent_foundation.host.minimal import create_management_host
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import Principal, ResourceRef, ScopeRef
from agent_foundation.modules.access.application import AccessApplication
from agent_foundation.modules.access.contracts import AccessGrant
from agent_foundation.modules.management.application import ManagementApplication
from agent_foundation.modules.management.contracts import ManagementPolicy


ACTIONS = (
    "release_stage", "release_approve", "release_activate", "read",
    "index_build", "index_verify", "index_activate", "evaluate",
    "transfer_plan", "transfer_approve", "transfer_execute", "diagnose",
    "backup", "restore",
)
RELEASE_KINDS = (
    "domain_policy", "execution_profile", "extractor", "provider",
    "host_configuration", "graph_schema",
)


def _fixture(tmp_path, *, index_effect=None, transfer_effect=None, release_effects=None):
    access = AccessApplication(InMemoryAccessStateRepository())
    scope = ScopeRef(tenant_id="tenant", space_id="management")
    tokens = {}
    for principal_id in ("stager", "approver", "operator"):
        grant = AccessGrant(
            grant_id=principal_id, principal_id=principal_id, scope=scope,
            domains=("management",), actions=ACTIONS, limits={"max_items": 100},
        )
        access.put_grant(grant)
        tokens[principal_id] = access.issue_session(
            Principal(principal_id=principal_id, kind="operator"), principal_id
        ).access_token
    effects_root = tmp_path / "effects"
    effects = release_effects or {
        kind: FilesystemReleaseEffect(effects_root, kind) for kind in RELEASE_KINDS
    }
    database = tmp_path / "management.sqlite"

    def create():
        return ManagementApplication(
            SqliteManagementAuthority(sqlite3.connect(database, check_same_thread=False)),
            access,
            ManagementPolicy(policy_id="management", version=1),
            release_effects=effects,
            index_effect=index_effect or FilesystemIndexEffect(effects_root),
            transfer_effect=transfer_effect or FilesystemTransferEffect(tmp_path / "transfers"),
        )

    return create, access, tokens, scope, effects_root


def _host_payload(version: int):
    return {
        "configuration_id": "runtime", "version": version,
        "enabled_modules": ["access", "memory"],
    }


def _active_release(app, tokens, version: int, *, prefix: str):
    staged = app.stage_release(
        tokens["stager"], kind="host_configuration", artifact_id="runtime",
        version=version, payload=_host_payload(version), operation_id=f"{prefix}-stage",
    )
    approved = app.approve_release(
        tokens["approver"], staged.release_id, expected_revision=staged.revision,
        operation_id=f"{prefix}-approve",
    )
    return app.activate_release(
        tokens["operator"], staged.release_id, expected_revision=approved.revision,
        operation_id=f"{prefix}-activate",
    )


def test_release_approval_activation_restart_and_atomic_replacement(tmp_path) -> None:
    create, _access, tokens, _scope, effects_root = _fixture(tmp_path)
    app = create()
    first = _active_release(app, tokens, 1, prefix="one")
    assert first.status == "active"
    pointer = effects_root / "active/host_configuration/runtime.json"
    assert json.loads(pointer.read_text())["release_id"] == first.release_id
    app.close()

    restarted = create()
    assert restarted.get_active_release(
        tokens["operator"], kind="host_configuration", artifact_id="runtime"
    ).release_id == first.release_id
    second = _active_release(restarted, tokens, 2, prefix="two")
    assert second.status == "active"
    assert restarted.get_release(tokens["operator"], first.release_id).status == "retired"
    releases = restarted.list_releases(
        tokens["operator"], kind="host_configuration", limit=10
    )["items"]
    assert {release.release_id for release in releases} == {first.release_id, second.release_id}
    assert json.loads(pointer.read_text())["release_id"] == second.release_id
    restarted.close()


def test_failed_release_effect_keeps_previous_runtime_active(tmp_path) -> None:
    class ToggleEffect:
        def __init__(self, inner):
            self.inner = inner
            self.fail = False
        def validate(self, release):
            self.inner.validate(release)
        def activate(self, release, *, operation_id):
            if self.fail:
                raise FoundationError("activation_injected", "injected", retryable=True)
            return self.inner.activate(release, operation_id=operation_id)

    root = tmp_path / "effects"
    toggle = ToggleEffect(FilesystemReleaseEffect(root, "host_configuration"))
    effects = {kind: FilesystemReleaseEffect(root, kind) for kind in RELEASE_KINDS}
    effects["host_configuration"] = toggle
    create, _access, tokens, _scope, _ = _fixture(tmp_path, release_effects=effects)
    app = create()
    active = _active_release(app, tokens, 1, prefix="stable")
    staged = app.stage_release(
        tokens["stager"], kind="host_configuration", artifact_id="runtime", version=2,
        payload=_host_payload(2), operation_id="failed-stage",
    )
    approved = app.approve_release(
        tokens["approver"], staged.release_id, expected_revision=1,
        operation_id="failed-approve",
    )
    toggle.fail = True
    with pytest.raises(FoundationError) as failed:
        app.activate_release(
            tokens["operator"], staged.release_id, expected_revision=approved.revision,
            operation_id="failed-activate",
        )
    assert failed.value.code == "activation_injected"
    assert app.get_active_release(
        tokens["operator"], kind="host_configuration", artifact_id="runtime"
    ).release_id == active.release_id
    assert app.get_release(tokens["operator"], staged.release_id).status == "failed"
    app.close()


def test_index_verification_failure_never_replaces_active_generation(tmp_path) -> None:
    create, _access, tokens, _scope, effects_root = _fixture(tmp_path)
    app = create()
    artifact = tmp_path / "index-one.bin"
    artifact.write_bytes(b"index-one")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    first = app.begin_index(
        tokens["stager"], domain="graph", execution_profile_fingerprint="a" * 64,
        source_revision="graph:1", artifact_path=str(artifact), artifact_digest=digest,
        operation_id="index-one-begin",
    )
    verified = app.verify_index(
        tokens["operator"], first.generation_id, expected_revision=1,
        operation_id="index-one-verify",
    )
    active = app.activate_index(
        tokens["operator"], first.generation_id, expected_revision=verified.revision,
        operation_id="index-one-activate",
    )
    broken = tmp_path / "index-two.bin"
    broken.write_bytes(b"index-two")
    second = app.begin_index(
        tokens["stager"], domain="graph", execution_profile_fingerprint="b" * 64,
        source_revision="graph:2", artifact_path=str(broken), artifact_digest="0" * 64,
        operation_id="index-two-begin",
    )
    with pytest.raises(FoundationError) as mismatch:
        app.verify_index(
            tokens["operator"], second.generation_id, expected_revision=1,
            operation_id="index-two-verify",
        )
    assert mismatch.value.code == "index_artifact_digest_mismatch"
    assert app.get_active_index(tokens["operator"], "graph").generation_id == active.generation_id
    assert {item.generation_id for item in app.list_indexes(
        tokens["operator"], domain="graph", limit=10
    )["items"]} == {first.generation_id, second.generation_id}
    pointer = json.loads((effects_root / "active-indexes/graph.json").read_text())
    assert pointer["generation_id"] == active.generation_id
    app.close()


def test_failed_transfer_can_be_reapproved_and_effect_is_idempotent(tmp_path) -> None:
    class FailOnce:
        def __init__(self, inner):
            self.inner = inner
            self.failed = False
        def execute(self, plan, *, operation_id):
            if not self.failed:
                self.failed = True
                raise FoundationError("transfer_unavailable", "injected", retryable=True)
            return self.inner.execute(plan, operation_id=operation_id)

    transfer = FailOnce(FilesystemTransferEffect(tmp_path / "transfers"))
    create, _access, tokens, scope, _effects = _fixture(tmp_path, transfer_effect=transfer)
    app = create()
    ref = ResourceRef(
        domain="memory", scope=scope, resource_type="memory", resource_id="m1", revision="2"
    )
    plan = app.propose_transfer(
        tokens["stager"], kind="handoff", destination="accepted/handoff.json",
        resource_refs=(ref,), payload={"content": "bounded export"}, operation_id="transfer-plan",
    )
    approved = app.approve_transfer(
        tokens["approver"], plan.transfer_id, expected_revision=1,
        operation_id="transfer-approve",
    )
    with pytest.raises(FoundationError):
        app.execute_transfer(
            tokens["operator"], plan.transfer_id, expected_revision=approved.revision,
            operation_id="transfer-first",
        )
    failed = app.get_transfer(tokens["operator"], plan.transfer_id)
    recovered = app.approve_transfer(
        tokens["approver"], plan.transfer_id, expected_revision=failed.revision,
        operation_id="transfer-reapprove",
    )
    completed = app.execute_transfer(
        tokens["operator"], plan.transfer_id, expected_revision=recovered.revision,
        operation_id="transfer-complete",
    )
    assert completed.status == "completed"
    assert app.list_transfers(
        tokens["operator"], kind="handoff", status="completed"
    )["items"] == (completed,)
    output = tmp_path / "transfers" / completed.effect_receipt["destination"]
    assert json.loads(output.read_text())["payload_digest"] == plan.payload_digest
    assert app.execute_transfer(
        tokens["operator"], plan.transfer_id, expected_revision=recovered.revision,
        operation_id="transfer-complete",
    ) == completed
    app.close()


def _configuration(root: Path, identity: str) -> HostConfiguration:
    stores = {
        name: {
            "store_id": name, "driver": "sqlite", "endpoint_ref": str(root / f"{name}.sqlite")
        }
        for name in ("access", "memory")
    }
    return HostConfiguration(
        configuration_id=identity, version=1, enabled_modules=("access", "memory"),
        stores=stores, module_stores={name: name for name in stores},
    )


def _seed_databases(configuration: HostConfiguration) -> None:
    for name, store in configuration.stores.items():
        Path(store.endpoint_ref).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(store.endpoint_ref)
        with connection:
            connection.execute("CREATE TABLE identity(name TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO identity VALUES(?,?)", (name, f"{name}-value"))
        connection.close()


def test_complete_backup_validates_whole_set_before_restore_and_recovers(tmp_path) -> None:
    source = _configuration(tmp_path / "source", "source")
    _seed_databases(source)
    backup = create_sqlite_backup(source, tmp_path / "backup")
    original_manifest = Path(backup["manifest_path"])
    original = json.loads(original_manifest.read_text())

    target = _configuration(tmp_path / "target", "target")
    incomplete = {**original, "stores": original["stores"][:-1]}
    incomplete_path = tmp_path / "backup/incomplete.json"
    incomplete_path.write_text(json.dumps(incomplete))
    with pytest.raises(FoundationError) as incomplete_error:
        restore_sqlite_backup(incomplete_path, target)
    assert incomplete_error.value.code == "recovery_store_set_incomplete"
    assert all(not Path(store.endpoint_ref).exists() for store in target.stores.values())

    corrupt_root = tmp_path / "corrupt"
    corrupt_root.mkdir()
    for item in original["stores"]:
        (corrupt_root / item["file"]).write_bytes((tmp_path / "backup" / item["file"]).read_bytes())
    corrupt_manifest = {**original, "stores": [dict(item) for item in original["stores"]]}
    (corrupt_root / corrupt_manifest["stores"][-1]["file"]).write_bytes(b"corrupt")
    corrupt_path = corrupt_root / "manifest.json"
    corrupt_path.write_text(json.dumps(corrupt_manifest))
    with pytest.raises(FoundationError) as corrupt_error:
        restore_sqlite_backup(corrupt_path, target)
    assert corrupt_error.value.code in {"recovery_digest_mismatch", "recovery_sqlite_invalid"}
    assert all(not Path(store.endpoint_ref).exists() for store in target.stores.values())

    receipt = restore_sqlite_backup(original_manifest, target)
    assert receipt["restored_store_ids"] == ["access", "memory"]
    for name, store in target.stores.items():
        connection = sqlite3.connect(store.endpoint_ref)
        assert connection.execute("SELECT value FROM identity WHERE name=?", (name,)).fetchone()[0] == f"{name}-value"
        connection.close()


def test_complete_backup_covers_dedicated_graph_write_store_and_rejects_neo4j(tmp_path) -> None:
    source_root = tmp_path / "graph-source"
    source = HostConfiguration(
        configuration_id="graph-source",
        version=1,
        enabled_modules=("access", "graph"),
        stores={
            "access": {
                "store_id": "access", "driver": "sqlite",
                "endpoint_ref": str(source_root / "access.sqlite"),
            },
            "graph-control": {
                "store_id": "graph-control", "driver": "sqlite",
                "endpoint_ref": str(source_root / "graph-control.sqlite"),
            },
            "graph-write": {
                "store_id": "graph-write", "driver": "sqlite",
                "endpoint_ref": str(source_root / "graph-write.sqlite"),
            },
        },
        module_stores={"access": "access", "graph": "graph-control"},
        module_write_stores={"graph": "graph-write"},
    )
    _seed_databases(source)
    backup = create_sqlite_backup(source, tmp_path / "graph-backup")
    assert backup["store_ids"] == ["access", "graph-control", "graph-write"]
    assert list(backup["store_roles"]) == [
        {"module": "access", "role": "authority", "store_id": "access"},
        {"module": "graph", "role": "control", "store_id": "graph-control"},
        {"module": "graph", "role": "write", "store_id": "graph-write"},
    ]

    neo4j_payload = source.model_dump(mode="json")
    neo4j_payload.update({
        "configuration_id": "graph-neo4j",
        "stores": {
            **neo4j_payload["stores"],
            "graph-write": {
                "store_id": "graph-write", "driver": "neo4j",
                "endpoint_ref": "bolt://127.0.0.1:7687", "database": "neo4j",
            },
        },
        "fingerprint": "",
    })
    neo4j = HostConfiguration.model_validate(neo4j_payload)
    with pytest.raises(FoundationError) as incompatible:
        create_sqlite_backup(neo4j, tmp_path / "unsupported-backup")
    assert incompatible.value.code == "backup_store_incompatible"
    assert not (tmp_path / "unsupported-backup").exists()


def test_evaluation_diagnostics_and_operation_conflict(tmp_path) -> None:
    create, _access, tokens, _scope, _effects = _fixture(tmp_path)
    app = create()
    report = app.record_evaluation(
        tokens["operator"], subject_ref="extractor:claims:1", cases=10, passed=9,
        metrics={"precision": 0.9}, source_digest="f" * 64,
        operation_id="evaluation",
    )
    replay = app.record_evaluation(
        tokens["operator"], subject_ref="extractor:claims:1", cases=10, passed=9,
        metrics={"precision": 0.9}, source_digest="f" * 64,
        operation_id="evaluation",
    )
    assert replay == report
    assert app.get_evaluation(tokens["operator"], report.evaluation_id) == report
    assert app.list_evaluations(
        tokens["operator"], subject_ref="extractor:claims:1"
    )["items"] == (report,)
    with pytest.raises(FoundationError) as conflict:
        app.record_evaluation(
            tokens["operator"], subject_ref="extractor:claims:1", cases=10, passed=8,
            metrics={"precision": 0.8}, source_digest="f" * 64,
            operation_id="evaluation",
        )
    assert conflict.value.code == "operation_id_conflict"
    diagnostics = app.diagnostics(tokens["operator"])
    assert diagnostics["evaluations"] == 1
    app.close()


def test_management_host_is_declared_and_does_not_create_disabled_domains(tmp_path) -> None:
    configuration = HostConfiguration(
        configuration_id="management-only", version=1,
        enabled_modules=("access", "management"),
        stores={
            "access": {
                "store_id": "access", "driver": "sqlite",
                "endpoint_ref": str(tmp_path / "access.sqlite"),
            },
            "management": {
                "store_id": "management", "driver": "sqlite",
                "endpoint_ref": str(tmp_path / "management.sqlite"),
            },
        },
        module_stores={"access": "access", "management": "management"},
    )
    host = create_management_host(configuration)
    assert host.management is not None
    assert host.memory is host.evidence is host.graph is host.context is None
    assert (tmp_path / "access.sqlite").exists()
    assert (tmp_path / "management.sqlite").exists()
    assert not (tmp_path / "memory.sqlite").exists()
    host.close()


def test_management_effect_resources_are_included_in_recovery_set(tmp_path) -> None:
    source = HostConfiguration(
        configuration_id="source-management", version=1,
        enabled_modules=("access", "management"),
        stores={
            name: {
                "store_id": name, "driver": "sqlite",
                "endpoint_ref": str(tmp_path / "source" / f"{name}.sqlite"),
            }
            for name in ("access", "management")
        },
        module_stores={"access": "access", "management": "management"},
    )
    _seed_databases(source)
    active_pointer = tmp_path / "source/management-effects/active/host_configuration/runtime.json"
    active_pointer.parent.mkdir(parents=True)
    active_pointer.write_text('{"release_id":"release-1"}\n')
    backup = create_sqlite_backup(source, tmp_path / "management-backup")
    assert [item["resource_id"] for item in backup["resources"]] == ["management-effects"]

    target = HostConfiguration(
        configuration_id="target-management", version=1,
        enabled_modules=("access", "management"),
        stores={
            name: {
                "store_id": name, "driver": "sqlite",
                "endpoint_ref": str(tmp_path / "target" / f"{name}.sqlite"),
            }
            for name in ("access", "management")
        },
        module_stores={"access": "access", "management": "management"},
    )
    receipt = restore_sqlite_backup(backup["manifest_path"], target)
    restored_pointer = tmp_path / "target/management-effects/active/host_configuration/runtime.json"
    assert receipt["restored_resource_ids"] == ["management-effects"]
    assert restored_pointer.read_text() == active_pointer.read_text()
