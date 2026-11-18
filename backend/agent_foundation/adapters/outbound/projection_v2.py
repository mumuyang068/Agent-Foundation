from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Protocol
from uuid import UUID

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef, ScopeRef


class TextVectorizer(Protocol):
    """Replaceable algorithm contract injected by the host composition root."""

    dimensions: int

    def vectorize(self, text: str) -> tuple[float, ...]: ...


def _scope_key(scope: ScopeRef) -> str:
    return json.dumps(scope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _point_id(ref: ResourceRef, generation_id: str) -> str:
    raw = f"{ref.domain}|{_scope_key(ref.scope)}|{ref.resource_type}|{ref.resource_id}|{ref.revision}|{generation_id}"
    digest = hashlib.sha256(raw.encode()).digest()[:16]
    return str(UUID(bytes=digest))


@dataclass(frozen=True)
class ProjectedResource:
    ref: ResourceRef
    text: str


class Neo4jResourceProjection:
    """Generation-scoped resource projection; never acts as domain authority."""

    def __init__(self, driver, *, database: str = "neo4j") -> None:
        self._driver = driver
        self._database = database
        self._driver.verify_connectivity()

    def replace_generation(
        self,
        scope: ScopeRef,
        *,
        domain: str,
        generation_id: str,
        resources: Iterable[ProjectedResource],
    ) -> int:
        rows = [
            {
                "resource_type": resource.ref.resource_type,
                "resource_id": resource.ref.resource_id,
                "revision": resource.ref.revision,
                "payload": resource.ref.model_dump_json(),
            }
            for resource in resources
            if resource.ref.scope == scope and resource.ref.domain == domain
        ]
        with self._driver.session(database=self._database) as session:
            session.run(
                "MATCH (r:AFProjectedResource {scope_key:$scope_key, domain:$domain, generation_id:$generation}) DETACH DELETE r",
                scope_key=_scope_key(scope), domain=domain, generation=generation_id,
            ).consume()
            session.run(
                """
                UNWIND $rows AS row
                CREATE (:AFProjectedResource {
                    scope_key:$scope_key, domain:$domain, generation_id:$generation,
                    resource_type:row.resource_type, resource_id:row.resource_id,
                    revision:row.revision, payload:row.payload
                })
                """,
                scope_key=_scope_key(scope), domain=domain, generation=generation_id, rows=rows,
            ).consume()
        return len(rows)

    def activate_generation(self, scope: ScopeRef, *, domain: str, generation_id: str) -> None:
        with self._driver.session(database=self._database) as session:
            count = session.run(
                "MATCH (r:AFProjectedResource {scope_key:$scope_key, domain:$domain, generation_id:$generation}) RETURN count(r) AS count",
                scope_key=_scope_key(scope), domain=domain, generation=generation_id,
            ).single()["count"]
            if count == 0:
                raise FoundationError("projection_generation_empty", "Cannot activate an empty generation")
            session.run(
                """
                MERGE (g:AFProjectionGeneration {scope_key:$scope_key, domain:$domain})
                SET g.generation_id=$generation
                """,
                scope_key=_scope_key(scope), domain=domain, generation=generation_id,
            ).consume()

    def candidates(
        self,
        scope: ScopeRef,
        *,
        domain: str,
        query: str,
        limit: int,
        resource_ids: tuple[str, ...] = (),
    ) -> tuple[ResourceRef, ...]:
        del query
        query = """
                MATCH (g:AFProjectionGeneration {scope_key:$scope_key, domain:$domain})
                MATCH (r:AFProjectedResource {scope_key:$scope_key, domain:$domain, generation_id:g.generation_id})
                __RESOURCE_FILTER__
                RETURN r.payload AS payload ORDER BY r.resource_id LIMIT $limit
                """.replace(
            "__RESOURCE_FILTER__",
            "WHERE r.resource_id IN $resource_ids" if resource_ids else "",
        )
        with self._driver.session(database=self._database) as session:
            result = session.run(
                query,
                scope_key=_scope_key(scope),
                domain=domain,
                limit=limit,
                resource_ids=list(resource_ids),
            )
            return tuple(ResourceRef.model_validate_json(row["payload"]) for row in result)

    def close(self) -> None:
        self._driver.close()


class QdrantResourceProjection:
    """HTTP projection with explicit scope and active-generation filters."""

    def __init__(self, client, *, collection: str, vectorizer: TextVectorizer) -> None:
        self._client = client
        self._collection = collection
        self._vectorizer = vectorizer
        response = self._client.put(
            f"/collections/{collection}",
            json={"vectors": {"size": vectorizer.dimensions, "distance": "Cosine"}},
        )
        if response.status_code not in {200, 201, 409}:
            raise FoundationError("projection_unavailable", f"Qdrant collection failed: {response.status_code}")

    def replace_generation(
        self,
        scope: ScopeRef,
        *,
        domain: str,
        generation_id: str,
        resources: Iterable[ProjectedResource],
    ) -> int:
        scope_key = _scope_key(scope)
        self._request("POST", f"/collections/{self._collection}/points/delete?wait=true", json={
            "filter": {"must": [
                {"key": "scope_key", "match": {"value": scope_key}},
                {"key": "domain", "match": {"value": domain}},
                {"key": "generation_id", "match": {"value": generation_id}},
            ]}
        })
        points = [
            {
                "id": _point_id(resource.ref, generation_id),
                "vector": list(self._vectorizer.vectorize(resource.text)),
                "payload": {
                "scope_key": scope_key, "domain": domain, "generation_id": generation_id,
                "resource_id": resource.ref.resource_id,
                "content_digest": hashlib.sha256(resource.text.encode("utf-8")).hexdigest(),
                "ref": resource.ref.model_dump(mode="json"),
                },
            }
            for resource in resources
            if resource.ref.scope == scope and resource.ref.domain == domain
        ]
        if points:
            self._request("PUT", f"/collections/{self._collection}/points?wait=true", json={"points": points})
        return len(points)

    def activate_generation(self, scope: ScopeRef, *, domain: str, generation_id: str) -> None:
        found = self._scroll(scope, domain=domain, generation_id=generation_id, limit=1)
        if not found:
            raise FoundationError("projection_generation_empty", "Cannot activate an empty generation")
        marker = ResourceRef(
            domain=domain, scope=scope, resource_type="projection_generation",
            resource_id="active", revision=generation_id,
        )
        marker_identity = marker.model_copy(update={"revision": None})
        self._request("POST", f"/collections/{self._collection}/points/delete?wait=true", json={
            "filter": {"must": [
                {"key": "scope_key", "match": {"value": _scope_key(scope)}},
                {"key": "domain", "match": {"value": domain}},
                {"key": "generation_id", "match": {"value": "__marker__"}},
            ]}
        })
        self._request("PUT", f"/collections/{self._collection}/points?wait=true", json={"points": [{
            "id": _point_id(marker_identity, "marker"),
            "vector": list(self._vectorizer.vectorize(
                f"active projection {domain} {generation_id} {_scope_key(scope)}"
            )),
            "payload": {
                "scope_key": _scope_key(scope), "domain": domain, "generation_id": "__marker__",
                "resource_id": "__active_generation__", "active_generation": generation_id,
                "ref": marker.model_dump(mode="json"),
            }
        }]})

    def candidates(
        self,
        scope: ScopeRef,
        *,
        domain: str,
        query: str,
        limit: int,
        resource_ids: tuple[str, ...] = (),
    ) -> tuple[ResourceRef, ...]:
        marker = self._scroll(scope, domain=domain, generation_id="__marker__", limit=1)
        if not marker:
            return ()
        generation = marker[0]["payload"]["active_generation"]
        filters = self._filters(
            scope, domain=domain, generation_id=generation, resource_ids=resource_ids
        )
        result = self._request(
            "POST",
            f"/collections/{self._collection}/points/query",
            json={
                "query": list(self._vectorizer.vectorize(query)),
                "filter": {"must": filters},
                "limit": limit,
                "with_payload": True,
                "with_vector": False,
            },
        )
        rows = result["result"]["points"]
        return tuple(ResourceRef.model_validate(row["payload"]["ref"]) for row in rows)

    def _filters(
        self,
        scope: ScopeRef,
        *,
        domain: str,
        generation_id: str,
        resource_ids: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        filters: list[dict[str, object]] = [
            {"key": "scope_key", "match": {"value": _scope_key(scope)}},
            {"key": "domain", "match": {"value": domain}},
            {"key": "generation_id", "match": {"value": generation_id}},
        ]
        if resource_ids:
            filters.append({"key": "resource_id", "match": {"any": list(resource_ids)}})
        return filters

    def _scroll(
        self,
        scope: ScopeRef,
        *,
        domain: str,
        generation_id: str,
        limit: int,
        resource_ids: tuple[str, ...] = (),
    ):
        filters = self._filters(
            scope, domain=domain, generation_id=generation_id, resource_ids=resource_ids
        )
        result = self._request("POST", f"/collections/{self._collection}/points/scroll", json={
            "filter": {"must": filters},
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
        })
        return result["result"]["points"]

    def _request(self, method: str, path: str, **kwargs):
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 300:
            raise FoundationError("projection_unavailable", f"Qdrant request failed: {response.status_code}", retryable=True)
        return response.json()

    def close(self) -> None:
        self._client.close()
