from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, Header
from pydantic import Field

from agent_foundation.kernel.primitives import ResourceRef
from .access_http import RequestModel
from .memory_http import bearer


class StageReleaseRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    kind: Literal["domain_policy", "execution_profile", "extractor", "provider", "host_configuration", "graph_schema"]
    artifact_id: str = Field(min_length=1, max_length=240)
    version: int = Field(ge=1)
    payload: dict


class TransitionRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    expected_revision: int = Field(ge=1)


class BeginIndexRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    domain: Literal["memory", "evidence", "graph", "context"]
    execution_profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str = Field(min_length=1, max_length=240)
    artifact_path: str = Field(min_length=1, max_length=1000)
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvaluationRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    subject_ref: str = Field(min_length=1, max_length=300)
    cases: int = Field(ge=1)
    passed: int = Field(ge=0)
    metrics: dict[str, float] = Field(default_factory=dict)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class TransferRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    kind: Literal["handoff", "migration"]
    destination: str = Field(min_length=1, max_length=500)
    resource_refs: tuple[ResourceRef, ...] = Field(min_length=1)
    payload: dict


class BackupRequest(RequestModel):
    destination: str = Field(min_length=1, max_length=1000)


class RestoreRequest(RequestModel):
    manifest_path: str = Field(min_length=1, max_length=1000)
    target_configuration: dict


class ExtractorFixtureRequest(RequestModel):
    fixture_id: str = Field(min_length=1, max_length=160)
    input_payload: dict
    expected_status: Literal["candidate", "no_candidate"]
    expected_value: object | None = None


class CertifyExtractorRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    fixtures: tuple[ExtractorFixtureRequest, ...] = Field(min_length=2, max_length=8)
    reason: str = Field(min_length=1, max_length=500)


class RevokeExtractorCertificateRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=500)


class ExecuteExtractorRequest(RequestModel):
    operation_id: str = Field(min_length=1, max_length=240)
    event_id: str = Field(min_length=1, max_length=240)
    schema_id: str = Field(min_length=1, max_length=240)
    schema_version: int = Field(ge=1)
    input_payload: dict
    source_refs: tuple[ResourceRef, ...] = ()


def add_management_routes(app: FastAPI, management, *, backup, restore) -> None:
    @app.get("/api/foundation/v1/management/diagnostics")
    def diagnostics(authorization: str | None = Header(default=None)):
        return management.diagnostics(bearer(authorization))

    @app.get("/api/foundation/v1/management/operations/{operation_id}")
    def operation_receipt(operation_id: str, authorization: str | None = Header(default=None)):
        return management.get_operation_receipt(bearer(authorization), operation_id)

    @app.post("/api/foundation/v1/management/releases")
    def stage_release(payload: StageReleaseRequest, authorization: str | None = Header(default=None)):
        return management.stage_release(bearer(authorization), kind=payload.kind, artifact_id=payload.artifact_id, version=payload.version, payload=payload.payload, operation_id=payload.operation_id)

    @app.get("/api/foundation/v1/management/releases/active")
    def active_release(kind: str, artifact_id: str, authorization: str | None = Header(default=None)):
        return management.get_active_release(bearer(authorization), kind=kind, artifact_id=artifact_id)

    @app.get("/api/foundation/v1/management/releases/{release_id}")
    def get_release(release_id: str, authorization: str | None = Header(default=None)):
        return management.get_release(bearer(authorization), release_id)

    @app.get("/api/foundation/v1/management/releases")
    def list_releases(
        kind: str | None = None, status: str | None = None, limit: int = 20,
        authorization: str | None = Header(default=None),
    ):
        return management.list_releases(
            bearer(authorization), kind=kind, status=status, limit=limit
        )

    @app.post("/api/foundation/v1/management/releases/{release_id}/approve")
    def approve_release(release_id: str, payload: TransitionRequest, authorization: str | None = Header(default=None)):
        return management.approve_release(bearer(authorization), release_id, expected_revision=payload.expected_revision, operation_id=payload.operation_id)

    @app.post("/api/foundation/v1/management/releases/{release_id}/activate")
    def activate_release(release_id: str, payload: TransitionRequest, authorization: str | None = Header(default=None)):
        return management.activate_release(bearer(authorization), release_id, expected_revision=payload.expected_revision, operation_id=payload.operation_id)

    @app.post("/api/foundation/v1/management/extractors/{release_id}/certifications")
    def certify_extractor(
        release_id: str,
        payload: CertifyExtractorRequest,
        authorization: str | None = Header(default=None),
    ):
        return management.certify_extractor(
            bearer(authorization), release_id, fixtures=payload.fixtures,
            reason=payload.reason, operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/management/extractor-certificates/{certificate_id}")
    def get_extractor_certificate(
        certificate_id: str, authorization: str | None = Header(default=None)
    ):
        return management.get_extractor_certificate(bearer(authorization), certificate_id)

    @app.post("/api/foundation/v1/management/extractor-certificates/{certificate_id}/revoke")
    def revoke_extractor_certificate(
        certificate_id: str,
        payload: RevokeExtractorCertificateRequest,
        authorization: str | None = Header(default=None),
    ):
        return management.revoke_extractor_certificate(
            bearer(authorization), certificate_id,
            expected_fingerprint=payload.expected_fingerprint,
            reason=payload.reason, operation_id=payload.operation_id,
        )

    @app.post("/api/foundation/v1/management/extractors/{release_id}/execute")
    def execute_extractor(
        release_id: str,
        payload: ExecuteExtractorRequest,
        authorization: str | None = Header(default=None),
    ):
        return management.execute_extractor(
            bearer(authorization), release_id,
            event_id=payload.event_id, schema_id=payload.schema_id,
            schema_version=payload.schema_version,
            input_payload=payload.input_payload, source_refs=payload.source_refs,
            operation_id=payload.operation_id,
        )

    @app.get("/api/foundation/v1/management/extractor-executions/{event_id}")
    def get_extractor_execution(
        event_id: str, authorization: str | None = Header(default=None)
    ):
        return management.get_extractor_execution(bearer(authorization), event_id)

    @app.post("/api/foundation/v1/management/indexes")
    def begin_index(payload: BeginIndexRequest, authorization: str | None = Header(default=None)):
        return management.begin_index(bearer(authorization), domain=payload.domain, execution_profile_fingerprint=payload.execution_profile_fingerprint, source_revision=payload.source_revision, artifact_path=payload.artifact_path, artifact_digest=payload.artifact_digest, operation_id=payload.operation_id)

    @app.get("/api/foundation/v1/management/indexes/active/{domain}")
    def active_index(domain: str, authorization: str | None = Header(default=None)):
        return management.get_active_index(bearer(authorization), domain)

    @app.get("/api/foundation/v1/management/indexes/{generation_id}")
    def get_index(generation_id: str, authorization: str | None = Header(default=None)):
        return management.get_index(bearer(authorization), generation_id)

    @app.get("/api/foundation/v1/management/indexes")
    def list_indexes(
        domain: str | None = None, status: str | None = None, limit: int = 20,
        authorization: str | None = Header(default=None),
    ):
        return management.list_indexes(
            bearer(authorization), domain=domain, status=status, limit=limit
        )

    @app.post("/api/foundation/v1/management/indexes/{generation_id}/verify")
    def verify_index(generation_id: str, payload: TransitionRequest, authorization: str | None = Header(default=None)):
        return management.verify_index(bearer(authorization), generation_id, expected_revision=payload.expected_revision, operation_id=payload.operation_id)

    @app.post("/api/foundation/v1/management/indexes/{generation_id}/activate")
    def activate_index(generation_id: str, payload: TransitionRequest, authorization: str | None = Header(default=None)):
        return management.activate_index(bearer(authorization), generation_id, expected_revision=payload.expected_revision, operation_id=payload.operation_id)

    @app.post("/api/foundation/v1/management/evaluations")
    def evaluation(payload: EvaluationRequest, authorization: str | None = Header(default=None)):
        return management.record_evaluation(bearer(authorization), subject_ref=payload.subject_ref, cases=payload.cases, passed=payload.passed, metrics=payload.metrics, source_digest=payload.source_digest, operation_id=payload.operation_id)

    @app.get("/api/foundation/v1/management/evaluations/{evaluation_id}")
    def get_evaluation(evaluation_id: str, authorization: str | None = Header(default=None)):
        return management.get_evaluation(bearer(authorization), evaluation_id)

    @app.get("/api/foundation/v1/management/evaluations")
    def list_evaluations(
        subject_ref: str | None = None, limit: int = 20,
        authorization: str | None = Header(default=None),
    ):
        return management.list_evaluations(
            bearer(authorization), subject_ref=subject_ref, limit=limit
        )

    @app.post("/api/foundation/v1/management/transfers")
    def propose_transfer(payload: TransferRequest, authorization: str | None = Header(default=None)):
        return management.propose_transfer(bearer(authorization), kind=payload.kind, destination=payload.destination, resource_refs=payload.resource_refs, payload=payload.payload, operation_id=payload.operation_id)

    @app.get("/api/foundation/v1/management/transfers/{transfer_id}")
    def get_transfer(transfer_id: str, authorization: str | None = Header(default=None)):
        return management.get_transfer(bearer(authorization), transfer_id)

    @app.get("/api/foundation/v1/management/transfers")
    def list_transfers(
        kind: str | None = None, status: str | None = None, limit: int = 20,
        authorization: str | None = Header(default=None),
    ):
        return management.list_transfers(
            bearer(authorization), kind=kind, status=status, limit=limit
        )

    @app.post("/api/foundation/v1/management/transfers/{transfer_id}/approve")
    def approve_transfer(transfer_id: str, payload: TransitionRequest, authorization: str | None = Header(default=None)):
        return management.approve_transfer(bearer(authorization), transfer_id, expected_revision=payload.expected_revision, operation_id=payload.operation_id)

    @app.post("/api/foundation/v1/management/transfers/{transfer_id}/execute")
    def execute_transfer(transfer_id: str, payload: TransitionRequest, authorization: str | None = Header(default=None)):
        return management.execute_transfer(bearer(authorization), transfer_id, expected_revision=payload.expected_revision, operation_id=payload.operation_id)

    @app.post("/api/foundation/v1/management/backups")
    def create_backup(payload: BackupRequest, authorization: str | None = Header(default=None)):
        management.authorize(bearer(authorization), "backup")
        return backup(payload.destination)

    @app.post("/api/foundation/v1/management/restores")
    def restore_backup(payload: RestoreRequest, authorization: str | None = Header(default=None)):
        management.authorize(bearer(authorization), "restore")
        return restore(payload.manifest_path, payload.target_configuration)
