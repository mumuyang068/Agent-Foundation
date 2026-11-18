from __future__ import annotations

import re

import httpx
import pytest

from agent_foundation_client import FoundationClient, FoundationClientError, OPERATIONS


def test_every_generated_operation_is_callable_without_service_imports() -> None:
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"x-request-id": "request-1"})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url="https://foundation.example", transport=transport)
    client = FoundationClient("https://foundation.example", token="consumer-token", http_client=http)
    for operation_id, (method, template) in OPERATIONS.items():
        names = re.findall(r"\{([^}]+)\}", template)
        result = client.call(
            operation_id,
            path={name: f"value/{name}" for name in names},
            query={"limit": 1} if method == "GET" else None,
            payload={"operation_id": "test"} if method in {"POST", "PUT", "PATCH"} else None,
        )
        assert result == {"ok": True}
    assert len(calls) == len(OPERATIONS) == 130
    assert all(request.headers["authorization"] == "Bearer consumer-token" for request in calls)
    assert all("{" not in str(request.url) and "/api/foundation/v1/" in str(request.url) for request in calls)
    assert all(request.method == method for request, (method, _path) in zip(calls, OPERATIONS.values()))
    client.close()
    http.close()


def test_typed_error_and_path_contract_do_not_fallback() -> None:
    def handler(_request: httpx.Request):
        return httpx.Response(
            403,
            json={"error": {"code": "grant_inactive", "message": "Grant unavailable", "retryable": False}},
            headers={"x-request-id": "request-2"},
        )

    http = httpx.Client(base_url="https://foundation.example", transport=httpx.MockTransport(handler))
    client = FoundationClient("https://foundation.example", http_client=http)
    with pytest.raises(FoundationClientError) as denied:
        client.memory.recall({"query": "x"})
    assert denied.value.code == "grant_inactive"
    assert denied.value.status_code == 403
    assert denied.value.request_id == "request-2"
    assert not denied.value.retryable
    with pytest.raises(ValueError, match="needs path parameters"):
        client.call("graph.get_node")
    with pytest.raises(ValueError, match="Unknown Foundation operation"):
        client.call("memory.old-registration-fallback")
    http.close()


def test_domain_endpoints_expose_exact_generated_operations() -> None:
    client = FoundationClient(
        "https://foundation.example",
        http_client=httpx.Client(base_url="https://foundation.example", transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={})
        )),
    )
    for domain in ("access", "memory", "evidence", "graph", "context", "jobs", "applications", "management"):
        endpoint = getattr(client, domain)
        assert {f"{domain}.{name}" for name in endpoint.operations} == {
            operation for operation in OPERATIONS if operation.startswith(domain + ".")
        }
    client.close()
