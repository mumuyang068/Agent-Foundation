from __future__ import annotations

from pathlib import Path

import pytest

from agent_foundation.host.configuration import HostConfiguration
from agent_foundation.host.minimal import (
    create_access_host,
    create_context_host,
    create_evidence_host,
    create_graph_host,
    create_management_host,
    create_memory_host,
    create_workflow_host,
)
from agent_foundation.kernel.primitives import Principal, ScopeRef
from agent_foundation.modules.access.contracts import AccessGrant


CASES = (
    ("access", ("access",)),
    ("evidence", ("access", "evidence")),
    ("memory", ("access", "memory")),
    ("graph-evidence", ("access", "evidence", "graph")),
    ("context-empty", ("access", "context")),
    ("context-memory", ("access", "context", "memory")),
    ("jobs", ("access", "jobs")),
    ("graph-jobs", ("access", "evidence", "graph", "jobs")),
    ("memory-applications", ("access", "applications", "memory")),
    ("context-applications", ("access", "applications", "context", "memory")),
    ("management", ("access", "management")),
    ("all", ("access", "applications", "context", "evidence", "graph", "jobs", "management", "memory")),
)


def _configuration(root: Path, name: str, modules: tuple[str, ...]) -> HostConfiguration:
    stores = {
        module: {
            "store_id": module,
            "driver": "sqlite",
            "endpoint_ref": str(root / f"{module}.sqlite3"),
        }
        for module in modules
    }
    dependencies = {}
    if "context" in modules:
        sources = tuple(domain for domain in ("evidence", "memory", "graph") if domain in modules)
        dependencies["context"] = sources
    if "applications" in modules:
        dependencies["applications"] = (
            ("memory", "context") if "context" in modules else ("memory",)
        )
    return HostConfiguration(
        configuration_id=f"matrix:{name}", version=1,
        enabled_modules=modules, stores=stores,
        module_stores={module: module for module in modules},
        module_dependencies=dependencies,
    )


def _create(configuration: HostConfiguration):
    enabled = set(configuration.enabled_modules)
    if "management" in enabled:
        return create_management_host(configuration)
    if {"jobs", "applications"} & enabled:
        return create_workflow_host(configuration)
    if "context" in enabled:
        return create_context_host(configuration)
    if "graph" in enabled:
        return create_graph_host(configuration)
    if "memory" in enabled:
        return create_memory_host(configuration)
    if "evidence" in enabled:
        return create_evidence_host(configuration)
    return create_access_host(configuration)


@pytest.mark.parametrize(("name", "modules"), CASES)
def test_real_module_assembly_creates_only_declared_authorities(tmp_path, name, modules):
    root = tmp_path / name
    root.mkdir()
    configuration = _configuration(root, name, modules)
    host = _create(configuration)
    try:
        assert set(host.enabled_modules) == set(modules)
        assert sorted(path.stem for path in root.glob("*.sqlite3")) == sorted(modules)
    finally:
        host.close()


def test_disabled_projection_does_not_import_optional_driver(tmp_path, monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "neo4j", None)
    configuration = _configuration(tmp_path, "access", ("access",))
    host = create_access_host(configuration)
    try:
        assert host.enabled_modules == ("access",)
    finally:
        host.close()


def test_declared_candidate_projection_is_injected_without_authority_methods(tmp_path, monkeypatch):
    projection = type("CandidateProjection", (), {})()
    projection.refs = ()
    projection.closed = False

    def candidates(scope, *, domain, query, limit, resource_ids=()):
        assert domain == "evidence"
        assert query == "candidate"
        return tuple(
            ref for ref in projection.refs if not resource_ids or ref.resource_id in resource_ids
        )[:limit]

    projection.candidates = candidates

    class Resources:
        def get(self, projection_id):
            assert projection_id == "evidence-candidates"
            return projection

        def close(self):
            projection.closed = True

    def create_resources(configuration, *, projection_ids=None):
        assert projection_ids == ("evidence-candidates",)
        return Resources()

    monkeypatch.setattr(
        "agent_foundation.host.minimal.create_projection_resources", create_resources
    )
    configuration = HostConfiguration(
        configuration_id="evidence-candidate-projection",
        version=1,
        enabled_modules=("access", "evidence"),
        stores={
            name: {
                "store_id": name,
                "driver": "sqlite",
                "endpoint_ref": str(tmp_path / f"{name}.sqlite3"),
            }
            for name in ("access", "evidence")
        },
        module_stores={name: name for name in ("access", "evidence")},
        projections={
            "evidence-candidates": {
                "projection_id": "evidence-candidates",
                "driver": "qdrant",
                "endpoint_ref": "http://candidate.invalid",
                "database_or_collection": "evidence",
                "role": "candidate",
            }
        },
        module_projections={"evidence": ("evidence-candidates",)},
    )
    host = create_evidence_host(configuration)
    scope = ScopeRef(tenant_id="tenant", space_id="space")
    host.access.put_grant(AccessGrant(
        grant_id="grant", principal_id="service", scope=scope,
        domains=("evidence",), actions=("admit", "search"), limits={"max_items": 4},
    ))
    token = host.access.issue_session(
        Principal(principal_id="service", kind="service"), "grant"
    ).access_token
    revision = host.evidence.admit(
        token, source_id="source", content="projected candidate", operation_id="admit"
    )
    projection.refs = (revision.ref(),)
    assert host.evidence.search(token, query="candidate", limit=1).hits[0].revision == revision
    host.close()
    assert projection.closed is True
