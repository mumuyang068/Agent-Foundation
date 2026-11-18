from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from agent_foundation.adapters.outbound.access_state import (
    InMemoryAccessStateRepository,
    PostgresAccessStateRepository,
    SqliteAccessStateRepository,
)
from agent_foundation.adapters.outbound.sqlite_evidence_v2 import SqliteEvidenceAuthority
from agent_foundation.adapters.outbound.postgres_evidence_v2 import PostgresEvidenceAuthority
from agent_foundation.adapters.outbound.postgres_graph_control_v2 import (
    PostgresGraphControlStore,
)
from agent_foundation.adapters.outbound.sqlite_graph_v2 import (
    SqliteGraphControlStore,
    SqliteGraphWriteAuthority,
)
from agent_foundation.adapters.outbound.neo4j_graph_v2 import Neo4jGraphWriteAuthority
from agent_foundation.adapters.outbound.sqlite_memory_v2 import SqliteMemoryAuthority
from agent_foundation.adapters.outbound.postgres_memory_v2 import PostgresMemoryAuthority
from agent_foundation.adapters.outbound.sqlite_context_v2 import SqliteContextAuthority
from agent_foundation.adapters.outbound.sqlite_application_turns_v2 import SqliteTurnAuthority
from agent_foundation.adapters.outbound.sqlite_jobs_v2 import SqliteJobAuthority
from agent_foundation.adapters.outbound.sqlite_management_v2 import SqliteManagementAuthority
from agent_foundation.adapters.outbound.extraction_sinks_v2 import DomainExtractionProposalSink
from agent_foundation.adapters.outbound.memory_links_v2 import GraphMemoryLinkPort
from agent_foundation.adapters.outbound.projected_candidates_v2 import (
    AuthorityEvidenceCandidateSource,
    AuthorityGraphCandidateSource,
    AuthorityMemoryCandidateSource,
    ProjectedEvidenceCandidateSource,
    ProjectedGraphCandidateSource,
    ProjectedMemoryCandidateSource,
)
from agent_foundation.adapters.outbound.filesystem_management_effects_v2 import (
    FilesystemIndexEffect,
    FilesystemReleaseEffect,
    FilesystemTransferEffect,
)
from agent_foundation.adapters.outbound.context_sources_v2 import (
    EvidenceContextSource,
    GraphContextSource,
    MemoryContextSource,
)
from agent_foundation.algorithms.context_v2 import ScoreContextCombination
from agent_foundation.algorithms.evidence_v2 import LexicalEvidenceCandidates
from agent_foundation.algorithms.graph_v2 import BoundedTemporalTraversal, LexicalGraphCandidates
from agent_foundation.algorithms.memory_v2 import LexicalMemoryCandidates
from agent_foundation.execution.contracts import ExecutionProfile
from agent_foundation.execution.extractors import ExtractorRuntime, JsonPointerExtractor
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.modules.access.application import AccessApplication
from agent_foundation.modules.evidence.application import EvidenceApplication
from agent_foundation.modules.evidence.contracts import EvidencePolicy
from agent_foundation.modules.graph.application import GraphApplication
from agent_foundation.modules.graph.contracts import GraphPolicy
from agent_foundation.modules.memory.application import MemoryApplication
from agent_foundation.modules.memory.contracts import MemoryPolicy
from agent_foundation.modules.context.application import ContextApplication
from agent_foundation.modules.context.contracts import ContextPolicy
from agent_foundation.modules.applications.contracts import ApplicationPolicy
from agent_foundation.modules.jobs.application import JobsApplication
from agent_foundation.modules.jobs.contracts import JobPolicy
from agent_foundation.modules.management.application import ManagementApplication
from agent_foundation.modules.management.contracts import ManagementPolicy
from agent_foundation.workflows.agent_turns import AgentTurnWorkflow
from agent_foundation.workflows.domain_jobs import (
    GraphApplyWorkflow,
    GraphBulkProposalWorkflow,
    MemoryMaintenanceWorkflow,
)

from .assembly import ComponentAssembly, ComponentSpec
from .configuration import HostConfiguration
from .projections_v2 import create_projection_resources


def _configured_endpoint(store, *, module: str) -> str:
    if not store.endpoint_ref:
        raise FoundationError("store_endpoint_missing", f"{module.title()} authority store needs endpoint_ref")
    endpoint = store.endpoint_ref
    if store.credential_ref:
        import os

        endpoint = os.environ.get(store.credential_ref, "")
        if not endpoint:
            raise FoundationError(
                "store_credential_missing",
                f"Credential environment variable {store.credential_ref!r} is missing",
            )
    return endpoint


def _evidence_authority(configuration: HostConfiguration):
    store_id = configuration.module_stores.get("evidence")
    if store_id is None:
        raise FoundationError("module_store_missing", "Evidence needs its own authority store")
    store = configuration.stores[store_id]
    endpoint = _configured_endpoint(store, module="evidence")
    if store.driver == "sqlite":
        return SqliteEvidenceAuthority(
            sqlite3.connect(endpoint, check_same_thread=False, timeout=10)
        )
    if store.driver == "postgres":
        try:
            import psycopg
        except ImportError as exc:
            raise FoundationError(
                "store_driver_missing",
                "PostgreSQL Evidence authority needs agent-foundation[postgres]",
            ) from exc
        return PostgresEvidenceAuthority(psycopg.connect(endpoint))
    raise FoundationError(
        "store_driver_unavailable",
        f"Evidence authority does not support {store.driver!r}",
    )


def _memory_authority(configuration: HostConfiguration):
    store_id = configuration.module_stores.get("memory")
    if store_id is None:
        raise FoundationError("module_store_missing", "Memory needs its own authority store")
    store = configuration.stores[store_id]
    endpoint = _configured_endpoint(store, module="memory")
    if store.driver == "sqlite":
        return SqliteMemoryAuthority(
            sqlite3.connect(endpoint, check_same_thread=False, timeout=10)
        )
    if store.driver == "postgres":
        try:
            import psycopg
        except ImportError as exc:
            raise FoundationError(
                "store_driver_missing",
                "PostgreSQL Memory authority needs agent-foundation[postgres]",
            ) from exc
        return PostgresMemoryAuthority(psycopg.connect(endpoint))
    raise FoundationError(
        "store_driver_unavailable",
        f"Memory authority does not support {store.driver!r}",
    )


def _graph_authorities(configuration: HostConfiguration):
    proposal_store_id = configuration.module_stores.get("graph")
    if proposal_store_id is None:
        raise FoundationError("module_store_missing", "Graph needs a proposal store")
    proposal_store = configuration.stores[proposal_store_id]
    proposal_endpoint = _configured_endpoint(proposal_store, module="graph control")
    if proposal_store.driver == "sqlite":
        authority = SqliteGraphControlStore(
            sqlite3.connect(
                proposal_endpoint, check_same_thread=False, timeout=10
            )
        )
    elif proposal_store.driver == "postgres":
        try:
            import psycopg
        except ImportError as exc:
            raise FoundationError(
                "store_driver_missing",
                "PostgreSQL Graph control store needs agent-foundation[postgres]",
            ) from exc
        authority = PostgresGraphControlStore(psycopg.connect(proposal_endpoint))
    else:
        raise FoundationError(
            "store_driver_unavailable",
            "Graph control store requires SQLite or PostgreSQL",
        )
    write_store_id = configuration.module_write_stores.get("graph")
    if write_store_id is None or write_store_id == proposal_store_id:
        if proposal_store.driver != "sqlite":
            authority.close()
            raise FoundationError(
                "graph_write_store_missing",
                "PostgreSQL Graph control needs a dedicated SQLite or Neo4j write authority",
            )
        try:
            writer = SqliteGraphWriteAuthority(
                sqlite3.connect(
                    proposal_endpoint, check_same_thread=False, timeout=10
                )
            )
        except BaseException:
            authority.close()
            raise
        return authority, writer
    write_store = configuration.stores[write_store_id]
    try:
        if write_store.driver == "sqlite":
            write_endpoint = _configured_endpoint(write_store, module="graph write")
            writer = SqliteGraphWriteAuthority(
                sqlite3.connect(
                    write_endpoint, check_same_thread=False, timeout=10
                )
            )
            return authority, writer
        if write_store.driver != "neo4j":
            raise FoundationError(
                "store_driver_unavailable",
                "Graph write authority requires SQLite or Neo4j",
            )
        if not write_store.endpoint_ref:
            raise FoundationError(
                "store_endpoint_missing", "Graph write authority store needs endpoint_ref"
            )
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise FoundationError(
                "store_driver_missing",
                "Neo4j Graph authority needs agent-foundation[projections]",
            ) from exc
        auth = None
        if write_store.credential_ref:
            import os

            raw_credential = os.environ.get(write_store.credential_ref, "")
            if ":" not in raw_credential:
                raise FoundationError(
                    "store_credential_invalid",
                    "Neo4j credential must be supplied as username:password",
                )
            auth = tuple(raw_credential.split(":", 1))
        driver = GraphDatabase.driver(write_store.endpoint_ref, auth=auth)
        try:
            writer = Neo4jGraphWriteAuthority(
                driver, database=write_store.database or "neo4j"
            )
        except BaseException:
            driver.close()
            raise
        return authority, writer
    except BaseException:
        authority.close()
        raise


def _close_unique(*components) -> None:
    closed: set[int] = set()
    for component in components:
        if component is None or id(component) in closed:
            continue
        closed.add(id(component))
        component.close()


def _projected_candidate_source(configuration: HostConfiguration, module: str, reader):
    candidate_ids = tuple(
        projection_id
        for projection_id in configuration.module_projections.get(module, ())
        if configuration.projections[projection_id].role == "candidate"
    )
    if not candidate_ids:
        adapter = {
            "memory": AuthorityMemoryCandidateSource,
            "evidence": AuthorityEvidenceCandidateSource,
            "graph": AuthorityGraphCandidateSource,
        }.get(module)
        if adapter is None:
            raise FoundationError(
                "candidate_projection_owner_invalid",
                f"Module {module!r} does not support candidate sources",
            )
        return adapter(reader)
    if len(candidate_ids) != 1:
        raise FoundationError(
            "candidate_projection_ambiguous",
            f"Module {module!r} must declare at most one candidate projection",
        )
    resources = create_projection_resources(configuration, projection_ids=candidate_ids)
    projection = resources.get(candidate_ids[0])
    adapter = {
        "memory": ProjectedMemoryCandidateSource,
        "evidence": ProjectedEvidenceCandidateSource,
        "graph": ProjectedGraphCandidateSource,
    }.get(module)
    if adapter is None:
        resources.close()
        raise FoundationError(
            "candidate_projection_owner_invalid",
            f"Module {module!r} does not support candidate projections",
        )
    return adapter(projection, reader, resources)


def _access_application(configuration: HostConfiguration) -> AccessApplication:
    store_id = configuration.module_stores.get("access")
    if store_id is None or configuration.stores[store_id].driver == "memory":
        return AccessApplication(InMemoryAccessStateRepository())
    store = configuration.stores[store_id]
    if not store.endpoint_ref:
        raise FoundationError("store_endpoint_missing", "Access authority store needs endpoint_ref")
    if store.driver == "sqlite":
        return AccessApplication(SqliteAccessStateRepository(
            sqlite3.connect(store.endpoint_ref, check_same_thread=False, timeout=10)
        ))
    if store.driver == "postgres":
        try:
            import psycopg
        except ImportError as exc:
            raise FoundationError(
                "store_driver_missing", "PostgreSQL Access authority needs agent-foundation[postgres]"
            ) from exc
        endpoint = store.endpoint_ref
        if store.credential_ref:
            import os

            endpoint = os.environ.get(store.credential_ref, "")
            if not endpoint:
                raise FoundationError(
                    "store_credential_missing",
                    f"Credential environment variable {store.credential_ref!r} is missing",
                )
        return AccessApplication(PostgresAccessStateRepository(psycopg.connect(endpoint)))
    raise FoundationError(
        "store_driver_unavailable", f"Access authority does not support {store.driver!r}"
    )


@dataclass(frozen=True)
class AccessFoundationHost:
    configuration: HostConfiguration
    access: AccessApplication
    _assembly: ComponentAssembly

    @property
    def enabled_modules(self) -> tuple[str, ...]:
        return self._assembly.enabled

    def close(self) -> None:
        self._assembly.close()


@dataclass(frozen=True)
class EvidenceFoundationHost:
    configuration: HostConfiguration
    access: AccessApplication
    evidence: EvidenceApplication
    _assembly: ComponentAssembly

    @property
    def enabled_modules(self) -> tuple[str, ...]:
        return self._assembly.enabled

    def close(self) -> None:
        self._assembly.close()


@dataclass(frozen=True)
class MemoryFoundationHost:
    configuration: HostConfiguration
    access: AccessApplication
    memory: MemoryApplication
    evidence: EvidenceApplication | None
    _assembly: ComponentAssembly

    @property
    def enabled_modules(self) -> tuple[str, ...]:
        return self._assembly.enabled

    def close(self) -> None:
        self._assembly.close()


@dataclass(frozen=True)
class GraphFoundationHost:
    configuration: HostConfiguration
    access: AccessApplication
    evidence: EvidenceApplication
    graph: GraphApplication
    _assembly: ComponentAssembly

    @property
    def enabled_modules(self) -> tuple[str, ...]:
        return self._assembly.enabled

    def close(self) -> None:
        self._assembly.close()


@dataclass(frozen=True)
class ContextFoundationHost:
    configuration: HostConfiguration
    access: AccessApplication
    context: ContextApplication
    evidence: EvidenceApplication | None
    memory: MemoryApplication | None
    graph: GraphApplication | None
    _assembly: ComponentAssembly

    @property
    def enabled_modules(self) -> tuple[str, ...]:
        return self._assembly.enabled

    def close(self) -> None:
        self._assembly.close()


@dataclass(frozen=True)
class WorkflowFoundationHost:
    """Composition root for durable jobs and product-neutral Agent workflows."""

    configuration: HostConfiguration
    access: AccessApplication
    evidence: EvidenceApplication | None
    memory: MemoryApplication | None
    graph: GraphApplication | None
    context: ContextApplication | None
    jobs: JobsApplication | None
    applications: AgentTurnWorkflow | None
    _base: object

    @property
    def enabled_modules(self) -> tuple[str, ...]:
        return self.configuration.enabled_modules

    def close(self) -> None:
        if self.applications is not None:
            self.applications.close()
        if self.jobs is not None:
            self.jobs.close()
        self._base.close()


@dataclass(frozen=True)
class ManagementFoundationHost:
    configuration: HostConfiguration
    access: AccessApplication
    evidence: EvidenceApplication | None
    memory: MemoryApplication | None
    graph: GraphApplication | None
    context: ContextApplication | None
    jobs: JobsApplication | None
    applications: AgentTurnWorkflow | None
    management: ManagementApplication
    _base: object

    @property
    def enabled_modules(self) -> tuple[str, ...]:
        return self.configuration.enabled_modules

    def close(self) -> None:
        self.management.close()
        self._base.close()


def create_access_host(configuration: HostConfiguration) -> AccessFoundationHost:
    """Build the PL-01 access-only runtime from declared resources."""

    unsupported = set(configuration.enabled_modules) - {"access"}
    if unsupported:
        raise FoundationError(
            "module_implementation_unavailable",
            f"PL-01 has no implementation for: {', '.join(sorted(unsupported))}",
        )

    def access_factory(_context):
        return _access_application(configuration)

    assembly = ComponentAssembly(
        tuple(configuration.enabled_modules),
        (ComponentSpec(name="access", dependencies=(), factory=access_factory),),
    )
    return AccessFoundationHost(configuration=configuration, access=assembly.get("access"), _assembly=assembly)


def create_evidence_host(
    configuration: HostConfiguration,
    *,
    policy: EvidencePolicy | None = None,
    profile: ExecutionProfile | None = None,
) -> EvidenceFoundationHost:
    if set(configuration.enabled_modules) != {"access", "evidence"}:
        raise FoundationError("module_set_invalid", "Evidence host requires exactly access and evidence")

    def access_factory(_context):
        return _access_application(configuration)

    def evidence_factory(context):
        try:
            authority = _evidence_authority(configuration)
            selected_policy = policy or EvidencePolicy(policy_id="evidence", version=1)
            selected_profile = profile or ExecutionProfile(
                profile_id="evidence-search",
                version=1,
                domain="evidence",
                algorithm_id="evidence.lexical-v2",
                implementation_version="1",
                budget={"max_items": 20},
            )
            return EvidenceApplication(
                authority,
                context.require("access"),
                selected_policy,
                selected_profile,
                LexicalEvidenceCandidates(),
                candidate_source=_projected_candidate_source(configuration, "evidence", authority),
            )
        except BaseException:
            if "authority" in locals():
                authority.close()
            raise

    assembly = ComponentAssembly(
        tuple(configuration.enabled_modules),
        (
            ComponentSpec(name="access", dependencies=(), factory=access_factory),
            ComponentSpec(name="evidence", dependencies=("access",), factory=evidence_factory),
        ),
    )
    return EvidenceFoundationHost(
        configuration=configuration,
        access=assembly.get("access"),
        evidence=assembly.get("evidence"),
        _assembly=assembly,
    )


def create_memory_host(
    configuration: HostConfiguration,
    *,
    policy: MemoryPolicy | None = None,
    profile: ExecutionProfile | None = None,
) -> MemoryFoundationHost:
    enabled = set(configuration.enabled_modules)
    if enabled not in ({"access", "memory"}, {"access", "evidence", "memory"}):
        raise FoundationError(
            "module_set_invalid",
            "Memory host requires access and memory, with optional evidence",
        )

    def access_factory(_context):
        return _access_application(configuration)

    def evidence_factory(context):
        try:
            authority = _evidence_authority(configuration)
            return EvidenceApplication(
                authority,
                context.require("access"),
                EvidencePolicy(policy_id="evidence", version=1),
                ExecutionProfile(
                    profile_id="evidence-search",
                    version=1,
                    domain="evidence",
                    algorithm_id="evidence.lexical-v2",
                    implementation_version="1",
                    budget={"max_items": 20},
                ),
                LexicalEvidenceCandidates(),
                candidate_source=_projected_candidate_source(configuration, "evidence", authority),
            )
        except BaseException:
            if "authority" in locals():
                authority.close()
            raise

    def memory_factory(context):
        try:
            selected_policy = policy or MemoryPolicy(policy_id="memory", version=1)
            selected_profile = profile or ExecutionProfile(
                profile_id="memory-recall",
                version=1,
                domain="memory",
                algorithm_id="memory.lexical-v2",
                implementation_version="1",
                budget={"max_items": 20},
            )
            source_authority = context.require("evidence") if "evidence" in enabled else None
            authority = _memory_authority(configuration)
            return MemoryApplication(
                authority,
                context.require("access"),
                selected_policy,
                selected_profile,
                LexicalMemoryCandidates(),
                source_authority,
                candidate_source=_projected_candidate_source(configuration, "memory", authority),
            )
        except BaseException:
            if "authority" in locals():
                authority.close()
            raise

    specs = [ComponentSpec(name="access", dependencies=(), factory=access_factory)]
    if "evidence" in enabled:
        specs.append(ComponentSpec(name="evidence", dependencies=("access",), factory=evidence_factory))
    specs.append(ComponentSpec(
        name="memory",
        dependencies=("access", "evidence") if "evidence" in enabled else ("access",),
        factory=memory_factory,
    ))
    assembly = ComponentAssembly(tuple(configuration.enabled_modules), tuple(specs))
    return MemoryFoundationHost(
        configuration=configuration,
        access=assembly.get("access"),
        memory=assembly.get("memory"),
        evidence=assembly.get("evidence") if "evidence" in enabled else None,
        _assembly=assembly,
    )


def create_graph_host(
    configuration: HostConfiguration,
    *,
    policy: GraphPolicy | None = None,
    profile: ExecutionProfile | None = None,
) -> GraphFoundationHost:
    if set(configuration.enabled_modules) != {"access", "evidence", "graph"}:
        raise FoundationError("module_set_invalid", "Graph host requires access, evidence and graph")

    def access_factory(_context):
        return _access_application(configuration)

    def evidence_factory(context):
        try:
            authority = _evidence_authority(configuration)
            return EvidenceApplication(
                authority,
                context.require("access"),
                EvidencePolicy(policy_id="evidence", version=1),
                ExecutionProfile(
                    profile_id="evidence-search",
                    version=1,
                    domain="evidence",
                    algorithm_id="evidence.lexical-v2",
                    implementation_version="1",
                    budget={"max_items": 20},
                ),
                LexicalEvidenceCandidates(),
                candidate_source=_projected_candidate_source(configuration, "evidence", authority),
            )
        except BaseException:
            if "authority" in locals():
                authority.close()
            raise

    def graph_factory(context):
        proposal_store = write_authority = None
        try:
            proposal_store, write_authority = _graph_authorities(configuration)
            return GraphApplication(
                proposal_store,
                context.require("access"),
                policy or GraphPolicy(policy_id="graph", version=1),
                profile or ExecutionProfile(
                    profile_id="graph-query",
                    version=1,
                    domain="graph",
                    algorithm_id="graph.lexical-v2",
                    implementation_version="1",
                    budget={"max_items": 20},
                ),
                LexicalGraphCandidates(),
                context.require("evidence"),
                write_authority=write_authority,
                reader=write_authority,
                temporal_algorithm=BoundedTemporalTraversal(),
                candidate_source=_projected_candidate_source(
                    configuration, "graph", write_authority
                ),
            )
        except BaseException:
            _close_unique(proposal_store, write_authority)
            raise

    assembly = ComponentAssembly(
        tuple(configuration.enabled_modules),
        (
            ComponentSpec(name="access", dependencies=(), factory=access_factory),
            ComponentSpec(name="evidence", dependencies=("access",), factory=evidence_factory),
            ComponentSpec(
                name="graph", dependencies=("access", "evidence"), factory=graph_factory
            ),
        ),
    )
    return GraphFoundationHost(
        configuration=configuration,
        access=assembly.get("access"),
        evidence=assembly.get("evidence"),
        graph=assembly.get("graph"),
        _assembly=assembly,
    )


def create_context_host(
    configuration: HostConfiguration,
    *,
    policy: ContextPolicy | None = None,
    profile: ExecutionProfile | None = None,
) -> ContextFoundationHost:
    enabled = set(configuration.enabled_modules)
    allowed = {"access", "context", "evidence", "memory", "graph"}
    if not {"access", "context"}.issubset(enabled) or not enabled.issubset(allowed):
        raise FoundationError(
            "module_set_invalid", "Context host needs access and context with optional domain sources"
        )
    if "graph" in enabled and "evidence" not in enabled:
        raise FoundationError("module_dependency_missing", "Graph source requires Evidence")
    required_sources = set(configuration.module_dependencies.get("context", ()))
    if not required_sources.issubset({"memory", "evidence", "graph"}):
        raise FoundationError(
            "module_dependency_invalid", "Context dependencies must be explicit source domains"
        )
    missing_sources = required_sources - enabled
    if missing_sources:
        raise FoundationError(
            "module_dependency_missing",
            f"Context required sources are disabled: {', '.join(sorted(missing_sources))}",
        )
    memory_dependencies = set(configuration.module_dependencies.get("memory", ()))
    if not memory_dependencies.issubset({"evidence", "graph"}):
        raise FoundationError(
            "module_dependency_invalid", "Memory dependencies may contain Evidence and Graph"
        )
    missing_memory_dependencies = memory_dependencies - enabled
    if missing_memory_dependencies:
        raise FoundationError(
            "module_dependency_missing",
            "Memory required modules are disabled: "
            + ", ".join(sorted(missing_memory_dependencies)),
        )

    def access_factory(_context):
        return _access_application(configuration)

    def evidence_factory(context):
        try:
            authority = _evidence_authority(configuration)
            return EvidenceApplication(
                authority, context.require("access"),
                EvidencePolicy(policy_id="evidence", version=1),
                ExecutionProfile(
                    profile_id="evidence-search", version=1, domain="evidence",
                    algorithm_id="evidence.lexical-v2", implementation_version="1",
                    budget={"max_items": 20},
                ),
                LexicalEvidenceCandidates(),
                candidate_source=_projected_candidate_source(configuration, "evidence", authority),
            )
        except BaseException:
            if "authority" in locals():
                authority.close()
            raise

    def memory_factory(context):
        try:
            authority = _memory_authority(configuration)
            return MemoryApplication(
                authority, context.require("access"),
                MemoryPolicy(policy_id="memory", version=1),
                ExecutionProfile(
                    profile_id="memory-recall", version=1, domain="memory",
                    algorithm_id="memory.lexical-v2", implementation_version="1",
                    budget={"max_items": 20},
                ),
                LexicalMemoryCandidates(),
                context.require("evidence") if "evidence" in enabled else None,
                candidate_source=_projected_candidate_source(configuration, "memory", authority),
                link_port=(
                    GraphMemoryLinkPort(context.require("graph"))
                    if "graph" in memory_dependencies else None
                ),
            )
        except BaseException:
            if "authority" in locals():
                authority.close()
            raise

    def graph_factory(context):
        proposal_store = write_authority = None
        try:
            proposal_store, write_authority = _graph_authorities(configuration)
            return GraphApplication(
                proposal_store, context.require("access"),
                GraphPolicy(policy_id="graph", version=1),
                ExecutionProfile(
                    profile_id="graph-query", version=1, domain="graph",
                    algorithm_id="graph.lexical-v2", implementation_version="1",
                    budget={"max_items": 20},
                ),
                LexicalGraphCandidates(), context.require("evidence"),
                write_authority=write_authority,
                reader=write_authority,
                temporal_algorithm=BoundedTemporalTraversal(),
                candidate_source=_projected_candidate_source(
                    configuration, "graph", write_authority
                ),
            )
        except BaseException:
            _close_unique(proposal_store, write_authority)
            raise

    def context_factory(context):
        store_id = configuration.module_stores.get("context")
        if store_id is None:
            raise FoundationError("module_store_missing", "Context needs its own authority store")
        store = configuration.stores[store_id]
        if store.driver != "sqlite" or not store.endpoint_ref:
            raise FoundationError("store_driver_unavailable", "Context authority requires SQLite in PL-05")
        connection = sqlite3.connect(store.endpoint_ref, check_same_thread=False, timeout=10)
        try:
            sources = []
            if "evidence" in enabled:
                sources.append(EvidenceContextSource(context.require("evidence")))
            if "memory" in enabled:
                sources.append(MemoryContextSource(context.require("memory")))
            if "graph" in enabled:
                sources.append(GraphContextSource(context.require("graph")))
            selected_policy = policy or ContextPolicy(policy_id="context", version=1)
            selected_profile = profile or ExecutionProfile(
                profile_id="context-combine", version=1, domain="context",
                algorithm_id="context.score-v2", implementation_version="1",
                budget={"max_items": 20, "max_chars": 100_000},
            )
            return ContextApplication(
                SqliteContextAuthority(connection), context.require("access"), selected_policy,
                selected_profile, ScoreContextCombination(), tuple(sources),
            )
        except BaseException:
            connection.close()
            raise

    specs = [ComponentSpec(name="access", dependencies=(), factory=access_factory)]
    if "evidence" in enabled:
        specs.append(ComponentSpec(name="evidence", dependencies=("access",), factory=evidence_factory))
    if "graph" in enabled:
        specs.append(ComponentSpec(
            name="graph", dependencies=("access", "evidence"), factory=graph_factory
        ))
    if "memory" in enabled:
        memory_component_dependencies = ["access"]
        if "evidence" in enabled:
            memory_component_dependencies.append("evidence")
        if "graph" in memory_dependencies:
            memory_component_dependencies.append("graph")
        specs.append(ComponentSpec(
            name="memory",
            dependencies=tuple(memory_component_dependencies),
            factory=memory_factory,
        ))
    context_dependencies = tuple(
        ["access", *[name for name in ("evidence", "memory", "graph") if name in enabled]]
    )
    specs.append(ComponentSpec(name="context", dependencies=context_dependencies, factory=context_factory))
    assembly = ComponentAssembly(tuple(configuration.enabled_modules), tuple(specs))
    return ContextFoundationHost(
        configuration=configuration,
        access=assembly.get("access"),
        context=assembly.get("context"),
        evidence=assembly.get("evidence") if "evidence" in enabled else None,
        memory=assembly.get("memory") if "memory" in enabled else None,
        graph=assembly.get("graph") if "graph" in enabled else None,
        _assembly=assembly,
    )


def create_workflow_host(configuration: HostConfiguration) -> WorkflowFoundationHost:
    """Attach durable workflow authorities to an independently assembled domain host."""

    enabled = set(configuration.enabled_modules)
    workflow_modules = enabled & {"jobs", "applications"}
    if not workflow_modules:
        raise FoundationError(
            "module_set_invalid", "Workflow host requires jobs or applications"
        )
    unsupported = enabled - {
        "access", "evidence", "memory", "graph", "context", "jobs", "applications"
    }
    if unsupported:
        raise FoundationError(
            "module_implementation_unavailable",
            f"Workflow host has no implementation for: {', '.join(sorted(unsupported))}",
        )
    if "applications" in enabled and "memory" not in enabled:
        raise FoundationError(
            "module_dependency_missing", "Applications requires the Memory domain"
        )
    application_dependencies = set(configuration.module_dependencies.get("applications", ()))
    if not application_dependencies.issubset({"memory", "context"}):
        raise FoundationError(
            "module_dependency_invalid", "Applications dependencies may contain Memory and Context"
        )
    missing_dependencies = application_dependencies - enabled
    if missing_dependencies:
        raise FoundationError(
            "module_dependency_missing",
            "Applications required modules are disabled: "
            + ", ".join(sorted(missing_dependencies)),
        )

    base_modules = enabled - workflow_modules
    base_payload = configuration.model_dump(mode="json")
    base_payload.update({
        "configuration_id": f"{configuration.configuration_id}:domains",
        "enabled_modules": sorted(base_modules),
        "module_stores": {
            module: store
            for module, store in configuration.module_stores.items()
            if module in base_modules
        },
        "module_write_stores": {
            module: store
            for module, store in configuration.module_write_stores.items()
            if module in base_modules
        },
        "module_dependencies": {
            module: [dependency for dependency in dependencies if dependency in base_modules]
            for module, dependencies in configuration.module_dependencies.items()
            if module in base_modules
        },
        "fingerprint": "",
    })
    base_configuration = HostConfiguration.model_validate(base_payload)
    if "context" in base_modules:
        base = create_context_host(base_configuration)
    elif "graph" in base_modules:
        if base_modules != {"access", "evidence", "graph"}:
            raise FoundationError(
                "module_set_invalid",
                "Graph without Context can only be combined with Access and Evidence",
            )
        base = create_graph_host(base_configuration)
    elif "memory" in base_modules:
        base = create_memory_host(base_configuration)
    elif "evidence" in base_modules:
        base = create_evidence_host(base_configuration)
    else:
        base = create_access_host(base_configuration)

    jobs = None
    applications = None
    try:
        if "jobs" in enabled:
            store = _required_sqlite_store(configuration, "jobs", "PL-06 Jobs")
            handlers = {}
            if getattr(base, "graph", None) is not None:
                handlers["graph.bulk-proposal"] = GraphBulkProposalWorkflow(base.graph)
                handlers["graph.apply"] = GraphApplyWorkflow(base.graph)
            if getattr(base, "memory", None) is not None:
                handlers["memory.maintenance"] = MemoryMaintenanceWorkflow(base.memory)
            jobs = JobsApplication(
                SqliteJobAuthority(sqlite3.connect(
                    store.endpoint_ref, check_same_thread=False, timeout=10
                )),
                base.access,
                JobPolicy(policy_id="jobs", version=1),
                handlers,
            )
        if "applications" in enabled:
            store = _required_sqlite_store(
                configuration, "applications", "PL-06 Applications"
            )
            applications = AgentTurnWorkflow(
                SqliteTurnAuthority(sqlite3.connect(
                    store.endpoint_ref, check_same_thread=False, timeout=10
                )),
                base.access,
                ApplicationPolicy(policy_id="applications", version=1),
                memory=base.memory,
                context=getattr(base, "context", None),
            )
    except BaseException:
        if applications is not None:
            applications.close()
        if jobs is not None:
            jobs.close()
        base.close()
        raise
    return WorkflowFoundationHost(
        configuration=configuration,
        access=base.access,
        evidence=getattr(base, "evidence", None),
        memory=getattr(base, "memory", None),
        graph=getattr(base, "graph", None),
        context=getattr(base, "context", None),
        jobs=jobs,
        applications=applications,
        _base=base,
    )


def _required_sqlite_store(
    configuration: HostConfiguration, module: str, label: str
):
    store_id = configuration.module_stores.get(module)
    if store_id is None:
        raise FoundationError("module_store_missing", f"{label} needs its own authority store")
    store = configuration.stores[store_id]
    if store.driver != "sqlite" or not store.endpoint_ref:
        raise FoundationError(
            "store_driver_unavailable", f"{label} authority requires SQLite"
        )
    return store


def create_management_host(configuration: HostConfiguration) -> ManagementFoundationHost:
    enabled = set(configuration.enabled_modules)
    if "management" not in enabled:
        raise FoundationError("module_set_invalid", "Management host requires Management")
    base_modules = enabled - {"management"}
    unsupported = base_modules - {
        "access", "evidence", "memory", "graph", "context", "jobs", "applications"
    }
    if unsupported:
        raise FoundationError(
            "module_implementation_unavailable",
            f"Management host has no implementation for: {', '.join(sorted(unsupported))}",
        )
    payload = configuration.model_dump(mode="json")
    payload.update({
        "configuration_id": f"{configuration.configuration_id}:runtime",
        "enabled_modules": sorted(base_modules),
        "module_stores": {
            module: store for module, store in configuration.module_stores.items()
            if module in base_modules
        },
        "module_dependencies": {
            module: [dependency for dependency in dependencies if dependency in base_modules]
            for module, dependencies in configuration.module_dependencies.items()
            if module in base_modules
        },
        "fingerprint": "",
    })
    base_configuration = HostConfiguration.model_validate(payload)
    if {"jobs", "applications"} & base_modules:
        base = create_workflow_host(base_configuration)
    elif "context" in base_modules:
        base = create_context_host(base_configuration)
    elif "graph" in base_modules:
        base = create_graph_host(base_configuration)
    elif "memory" in base_modules:
        base = create_memory_host(base_configuration)
    elif "evidence" in base_modules:
        base = create_evidence_host(base_configuration)
    else:
        base = create_access_host(base_configuration)
    store = _required_sqlite_store(configuration, "management", "PL-07 Management")
    effect_root = Path(store.endpoint_ref).resolve().parent / "management-effects"
    release_kinds = (
        "domain_policy", "execution_profile", "extractor", "provider",
        "host_configuration", "graph_schema",
    )
    try:
        built_in_extractor = JsonPointerExtractor()
        trusted_extractors = (
            (built_in_extractor,)
            if built_in_extractor.implementation_id in configuration.trusted_algorithms
            else ()
        )
        management = ManagementApplication(
            SqliteManagementAuthority(sqlite3.connect(
                store.endpoint_ref, check_same_thread=False, timeout=10
            )),
            base.access,
            ManagementPolicy(policy_id="management", version=1),
            release_effects={
                kind: FilesystemReleaseEffect(effect_root, kind) for kind in release_kinds
            },
            index_effect=FilesystemIndexEffect(effect_root),
            transfer_effect=FilesystemTransferEffect(effect_root / "transfers"),
            extractor_runtime=ExtractorRuntime(trusted_extractors),
            extraction_sink=DomainExtractionProposalSink(
                memory=getattr(base, "memory", None),
                graph=getattr(base, "graph", None),
            ),
        )
    except BaseException:
        base.close()
        raise
    return ManagementFoundationHost(
        configuration=configuration,
        access=base.access,
        evidence=getattr(base, "evidence", None),
        memory=getattr(base, "memory", None),
        graph=getattr(base, "graph", None),
        context=getattr(base, "context", None),
        jobs=getattr(base, "jobs", None),
        applications=getattr(base, "applications", None),
        management=management,
        _base=base,
    )
