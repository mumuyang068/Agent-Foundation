from __future__ import annotations

from fastapi import FastAPI, Header, Query
from pydantic import Field

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef
from agent_foundation.modules.evidence.contracts import SourceRange
from agent_foundation.modules.evidence.ports import EvidenceService

from .access_http import RequestModel


class AdmitRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    source_id: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1)
    source_range: SourceRange | None = None


class SearchRequest(RequestModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1)
    source_ids: tuple[str, ...] = ()
    revision_ids: tuple[str, ...] = ()


class SupportRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    target: ResourceRef
    evidence_refs: tuple[ResourceRef, ...] = Field(min_length=1)


def bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise FoundationError("session_inactive", "A Bearer credential is required")
    return authorization.removeprefix("Bearer ")


def add_evidence_routes(app: FastAPI, evidence: EvidenceService) -> None:
    @app.post("/api/foundation/v1/evidence/revisions")
    def admit(payload: AdmitRequest, authorization: str | None = Header(default=None)):
        return evidence.admit(
            bearer(authorization),
            source_id=payload.source_id,
            content=payload.content,
            source_range=payload.source_range,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/evidence/revisions/{revision_id}")
    def get_revision(revision_id: str, authorization: str | None = Header(default=None)):
        return evidence.get(bearer(authorization), revision_id)

    @app.get("/api/foundation/v1/evidence/revisions")
    def list_revisions(
        states: list[str] = Query(default=["active"]),
        source_id: str | None = None,
        limit: int = Query(default=8, ge=1, le=100),
        cursor: str | None = None,
        authorization: str | None = Header(default=None),
    ):
        return evidence.list(
            bearer(authorization),
            states=tuple(states),
            source_id=source_id,
            limit=limit,
            cursor=cursor,
        )

    @app.get("/api/foundation/v1/evidence/operations/{operation_id}")
    def get_operation_receipt(operation_id: str, authorization: str | None = Header(default=None)):
        return evidence.get_operation_receipt(bearer(authorization), operation_id)

    @app.get("/api/foundation/v1/evidence/retrieval/status")
    def retrieval_status(authorization: str | None = Header(default=None)):
        return evidence.retrieval_status(bearer(authorization))

    @app.get("/api/foundation/v1/evidence/authority/watermark")
    def retrieval_watermark(authorization: str | None = Header(default=None)):
        return {"authority_watermark": evidence.retrieval_watermark(bearer(authorization))}

    @app.post("/api/foundation/v1/evidence/search")
    def search(payload: SearchRequest, authorization: str | None = Header(default=None)):
        return evidence.search(
            bearer(authorization),
            query=payload.query,
            limit=payload.limit,
            source_ids=payload.source_ids,
            revision_ids=payload.revision_ids,
        )

    @app.post("/api/foundation/v1/evidence/revisions/{revision_id}/revoke")
    def revoke(revision_id: str, operation_id: str, authorization: str | None = Header(default=None)):
        return evidence.revoke(bearer(authorization), revision_id, operation_id=operation_id)

    @app.post("/api/foundation/v1/evidence/revisions/{revision_id}/revalidate")
    def revalidate(revision_id: str, operation_id: str, authorization: str | None = Header(default=None)):
        return evidence.revalidate(bearer(authorization), revision_id, operation_id=operation_id)

    @app.post("/api/foundation/v1/evidence/supports")
    def support(payload: SupportRequest, authorization: str | None = Header(default=None)):
        return evidence.support(
            bearer(authorization),
            target=payload.target,
            evidence_refs=payload.evidence_refs,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/evidence/supports/{support_id}")
    def get_support(support_id: str, authorization: str | None = Header(default=None)):
        return evidence.get_support(bearer(authorization), support_id)
