from __future__ import annotations

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef
from agent_foundation.modules.context.contracts import ContextCandidate


class EvidenceContextSource:
    domain = "evidence"

    def __init__(self, application) -> None:
        self._application = application

    def search(self, token: str, *, query: str, limit: int):
        result = self._application.search(token, query=query, limit=limit)
        return tuple(
            ContextCandidate(ref=hit.revision.ref(), content=hit.revision.content, score=max(0.0, hit.score))
            for hit in result.hits
        )

    def resolve(self, token: str, ref: ResourceRef):
        if ref.domain != self.domain or ref.revision != ref.resource_id:
            raise FoundationError("context_resource_not_found", "Evidence reference is not exact")
        revision = self._application.get(token, ref.resource_id)
        if revision.ref() != ref or not self._application.is_current(ref):
            raise FoundationError("context_resource_not_found", "Evidence reference is not current")
        return ContextCandidate(ref=ref, content=revision.content, score=1.0)


class MemoryContextSource:
    domain = "memory"

    def __init__(self, application) -> None:
        self._application = application

    def search(self, token: str, *, query: str, limit: int):
        result = self._application.recall(token, query=query, limit=limit)
        return tuple(
            ContextCandidate(ref=hit.entry.ref(), content=hit.entry.content, score=max(0.0, hit.score))
            for hit in result.hits
        )

    def resolve(self, token: str, ref: ResourceRef):
        if ref.domain != self.domain or ref.revision is None:
            raise FoundationError("context_resource_not_found", "Memory reference is not exact")
        entry = self._application.get(token, ref.resource_id)
        if entry.ref() != ref or not self._application.is_current(ref):
            raise FoundationError("context_resource_not_found", "Memory reference is not current")
        return ContextCandidate(ref=ref, content=entry.content, score=1.0)


class GraphContextSource:
    domain = "graph"

    def __init__(self, application) -> None:
        self._application = application

    def search(self, token: str, *, query: str, limit: int):
        result = self._application.query(token, query=query, limit=limit)
        return tuple(
            ContextCandidate(ref=hit.node.ref(), content=hit.node.label, score=max(0.0, hit.score))
            for hit in result.hits
        )

    def resolve(self, token: str, ref: ResourceRef):
        if ref.domain != self.domain or ref.resource_type != "graph_node" or ref.revision is None:
            raise FoundationError("context_resource_not_found", "Graph reference is not an exact node")
        node = self._application.get_node(token, ref.resource_id)
        if node.ref() != ref or not self._application.is_current(ref):
            raise FoundationError("context_resource_not_found", "Graph reference is not current")
        return ContextCandidate(ref=ref, content=node.label, score=1.0)
