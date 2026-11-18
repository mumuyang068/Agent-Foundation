from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from agent_foundation.adapters.inbound.access_http import create_access_app
from agent_foundation.adapters.outbound.access_state import SqliteAccessStateRepository
from agent_foundation.execution.contracts import ExecutionProfile
from agent_foundation.host.assembly import ComponentAssembly, ComponentSpec
from agent_foundation.host.configuration import HostConfiguration, StoreConfiguration
from agent_foundation.host.minimal import create_access_host
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import Principal, ScopeRef
from agent_foundation.modules.access.application import AccessApplication, AccessDenied
from agent_foundation.modules.access.contracts import AccessGrant


def grant() -> AccessGrant:
    return AccessGrant(
        grant_id="grant-1",
        principal_id="service-1",
        scope=ScopeRef(tenant_id="tenant", space_id="space"),
        domains=("memory",),
        actions=("read",),
    )


@pytest.mark.parametrize(
    "module",
    (
        "agent_foundation.kernel.primitives",
        "agent_foundation.execution.contracts",
        "agent_foundation.modules.access.domain",
        "agent_foundation.host.configuration",
    ),
)
def test_contract_imports_do_not_load_host_assembly_adapters_or_transport(module: str) -> None:
    backend = Path(__file__).parents[1]
    program = (
        "import json,sys;sys.path.insert(0," + repr(str(backend)) + ");"
        f"import {module};print(json.dumps(sorted(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program],
        cwd=backend.parent,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    loaded = json.loads(result.stdout)
    forbidden = (
        "agent_foundation.host.assembly",
        "agent_foundation.host.minimal",
        "agent_foundation.adapters",
        "agent_foundation.modules.memory",
        "agent_foundation.modules.evidence",
        "fastapi",
    )
    assert not [name for name in loaded if any(name == item or name.startswith(item + ".") for item in forbidden)]


def test_access_revocation_is_read_by_an_existing_authorizer(tmp_path: Path) -> None:
    database = tmp_path / "access.sqlite3"
    first = AccessApplication(SqliteAccessStateRepository(sqlite3.connect(database, check_same_thread=False)))
    first.put_grant(grant())
    issued = first.issue_session(Principal(principal_id="service-1", kind="service"), "grant-1")
    second = AccessApplication(SqliteAccessStateRepository(sqlite3.connect(database, check_same_thread=False)))
    first.revoke_grant("grant-1")
    try:
        with pytest.raises(AccessDenied) as denied:
            second.authorize_token(
                issued.access_token,
                domain="memory",
                action="read",
                policy_id="memory",
                policy_version=1,
            )
        assert denied.value.code == "grant_inactive"
    finally:
        first.close()
        second.close()


def test_http_credential_produces_trusted_identity_and_scope(tmp_path: Path) -> None:
    configuration = HostConfiguration(
        configuration_id="http-access",
        version=1,
        enabled_modules=("access",),
        stores={"access": StoreConfiguration(store_id="access", driver="sqlite", endpoint_ref=str(tmp_path / "http.db"))},
        module_stores={"access": "access"},
    )
    host = create_access_host(configuration)
    app = create_access_app(host.access, bootstrap_token="bootstrap-secret")
    try:
        with TestClient(app) as client:
            bootstrap = {"authorization": "Bearer bootstrap-secret"}
            created = client.post(
                "/api/foundation/v1/access/grants", headers=bootstrap, json=grant().model_dump(mode="json")
            )
            assert created.status_code == 200
            issued = client.post(
                "/api/foundation/v1/access/sessions",
                headers=bootstrap,
                json={
                    "principal_id": "service-1",
                    "principal_kind": "service",
                    "grant_id": "grant-1",
                    "ttl_seconds": 300,
                },
            )
            assert issued.status_code == 200
            authorized = client.post(
                "/api/foundation/v1/access/authorize",
                headers={"authorization": f"Bearer {issued.json()['access_token']}"},
                json={"domain": "memory", "action": "read", "policy_id": "memory", "policy_version": 1},
            )
            assert authorized.status_code == 200
            context = authorized.json()["trusted_context"]
            assert context["principal"] == {"principal_id": "service-1", "kind": "service", "delegation_id": None}
            assert context["scope"] == {"tenant_id": "tenant", "space_id": "space", "project_id": None}
            client.post("/api/foundation/v1/access/grants/grant-1/revoke", headers=bootstrap)
            denied = client.post(
                "/api/foundation/v1/access/authorize",
                headers={"authorization": f"Bearer {issued.json()['access_token']}"},
                json={"domain": "memory", "action": "read", "policy_id": "memory", "policy_version": 1},
            )
            assert denied.status_code == 403
            assert denied.json()["error"]["code"] == "grant_inactive"
    finally:
        host.close()


def test_assembly_rejects_undeclared_dependency() -> None:
    def invalid(context):
        context.require("access")

    with pytest.raises(FoundationError) as failure:
        ComponentAssembly(("memory",), (ComponentSpec("memory", (), invalid),))
    assert failure.value.code == "module_dependency_undeclared"


def test_assembly_closes_built_components_when_later_startup_fails() -> None:
    closed: list[str] = []

    class Resource:
        def close(self) -> None:
            closed.append("access")

    def fail(_context):
        raise FoundationError("startup_failed", "simulated startup failure")

    with pytest.raises(FoundationError):
        ComponentAssembly(
            ("access", "memory"),
            (
                ComponentSpec("access", (), lambda _context: Resource()),
                ComponentSpec("memory", ("access",), fail),
            ),
        )
    assert closed == ["access"]


def test_access_only_host_does_not_open_disabled_store(tmp_path: Path) -> None:
    access_path = tmp_path / "access.sqlite3"
    disabled_path = tmp_path / "memory.sqlite3"
    host = create_access_host(HostConfiguration(
        configuration_id="access-only",
        version=1,
        enabled_modules=("access",),
        stores={
            "access": StoreConfiguration(store_id="access", driver="sqlite", endpoint_ref=str(access_path)),
            "memory": StoreConfiguration(store_id="memory", driver="sqlite", endpoint_ref=str(disabled_path)),
        },
        module_stores={"access": "access", "memory": "memory"},
    ))
    try:
        assert access_path.exists()
        assert not disabled_path.exists()
    finally:
        host.close()


def test_execution_profile_changes_without_changing_grant_identity() -> None:
    access_grant = grant()
    first = ExecutionProfile(
        profile_id="memory-read",
        version=1,
        domain="memory",
        algorithm_id="memory.lexical",
        implementation_version="1",
        parameters={"k": 60},
    )
    second = first.model_copy(update={"version": 2, "parameters": {"k": 20}, "fingerprint": ""})
    second = ExecutionProfile.model_validate(second.model_dump(mode="json"))
    assert first.fingerprint != second.fingerprint
    assert access_grant.fingerprint == grant().fingerprint
