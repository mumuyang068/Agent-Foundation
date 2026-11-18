from __future__ import annotations

from fastapi import FastAPI, Header
from pydantic import Field

from agent_foundation.kernel.primitives import ResourceRef

from .access_http import RequestModel
from .memory_http import bearer


class PrepareTurnRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    application_id: str = Field(min_length=1, max_length=160)
    query: str = Field(min_length=1, max_length=20_000)
    use_context: bool = False
    source_domains: tuple[str, ...] = ()
    pinned_refs: tuple[ResourceRef, ...] = ()


class ObserveTurnRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=20_000)
    source_refs: tuple[ResourceRef, ...] = ()


class OutcomeTurnRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    value: float = Field(ge=-1, le=1)
    expected_memory_revision: int = Field(ge=1)


def add_applications_routes(app: FastAPI, applications) -> None:
    @app.post("/api/foundation/v1/applications/turns")
    def prepare(payload: PrepareTurnRequest, authorization: str | None = Header(default=None)):
        turn, assembly = applications.prepare(
            bearer(authorization), application_id=payload.application_id,
            query=payload.query, use_context=payload.use_context,
            source_domains=payload.source_domains, pinned_refs=payload.pinned_refs,
            operation_id=payload.operation_id,
        )
        return {"turn": turn, "context": assembly}

    @app.get("/api/foundation/v1/applications/turns/{turn_id}")
    def get(turn_id: str, authorization: str | None = Header(default=None)):
        return applications.get(bearer(authorization), turn_id)

    @app.post("/api/foundation/v1/applications/turns/{turn_id}/observations")
    def observe(
        turn_id: str, payload: ObserveTurnRequest,
        authorization: str | None = Header(default=None),
    ):
        turn, memory = applications.observe(
            bearer(authorization), turn_id, content=payload.content,
            source_refs=payload.source_refs, operation_id=payload.operation_id,
        )
        return {"turn": turn, "memory": memory}

    @app.post("/api/foundation/v1/applications/turns/{turn_id}/outcomes")
    def outcome(
        turn_id: str, payload: OutcomeTurnRequest,
        authorization: str | None = Header(default=None),
    ):
        return applications.record_outcome(
            bearer(authorization), turn_id, value=payload.value,
            expected_memory_revision=payload.expected_memory_revision,
            operation_id=payload.operation_id,
        )
