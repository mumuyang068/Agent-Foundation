from __future__ import annotations

from agent_foundation.kernel.primitives import ResourceRef, ScopeRef
from agent_foundation.modules.evidence.ports import EvidenceAuthorityReader
from agent_foundation.modules.graph.ports import GraphAuthorityReader
from agent_foundation.modules.memory.ports import MemoryAuthorityReader


def _valid_ref(
    ref: object,
    *,
    domain: str,
    scope: ScopeRef,
    resource_type: str,
) -> bool:
    return bool(
        isinstance(ref, ResourceRef)
        and ref.domain == domain
        and ref.scope == scope
        and ref.resource_type == resource_type
    )


class _OwnedProjection:
    def __init__(self, projection, resources) -> None:
        self._projection = projection
        self._resources = resources

    def close(self) -> None:
        self._resources.close()


class ProjectedMemoryCandidateSource(_OwnedProjection):
    """Returns bounded Memory references; the domain resolves authority state."""

    def __init__(self, projection, reader: MemoryAuthorityReader, resources) -> None:
        super().__init__(projection, resources)

    def candidates(
        self, scope: ScopeRef, *, query: str, limit: int
    ) -> tuple[ResourceRef, ...]:
        return tuple(
            ref
            for ref in self._projection.candidates(
                scope, domain="memory", query=query, limit=limit
            )
            if _valid_ref(ref, domain="memory", scope=scope, resource_type="memory")
        )


class ProjectedEvidenceCandidateSource(_OwnedProjection):
    """Applies exact source/revision filters before the projection limit."""

    def __init__(self, projection, reader: EvidenceAuthorityReader, resources) -> None:
        super().__init__(projection, resources)
        self._reader = reader

    def candidates(
        self,
        scope: ScopeRef,
        *,
        query: str,
        source_ids: tuple[str, ...],
        revision_ids: tuple[str, ...],
        limit: int,
    ) -> tuple[ResourceRef, ...]:
        exact_ids = set(revision_ids)
        source_revision_ids: set[str] = set()
        for source_id in source_ids:
            page = self._reader.list_revisions(
                scope,
                states=("active",),
                source_id=source_id,
                limit=limit,
                cursor=None,
            )
            source_revision_ids.update(item.revision_id for item in page.items)

        allowed_ids: set[str] | None
        if exact_ids and source_ids:
            allowed_ids = exact_ids & source_revision_ids
        elif exact_ids:
            allowed_ids = exact_ids
        elif source_ids:
            allowed_ids = source_revision_ids
        else:
            allowed_ids = None
        if allowed_ids == set():
            return ()

        return tuple(
            ref
            for ref in self._projection.candidates(
                scope,
                domain="evidence",
                query=query,
                limit=limit,
                resource_ids=tuple(sorted(allowed_ids)) if allowed_ids is not None else (),
            )
            if _valid_ref(
                ref,
                domain="evidence",
                scope=scope,
                resource_type="evidence_revision",
            )
        )


class ProjectedGraphCandidateSource(_OwnedProjection):
    """Returns bounded Graph node references; the domain resolves authority state."""

    def __init__(self, projection, reader: GraphAuthorityReader, resources) -> None:
        super().__init__(projection, resources)

    def candidates(
        self, scope: ScopeRef, *, query: str, limit: int
    ) -> tuple[ResourceRef, ...]:
        return tuple(
            ref
            for ref in self._projection.candidates(
                scope, domain="graph", query=query, limit=limit
            )
            if _valid_ref(ref, domain="graph", scope=scope, resource_type="graph_node")
        )


class AuthorityMemoryCandidateSource:
    """Default bounded candidate reader for an authority-only deployment."""

    def __init__(self, reader: MemoryAuthorityReader) -> None:
        self._reader = reader

    def candidates(
        self, scope: ScopeRef, *, query: str, limit: int
    ) -> tuple[ResourceRef, ...]:
        del query
        return tuple(
            item.ref()
            for item in self._reader.list_current(scope, states=("active",), limit=limit)
        )


class AuthorityEvidenceCandidateSource:
    """Default source that applies Evidence filters before truncation."""

    def __init__(self, reader: EvidenceAuthorityReader) -> None:
        self._reader = reader

    def candidates(
        self,
        scope: ScopeRef,
        *,
        query: str,
        source_ids: tuple[str, ...],
        revision_ids: tuple[str, ...],
        limit: int,
    ) -> tuple[ResourceRef, ...]:
        del query
        exact_ids = set(revision_ids)
        selected: dict[str, ResourceRef] = {}
        if exact_ids:
            for revision_id in sorted(exact_ids):
                item = self._reader.get_revision(scope, revision_id)
                if (
                    item is not None
                    and item.status == "active"
                    and (not source_ids or item.source_id in source_ids)
                    and self._reader.is_current(item.ref())
                ):
                    selected.setdefault(item.revision_id, item.ref())
                    if len(selected) == limit:
                        break
            return tuple(selected.values())

        requested_sources = source_ids or (None,)
        for source_id in requested_sources:
            page = self._reader.list_revisions(
                scope,
                states=("active",),
                source_id=source_id,
                limit=limit,
                cursor=None,
            )
            for item in page.items:
                if self._reader.is_current(item.ref()):
                    selected.setdefault(item.revision_id, item.ref())
                    if len(selected) == limit:
                        return tuple(selected.values())
        return tuple(selected.values())


class AuthorityGraphCandidateSource:
    """Default bounded candidate reader for an authority-only deployment."""

    def __init__(self, reader: GraphAuthorityReader) -> None:
        self._reader = reader

    def candidates(
        self, scope: ScopeRef, *, query: str, limit: int
    ) -> tuple[ResourceRef, ...]:
        del query
        return tuple(item.ref() for item in self._reader.list_nodes(scope, limit=limit))
