"""Same-process adapter that preserves the Foundation v1 HTTP boundary."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI
from starlette.testclient import TestClient

from .routes_v1 import OPERATIONS


class InProcessFoundationError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        status: int,
        message: str,
        retryable: bool,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.request_id = request_id


class InProcessFoundationClient:
    """Call a mounted service without bypassing transport or authorization."""

    def __init__(
        self,
        app: FastAPI,
        *,
        token: str | Callable[[], str] | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._http = TestClient(app)
        self._token = token
        self._request_id_factory = request_id_factory

    @property
    def operations(self) -> tuple[str, ...]:
        return tuple(OPERATIONS)

    def __enter__(self) -> "InProcessFoundationClient":
        self._http.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        self._http.__exit__(*args)

    def call(
        self,
        operation_id: str,
        *,
        path: Mapping[str, str | int] | None = None,
        query: Mapping[str, str | int | bool | None] | None = None,
        payload: Any = None,
    ) -> Any:
        try:
            method, template = OPERATIONS[operation_id]
        except KeyError as error:
            raise ValueError(f"Unknown Foundation operation: {operation_id}") from error
        required = tuple(part.split("}", 1)[0] for part in template.split("{")[1:])
        supplied = set((path or {}).keys())
        if supplied != set(required):
            raise ValueError(
                f"Operation {operation_id} needs path parameters {','.join(sorted(required))}"
            )
        route = template
        for name in required:
            value = str((path or {})[name])
            if not value:
                raise ValueError(f"Path parameter {name} cannot be empty")
            route = route.replace("{" + name + "}", quote(value, safe=""))
        headers = {"accept": "application/json"}
        token = self._token() if callable(self._token) else self._token
        if token:
            headers["authorization"] = f"Bearer {token}"
        if self._request_id_factory:
            headers["x-request-id"] = self._request_id_factory()
        response = self._http.request(
            method,
            "/api/foundation/v1" + route,
            params={key: value for key, value in (query or {}).items() if value is not None},
            json=payload if payload is not None else None,
            headers=headers,
        )
        body = response.json()
        if response.is_error:
            details = body.get("error", {}) if isinstance(body, dict) else {}
            raise InProcessFoundationError(
                code=str(details.get("code", "http_error")),
                status=response.status_code,
                message=str(details.get("message", f"HTTP {response.status_code}")),
                retryable=bool(details.get("retryable", response.status_code >= 500)),
                request_id=details.get("request_id") or response.headers.get("x-request-id"),
            )
        return body
