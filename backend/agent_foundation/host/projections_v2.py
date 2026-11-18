from __future__ import annotations

import os

from agent_foundation.algorithms.vector_v2 import DeterministicHashVectorizer
from agent_foundation.adapters.outbound.projection_v2 import (
    Neo4jResourceProjection,
    QdrantResourceProjection,
)
from agent_foundation.kernel.errors import FoundationError

from .configuration import HostConfiguration


class ProjectionResources:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, projection_id: str):
        try:
            return self._values[projection_id]
        except KeyError as exc:
            raise FoundationError("projection_not_enabled", f"Projection {projection_id!r} is not enabled") from exc

    def close(self) -> None:
        for value in reversed(tuple(self._values.values())):
            value.close()


def create_projection_resources(
    configuration: HostConfiguration, *, projection_ids: tuple[str, ...] | None = None
) -> ProjectionResources:
    requested = set(projection_ids) if projection_ids is not None else {
        projection_id
        for module, projection_ids in configuration.module_projections.items()
        if module in configuration.enabled_modules
        for projection_id in projection_ids
    }
    unknown = requested - set(configuration.projections)
    if unknown:
        raise FoundationError(
            "projection_not_configured",
            "Unknown projections: " + ", ".join(sorted(unknown)),
        )
    values: dict[str, object] = {}
    try:
        for projection_id in sorted(requested):
            spec = configuration.projections[projection_id]
            credential = os.environ.get(spec.credential_ref) if spec.credential_ref else None
            if spec.credential_ref and credential is None:
                raise FoundationError(
                    "store_credential_missing", f"Credential environment variable {spec.credential_ref!r} is missing"
                )
            if spec.driver == "neo4j":
                try:
                    from neo4j import GraphDatabase
                except ImportError as exc:
                    raise FoundationError("store_driver_missing", "Install agent-foundation[projections]") from exc
                auth = None
                if credential:
                    username, separator, password = credential.partition(":")
                    if not separator:
                        raise FoundationError("store_credential_invalid", "Neo4j credential must be username:password")
                    auth = (username, password)
                values[projection_id] = Neo4jResourceProjection(
                    GraphDatabase.driver(spec.endpoint_ref, auth=auth),
                    database=spec.database_or_collection,
                )
            else:
                import httpx

                headers = {"api-key": credential} if credential else {}
                values[projection_id] = QdrantResourceProjection(
                    httpx.Client(
                        base_url=spec.endpoint_ref.rstrip("/"), headers=headers,
                        timeout=20, trust_env=False,
                    ),
                    collection=spec.database_or_collection,
                    vectorizer=DeterministicHashVectorizer(),
                )
    except BaseException:
        ProjectionResources(values).close()
        raise
    return ProjectionResources(values)
