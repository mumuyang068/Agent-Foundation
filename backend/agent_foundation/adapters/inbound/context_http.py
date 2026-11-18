from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, Header
from pydantic import Field

from agent_foundation.kernel.primitives import ResourceRef
from agent_foundation.modules.context.contracts import ContextAssembly

from .access_http import RequestModel
from .memory_http import bearer


class AssembleRequest(RequestModel):
    query: str = Field(min_length=1, max_length=20_000)
    source_domains: tuple[Literal["memory", "evidence", "graph"], ...]
    pinned_refs: tuple[ResourceRef, ...] = ()
    max_items: int | None = Field(default=None, ge=1)
    max_chars: int | None = Field(default=None, ge=1)


class SnapshotRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    assembly: ContextAssembly
    retention: Literal["full", "redacted", "provenance_only"] = "provenance_only"


class SnapshotMetadataRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    assembly_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResidentReplaceRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    expected_revision: int | None = Field(default=None, ge=1)
    assembly: ContextAssembly


def add_context_routes(app: FastAPI, context) -> None:
    @app.post("/api/foundation/v1/context/assemblies")
    def assemble(payload: AssembleRequest, authorization: str | None = Header(default=None)):
        return context.assemble(
            bearer(authorization), query=payload.query, source_domains=payload.source_domains,
            pinned_refs=payload.pinned_refs, max_items=payload.max_items, max_chars=payload.max_chars,
        )

    @app.post("/api/foundation/v1/context/snapshots")
    def snapshot(payload: SnapshotRequest, authorization: str | None = Header(default=None)):
        return context.create_snapshot(
            bearer(authorization), payload.assembly,
            retention=payload.retention, operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/context/snapshot-metadata")
    def snapshot_metadata(
        payload: SnapshotMetadataRequest, authorization: str | None = Header(default=None)
    ):
        return context.create_snapshot_metadata(
            bearer(authorization), assembly_fingerprint=payload.assembly_fingerprint,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/context/snapshots/{snapshot_id}")
    def get_snapshot(snapshot_id: str, authorization: str | None = Header(default=None)):
        return context.get_snapshot(bearer(authorization), snapshot_id)

    @app.get("/api/foundation/v1/context/snapshots")
    def list_snapshots(limit: int = 20, authorization: str | None = Header(default=None)):
        return context.list_snapshots(bearer(authorization), limit=limit)

    @app.post("/api/foundation/v1/context/snapshots/{snapshot_id}/replay")
    def replay(snapshot_id: str, authorization: str | None = Header(default=None)):
        return context.replay(bearer(authorization), snapshot_id)

    @app.get("/api/foundation/v1/context/snapshots/compare/{left_id}/{right_id}")
    def compare(left_id: str, right_id: str, authorization: str | None = Header(default=None)):
        return context.compare(bearer(authorization), left_id, right_id)

    @app.post("/api/foundation/v1/context/resident/preview")
    def preview_resident(assembly: ContextAssembly, authorization: str | None = Header(default=None)):
        return context.preview_resident(bearer(authorization), assembly)

    @app.put("/api/foundation/v1/context/resident")
    def replace_resident(
        payload: ResidentReplaceRequest, authorization: str | None = Header(default=None)
    ):
        return context.replace_resident(
            bearer(authorization), payload.assembly, expected_revision=payload.expected_revision,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/context/resident")
    def get_resident(authorization: str | None = Header(default=None)):
        return context.get_resident(bearer(authorization))

    @app.post("/api/foundation/v1/context/resident/replay")
    def replay_resident(authorization: str | None = Header(default=None)):
        return context.replay_resident(bearer(authorization))

    @app.get("/api/foundation/v1/context/operations/{operation_id}")
    def operation_receipt(operation_id: str, authorization: str | None = Header(default=None)):
        return context.get_operation_receipt(bearer(authorization), operation_id)
