import assert from "node:assert/strict";
import test from "node:test";

import {
  FOUNDATION_OPERATIONS,
  FoundationClient,
  FoundationClientError,
} from "../dist/index.js";

const jsonResponse = (payload, init = {}) =>
  new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json", "x-request-id": "request-1" },
    ...init,
  });

const pathValues = (template) => Object.fromEntries(
  [...template.matchAll(/\{([^}]+)\}/g)].map((match) => [match[1], `value/${match[1]}`]),
);

test("every generated public operation uses the exact v1 route and method", async () => {
  const calls = [];
  const client = new FoundationClient({
    origin: "http://127.0.0.1:8041",
    token: async () => "consumer-token",
    fetch: async (url, init) => {
      calls.push({ url, init });
      return jsonResponse({ ok: true });
    },
  });

  assert.equal(client.operations.length, 130);
  for (const [operationId, operation] of Object.entries(FOUNDATION_OPERATIONS)) {
    await client.call(operationId, {
      path: pathValues(operation.path),
      query: operation.method === "GET" ? { marker: true } : undefined,
      payload: operation.method === "GET" ? undefined : { operation_id: operationId },
    });
  }

  assert.equal(calls.length, 130);
  for (const [index, operation] of Object.values(FOUNDATION_OPERATIONS).entries()) {
    const call = calls[index];
    assert.equal(call.init.method, operation.method);
    assert.equal(call.init.headers.authorization, "Bearer consumer-token");
    assert.match(call.url, /^http:\/\/127\.0\.0\.1:8041\/api\/foundation\/v1\//);
    assert.ok(!call.url.includes("{"));
    if (operation.path.includes("{")) assert.ok(call.url.includes("value%2F"));
  }
});

test("exposes typed Foundation errors without fallback", async () => {
  const calls = [];
  const client = new FoundationClient({
    origin: "http://127.0.0.1:8041",
    fetch: async (url) => {
      calls.push(url);
      return jsonResponse(
        { error: { code: "grant_inactive", message: "Grant is unavailable", retryable: false } },
        { status: 403 },
      );
    },
  });
  await assert.rejects(
    () => client.memory.recall({ query: "bounded" }),
    (error) => error instanceof FoundationClientError
      && error.code === "grant_inactive"
      && error.status === 403
      && error.requestId === "request-1"
      && !error.retryable,
  );
  assert.equal(calls.length, 1);
  assert.equal(calls[0], "http://127.0.0.1:8041/api/foundation/v1/memory/recall");
});

test("validates exact path parameters and exposes all domain catalogs", () => {
  const client = new FoundationClient({ origin: "https://foundation.example" });
  assert.throws(
    () => client.call("evidence.get_revision", { path: {} }),
    /needs path parameters revision_id/,
  );
  assert.throws(
    () => client.call("evidence.get_revision", { path: { revision_id: "r", extra: "x" } }),
    /needs path parameters revision_id/,
  );
  assert.throws(() => client.call("legacy.runtime", {}), /Unknown Foundation operation/);
  const count = [client.access, client.memory, client.evidence, client.graph, client.context, client.jobs, client.applications, client.management]
    .reduce((sum, endpoint) => sum + endpoint.operations.length, 0);
  assert.equal(count, 130);
});
