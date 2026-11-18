from __future__ import annotations

from fastapi import FastAPI, Header
from pydantic import Field

from .access_http import RequestModel
from .memory_http import bearer


class PlanJobRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    kind: str = Field(min_length=1, max_length=160)
    payload: dict = Field(default_factory=dict)


class AuthorizeJobRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    expected_revision: int = Field(ge=1)


class RunJobRequest(RequestModel):
    worker_id: str = Field(min_length=1, max_length=240)
    lease_seconds: int = Field(default=30, ge=1, le=3600)


class CancelJobRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)


def add_jobs_routes(app: FastAPI, jobs) -> None:
    @app.post("/api/foundation/v1/jobs")
    def plan(payload: PlanJobRequest, authorization: str | None = Header(default=None)):
        return jobs.plan(
            bearer(authorization), kind=payload.kind, payload=payload.payload,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/jobs")
    def list_jobs(limit: int = 20, authorization: str | None = Header(default=None)):
        return jobs.list(bearer(authorization), limit=limit)

    # Keep fixed sub-resources ahead of the dynamic job route. Starlette resolves
    # routes in declaration order, so otherwise "operations" and "outbox" are
    # interpreted as job identifiers.
    @app.get("/api/foundation/v1/jobs/operations/{operation_id}")
    def receipt(operation_id: str, authorization: str | None = Header(default=None)):
        return jobs.get_operation_receipt(bearer(authorization), operation_id)

    @app.get("/api/foundation/v1/jobs/outbox/pending")
    def outbox(limit: int = 100, authorization: str | None = Header(default=None)):
        return jobs.list_outbox(bearer(authorization), limit=limit)

    @app.post("/api/foundation/v1/jobs/outbox/{event_id}/acknowledge")
    def acknowledge(event_id: str, authorization: str | None = Header(default=None)):
        return jobs.acknowledge_outbox(bearer(authorization), event_id)

    @app.get("/api/foundation/v1/jobs/{job_id}")
    def get(job_id: str, authorization: str | None = Header(default=None)):
        return jobs.get(bearer(authorization), job_id)

    @app.post("/api/foundation/v1/jobs/{job_id}/authorize")
    def authorize(
        job_id: str, payload: AuthorizeJobRequest,
        authorization: str | None = Header(default=None),
    ):
        return jobs.authorize_job(
            bearer(authorization), job_id, expected_revision=payload.expected_revision,
            operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/jobs/{job_id}/run-next")
    def run_next(
        job_id: str, payload: RunJobRequest,
        authorization: str | None = Header(default=None),
    ):
        return jobs.run_next(
            bearer(authorization), job_id, worker_id=payload.worker_id,
            lease_seconds=payload.lease_seconds,
        )

    @app.post("/api/foundation/v1/jobs/{job_id}/cancel")
    def cancel(
        job_id: str, payload: CancelJobRequest,
        authorization: str | None = Header(default=None),
    ):
        return jobs.cancel(bearer(authorization), job_id, operation_id=payload.operation_id)
