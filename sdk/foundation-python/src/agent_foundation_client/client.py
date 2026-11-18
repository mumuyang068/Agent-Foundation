from __future__ import annotations

import re
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from .routes import OPERATIONS


class HttpClient(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...
    def close(self) -> None: ...


class FoundationClientError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int, retryable: bool, request_id: str | None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.request_id = request_id


class DomainEndpoint:
    def __init__(self, client: "FoundationClient", domain: str) -> None:
        self._client = client
        self.domain = domain

    @property
    def operations(self) -> tuple[str, ...]:
        prefix = self.domain + "."
        return tuple(key.removeprefix(prefix) for key in OPERATIONS if key.startswith(prefix))

    def call(self, operation: str, *, path: dict[str, Any] | None = None, query: dict[str, Any] | None = None, payload: dict[str, Any] | None = None):
        return self._client.call(f"{self.domain}.{operation}", path=path, query=query, payload=payload)


class AccessEndpoint(DomainEndpoint):
    def put_grant(self, payload): return self.call("put_grant", payload=payload)
    def list_grants(self): return self.call("list_grants")
    def get_grant(self, grant_id): return self.call("get_grant", path={"grant_id": grant_id})
    def issue_session(self, payload): return self.call("issue_session", payload=payload)
    def get_session(self, session_id): return self.call("get_session", path={"session_id": session_id})
    def revoke_session(self, session_id): return self.call("revoke_session", path={"session_id": session_id})
    def put_delegation(self, payload): return self.call("put_delegation", payload=payload)
    def get_delegation(self, delegation_id): return self.call("get_delegation", path={"delegation_id": delegation_id})
    def revoke_delegation(self, delegation_id): return self.call("revoke_delegation", path={"delegation_id": delegation_id})
    def audit(self): return self.call("audit")
    def authorize(self, payload): return self.call("authorize", payload=payload)
    def revoke_grant(self, grant_id): return self.call("revoke_grant", path={"grant_id": grant_id})


class MemoryEndpoint(DomainEndpoint):
    def propose(self, payload): return self.call("propose", payload=payload)
    def get(self, memory_id): return self.call("get", path={"memory_id": memory_id})
    def review(self, memory_id, payload): return self.call("review", path={"memory_id": memory_id}, payload=payload)
    def correct(self, memory_id, payload): return self.call("correct", path={"memory_id": memory_id}, payload=payload)
    def feedback(self, memory_id, payload): return self.call("feedback", path={"memory_id": memory_id}, payload=payload)
    def recall(self, payload): return self.call("recall", payload=payload)


class EvidenceEndpoint(DomainEndpoint):
    def admit(self, payload): return self.call("admit", payload=payload)
    def get(self, revision_id): return self.call("get_revision", path={"revision_id": revision_id})
    def search(self, payload): return self.call("search", payload=payload)
    def revoke(self, revision_id, operation_id): return self.call("revoke", path={"revision_id": revision_id}, query={"operation_id": operation_id})


class GraphEndpoint(DomainEndpoint):
    def propose(self, payload): return self.call("propose", payload=payload)
    def review(self, proposal_id, payload): return self.call("review", path={"proposal_id": proposal_id}, payload=payload)
    def apply(self, proposal_id, payload): return self.call("apply", path={"proposal_id": proposal_id}, payload=payload)
    def query(self, payload): return self.call("query", payload=payload)
    def query_temporal(self, payload): return self.call("query_temporal", payload=payload)
    def propose_temporal_relation(self, payload): return self.call("propose_temporal_relation", payload=payload)
    def propose_temporal_invalidation(self, edge_id, payload): return self.call("propose_temporal_invalidation", path={"edge_id": edge_id}, payload=payload)
    def propose_temporal_supersession(self, edge_id, payload): return self.call("propose_temporal_supersession", path={"edge_id": edge_id}, payload=payload)
    def get_node(self, node_id): return self.call("get_node", path={"node_id": node_id})
    def get_edge_revision(self, edge_id, revision): return self.call("get_edge_revision", path={"edge_id": edge_id, "revision": revision})
    def get_schema(self, schema_id, version): return self.call("get_schema", path={"schema_id": schema_id, "version": version})
    def list_schemas(self, *, schema_id=None, limit=8): return self.call("list_schemas", query={"schema_id": schema_id, "limit": limit})


class ContextEndpoint(DomainEndpoint):
    def assemble(self, payload): return self.call("assemble", payload=payload)
    def snapshot(self, payload): return self.call("snapshot", payload=payload)
    def replay(self, snapshot_id): return self.call("replay", path={"snapshot_id": snapshot_id})


class JobsEndpoint(DomainEndpoint):
    def plan(self, payload): return self.call("plan", payload=payload)
    def authorize(self, job_id, payload): return self.call("authorize", path={"job_id": job_id}, payload=payload)
    def run_next(self, job_id, payload): return self.call("run_next", path={"job_id": job_id}, payload=payload)
    def get(self, job_id): return self.call("get", path={"job_id": job_id})
    def cancel(self, job_id, payload): return self.call("cancel", path={"job_id": job_id}, payload=payload)


class ApplicationsEndpoint(DomainEndpoint):
    def prepare(self, payload): return self.call("prepare", payload=payload)
    def get(self, turn_id): return self.call("get", path={"turn_id": turn_id})
    def observe(self, turn_id, payload): return self.call("observe", path={"turn_id": turn_id}, payload=payload)
    def outcome(self, turn_id, payload): return self.call("outcome", path={"turn_id": turn_id}, payload=payload)


class ManagementEndpoint(DomainEndpoint):
    def diagnostics(self): return self.call("diagnostics")
    def stage_release(self, payload): return self.call("stage_release", payload=payload)
    def approve_release(self, release_id, payload): return self.call("approve_release", path={"release_id": release_id}, payload=payload)
    def activate_release(self, release_id, payload): return self.call("activate_release", path={"release_id": release_id}, payload=payload)
    def active_release(self, *, kind, artifact_id): return self.call("active_release", query={"kind": kind, "artifact_id": artifact_id})
    def list_releases(self, *, kind=None, status=None, limit=20): return self.call("list_releases", query={"kind": kind, "status": status, "limit": limit})
    def list_indexes(self, *, domain=None, status=None, limit=20): return self.call("list_indexes", query={"domain": domain, "status": status, "limit": limit})
    def get_evaluation(self, evaluation_id): return self.call("get_evaluation", path={"evaluation_id": evaluation_id})
    def list_evaluations(self, *, subject_ref=None, limit=20): return self.call("list_evaluations", query={"subject_ref": subject_ref, "limit": limit})
    def list_transfers(self, *, kind=None, status=None, limit=20): return self.call("list_transfers", query={"kind": kind, "status": status, "limit": limit})
    def certify_extractor(self, release_id, payload): return self.call("certify_extractor", path={"release_id": release_id}, payload=payload)
    def get_extractor_certificate(self, certificate_id): return self.call("get_extractor_certificate", path={"certificate_id": certificate_id})
    def revoke_extractor_certificate(self, certificate_id, payload): return self.call("revoke_extractor_certificate", path={"certificate_id": certificate_id}, payload=payload)
    def execute_extractor(self, release_id, payload): return self.call("execute_extractor", path={"release_id": release_id}, payload=payload)
    def get_extractor_execution(self, event_id): return self.call("get_extractor_execution", path={"event_id": event_id})


ENDPOINT_TYPES = {
    "access": AccessEndpoint, "memory": MemoryEndpoint, "evidence": EvidenceEndpoint,
    "graph": GraphEndpoint, "context": ContextEndpoint, "jobs": JobsEndpoint,
    "applications": ApplicationsEndpoint, "management": ManagementEndpoint,
}


class FoundationClient:
    prefix = "/api/foundation/v1"

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 20.0, trust_env: bool = False, http_client: HttpClient | None = None) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._token = token
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, trust_env=trust_env)
        for domain, endpoint_type in ENDPOINT_TYPES.items():
            setattr(self, domain, endpoint_type(self, domain))

    @property
    def operations(self) -> tuple[str, ...]:
        return tuple(OPERATIONS)

    def call(self, operation_id: str, *, path: dict[str, Any] | None = None, query: dict[str, Any] | None = None, payload: dict[str, Any] | None = None):
        try:
            method, template = OPERATIONS[operation_id]
        except KeyError as error:
            raise ValueError(f"Unknown Foundation operation: {operation_id}") from error
        parameters = path or {}
        required = set(re.findall(r"\{([^}]+)\}", template))
        if set(parameters) != required:
            raise ValueError(f"Operation {operation_id} needs path parameters {sorted(required)}")
        route = template
        for name, value in parameters.items():
            if value is None or str(value) == "":
                raise ValueError(f"Path parameter {name} cannot be empty")
            route = route.replace("{" + name + "}", quote(str(value), safe=""))
        kwargs: dict[str, Any] = {}
        if query:
            kwargs["params"] = query
        if payload is not None:
            kwargs["json"] = payload
        return self.request(method, route, **kwargs)

    def request(self, method: str, path: str, **kwargs: Any):
        if self._token:
            request_headers = dict(kwargs.pop("headers", {}) or {})
            request_headers.setdefault("Authorization", f"Bearer {self._token}")
            kwargs["headers"] = request_headers
        response = self._http.request(method, f"{self.prefix}{path}", **kwargs)
        request_id = response.headers.get("x-request-id")
        try:
            payload = response.json()
        except ValueError as error:
            raise FoundationClientError("invalid_response", "Foundation returned a non-JSON response", status_code=response.status_code, retryable=response.status_code >= 500, request_id=request_id) from error
        if response.status_code >= 400:
            detail = payload.get("error", payload.get("detail", {})) if isinstance(payload, dict) else {}
            raise FoundationClientError(str(detail.get("code", "http_error")), str(detail.get("message", f"Foundation request failed with HTTP {response.status_code}")), status_code=response.status_code, retryable=bool(detail.get("retryable", response.status_code >= 500)), request_id=request_id)
        return payload

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "FoundationClient": return self
    def __exit__(self, *_: object) -> None: self.close()
