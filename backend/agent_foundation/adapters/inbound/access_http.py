from __future__ import annotations

import hmac
from typing import Literal

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import DomainName, Principal
from agent_foundation.modules.access.contracts import AccessGrant, DelegationGrant
from agent_foundation.modules.access.ports import AccessService


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionRequest(RequestModel):
    principal_id: str = Field(min_length=1, max_length=240)
    principal_kind: Literal["user", "service", "agent", "operator"]
    grant_id: str = Field(min_length=1, max_length=240)
    delegation_id: str | None = Field(default=None, min_length=1, max_length=240)
    ttl_seconds: int = Field(default=900, ge=1, le=86400)


class AuthorizationRequest(RequestModel):
    domain: DomainName
    action: str = Field(min_length=1, max_length=120)
    policy_id: str = Field(min_length=1, max_length=160)
    policy_version: int = Field(ge=1)
    execution_profile_id: str | None = None
    execution_profile_version: int | None = Field(default=None, ge=1)


def http_status(error: FoundationError) -> int:
    if error.code in {
        "bootstrap_credential_invalid",
        "session_inactive",
        "grant_inactive",
        "grant_expired",
        "grant_principal_mismatch",
        "grant_scope_denied",
        "grant_resource_denied",
        "delegation_inactive",
        "delegation_grant_mismatch",
    }:
        return 403
    if error.code.endswith("_not_found"):
        return 404
    if error.code.endswith("_unavailable"):
        return 503
    if (
        error.code.endswith("_invalid")
        or error.code.endswith("_missing")
        or error.code.endswith("_required")
        or error.code.endswith("_exceeded")
    ):
        return 422
    return 409


def error_response(error: FoundationError, request_id: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=http_status(error),
        content={
            "error": {
                "code": error.code,
                "message": str(error),
                "retryable": error.retryable,
                "request_id": request_id,
                "details": error.details,
            }
        },
    )


def create_access_app(access: AccessService, *, bootstrap_token: str) -> FastAPI:
    app = FastAPI(title="Agent Foundation", version="1.0.0a32")

    def require_bootstrap(authorization: str | None) -> None:
        expected = f"Bearer {bootstrap_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise FoundationError("bootstrap_credential_invalid", "Access administration requires bootstrap authority")

    @app.exception_handler(FoundationError)
    async def foundation_error_handler(request: Request, error: FoundationError):
        return error_response(error, request.headers.get("x-request-id"))

    @app.post("/api/foundation/v1/access/grants")
    def put_grant(grant: AccessGrant, authorization: str | None = Header(default=None)):
        require_bootstrap(authorization)
        return access.put_grant(grant)

    @app.get("/api/foundation/v1/access/grants")
    def list_grants(authorization: str | None = Header(default=None)):
        require_bootstrap(authorization)
        return {
            "items": [
                {"grant": grant, "revoked": revoked}
                for grant, revoked in access.list_grants()
            ]
        }

    @app.get("/api/foundation/v1/access/grants/{grant_id}")
    def get_grant(grant_id: str, authorization: str | None = Header(default=None)):
        require_bootstrap(authorization)
        grant, revoked = access.get_grant(grant_id)
        return {"grant": grant, "revoked": revoked}

    @app.post("/api/foundation/v1/access/grants/{grant_id}/revoke")
    def revoke_grant(grant_id: str, authorization: str | None = Header(default=None)):
        require_bootstrap(authorization)
        access.revoke_grant(grant_id)
        return {"grant_id": grant_id, "status": "revoked"}

    @app.post("/api/foundation/v1/access/delegations")
    def put_delegation(
        delegation: DelegationGrant,
        authorization: str | None = Header(default=None),
    ):
        require_bootstrap(authorization)
        return access.put_delegation(delegation)

    @app.get("/api/foundation/v1/access/delegations/{delegation_id}")
    def get_delegation(
        delegation_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_bootstrap(authorization)
        return access.get_delegation(delegation_id)

    @app.post("/api/foundation/v1/access/delegations/{delegation_id}/revoke")
    def revoke_delegation(
        delegation_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_bootstrap(authorization)
        return access.revoke_delegation(delegation_id)

    @app.post("/api/foundation/v1/access/sessions")
    def issue_session(payload: SessionRequest, authorization: str | None = Header(default=None)):
        require_bootstrap(authorization)
        return access.issue_session(
            Principal(
                principal_id=payload.principal_id,
                kind=payload.principal_kind,
                delegation_id=payload.delegation_id,
            ),
            payload.grant_id,
            ttl_seconds=payload.ttl_seconds,
        )

    @app.get("/api/foundation/v1/access/sessions/{session_id}")
    def get_session(session_id: str, authorization: str | None = Header(default=None)):
        require_bootstrap(authorization)
        return access.get_session(session_id)

    @app.post("/api/foundation/v1/access/sessions/{session_id}/revoke")
    def revoke_session(session_id: str, authorization: str | None = Header(default=None)):
        require_bootstrap(authorization)
        return access.revoke_session(session_id)

    @app.get("/api/foundation/v1/access/audit")
    def audit(authorization: str | None = Header(default=None)):
        require_bootstrap(authorization)
        return {"items": access.audit_events()}

    @app.post("/api/foundation/v1/access/authorize")
    def authorize(payload: AuthorizationRequest, authorization: str | None = Header(default=None)):
        if authorization is None or not authorization.startswith("Bearer "):
            raise FoundationError("session_inactive", "A Bearer credential is required")
        operation, grant, trusted = access.authorize_token(
            authorization.removeprefix("Bearer "),
            domain=payload.domain,
            action=payload.action,
            policy_id=payload.policy_id,
            policy_version=payload.policy_version,
            execution_profile_id=payload.execution_profile_id,
            execution_profile_version=payload.execution_profile_version,
        )
        return {
            "operation_context": operation,
            "grant_fingerprint": grant.fingerprint,
            "trusted_context": trusted,
        }

    return app
