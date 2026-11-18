"""Exercise the installed Python SDK over HTTP with synthetic public demo data."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from agent_foundation_client import FoundationClient, FoundationClientError


def run(base_url: str, bootstrap: str) -> dict:
    run_id = uuid4().hex[:12]
    scope = {"tenant_id": "portfolio", "space_id": f"graph-{run_id}"}
    clients = {}
    with FoundationClient(base_url, token=bootstrap) as admin:
        for name, domains, actions in (
            ("analyst", ["evidence", "graph"], ["admit", "read", "search", "propose", "revoke"]),
            ("reviewer", ["graph"], ["read", "review", "apply"]),
        ):
            principal = f"{name}-{run_id}"
            grant = admin.access.put_grant({
                "grant_id": principal, "principal_id": principal, "scope": scope,
                "domains": domains, "actions": actions, "limits": {"max_items": 20},
            })
            session = admin.access.issue_session({
                "principal_id": principal, "principal_kind": "user",
                "grant_id": grant["grant_id"], "ttl_seconds": 900,
            })
            clients[name] = FoundationClient(base_url, token=session["access_token"])
    analyst, reviewer = clients["analyst"], clients["reviewer"]
    try:
        source = analyst.evidence.admit({
            "operation_id": "admit-source", "source_id": "demo-risk-note",
            "content": "风险说明😀\nAlpha depends on Beta.",
            "source_range": {"start": 0, "end": 5},
        })
        ref = {"domain": "evidence", "scope": scope, "resource_type": "evidence_revision",
               "resource_id": source["revision_id"], "revision": source["revision_id"]}
        operations = [
            {"action": "put_node", "node": {"node_id": name.lower(), "scope": scope,
             "node_type": "component", "label": name, "source_refs": [ref]}}
            for name in ("Alpha", "Beta")
        ]
        operations.append({"action": "put_edge", "edge": {
            "edge_id": "alpha-depends-on-beta", "scope": scope, "relation_type": "depends_on",
            "from_node_id": "alpha", "to_node_id": "beta", "source_refs": [ref],
        }})
        proposal = analyst.graph.propose({"operation_id": "propose-graph", "operations": operations,
                                          "evidence_refs": [ref]})
        # This actor has no review permission; the independent reviewer continues.
        try:
            analyst.graph.review(proposal["proposal_id"], {
                "operation_id": "review-denied", "expected_revision": proposal["revision"], "approve": True,
            })
        except FoundationClientError as error:
            denied = {"status": error.status_code, "code": error.code}
        else:
            raise AssertionError("Analyst unexpectedly reviewed a proposal")
        reviewed = reviewer.graph.review(proposal["proposal_id"], {
            "operation_id": "review-graph", "expected_revision": proposal["revision"], "approve": True,
        })
        command = {"operation_id": "apply-graph", "expected_revision": reviewed["revision"]}
        applied = reviewer.graph.apply(proposal["proposal_id"], command)
        replayed = reviewer.graph.apply(proposal["proposal_id"], command)
        before = analyst.graph.query({"query": "Alpha", "limit": 8})
        assert applied == replayed, "Same operation must return its original receipt"
        assert len(before["hits"]) == 1, before
        analyst.evidence.revoke(source["revision_id"], "revoke-source")
        after = analyst.graph.query({"query": "Alpha", "limit": 8})
        assert len(after["hits"]) == 0, after
        return {
            "run_id": run_id, "recorded_at": datetime.now(timezone.utc).isoformat(),
            "channel": "installed Python client over HTTP", "data": "synthetic",
            "scope": scope, "proposed_by": proposal["proposed_by"],
            "reviewed_by": reviewed["reviewed_by"], "review_denied": denied,
            "source_revision": source["revision_id"], "source_range": source["source_range"],
            "proposal_state": applied["status"], "idempotent_replay_equal": True,
            "visible_before_revoke": len(before["hits"]), "visible_after_revoke": len(after["hits"]),
            "proposal": applied, "query_before": before, "query_after": after,
        }
    finally:
        for client in clients.values():
            client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18765")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Use a new receipt filename")
    result = run(args.base_url, os.environ["AGENT_FOUNDATION_BOOTSTRAP_TOKEN"])
    result["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with httpx.Client(trust_env=False) as client:
        openapi = client.get(args.base_url + "/openapi.json").raise_for_status().json()
    result["service_version"] = openapi["info"]["version"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for key in ("channel", "proposal_state", "review_denied", "idempotent_replay_equal",
                "visible_before_revoke", "visible_after_revoke"):
        print(f"{key}: {result[key]}")
    print(f"Receipt: {args.output}")


if __name__ == "__main__":
    main()
