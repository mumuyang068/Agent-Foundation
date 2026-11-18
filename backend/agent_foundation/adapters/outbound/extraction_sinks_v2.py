from __future__ import annotations

import json
from typing import Any

from agent_foundation.execution.extractors import ExtractorSpec
from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef
from agent_foundation.modules.graph.contracts import GraphNode, GraphOperation


class DomainExtractionProposalSink:
    """Routes trusted extractor output through public domain proposal commands."""

    def __init__(self, *, memory=None, graph=None) -> None:
        self._memory = memory
        self._graph = graph

    def propose(
        self,
        token: str,
        extractor: ExtractorSpec,
        *,
        event_id: str,
        value: Any,
        source_refs: tuple[ResourceRef, ...],
    ) -> ResourceRef:
        target = extractor.configuration.get("target_domain")
        if target == "memory":
            if self._memory is None:
                raise FoundationError(
                    "extractor_target_unavailable", "Memory is not enabled on this host"
                )
            content = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            entry = self._memory.propose(
                token,
                content=content,
                source_refs=source_refs,
                operation_id=f"{event_id}:memory-proposal",
            )
            return entry.ref()
        if target == "graph":
            if self._graph is None:
                raise FoundationError(
                    "extractor_target_unavailable", "Graph is not enabled on this host"
                )
            if not isinstance(value, dict):
                raise FoundationError(
                    "extractor_candidate_invalid", "Graph extractor output must be an object"
                )
            try:
                node = GraphNode(
                    node_id=value["node_id"],
                    scope=extractor.scope,
                    node_type=value["node_type"],
                    label=value["label"],
                    properties=value.get("properties", {}),
                    source_refs=source_refs,
                )
            except (KeyError, TypeError, ValueError) as error:
                raise FoundationError(
                    "extractor_candidate_invalid", "Graph extractor output is invalid"
                ) from error
            proposal = self._graph.propose(
                token,
                operations=(GraphOperation(action="put_node", node=node),),
                evidence_refs=source_refs,
                operation_id=f"{event_id}:graph-proposal",
                schema_id=extractor.schema_id,
                schema_version=extractor.schema_version,
            )
            return ResourceRef(
                domain="graph",
                scope=proposal.scope,
                resource_type="graph_proposal",
                resource_id=proposal.proposal_id,
                revision=str(proposal.revision),
            )
        raise FoundationError(
            "extractor_target_invalid", "Extractor target_domain must be memory or graph"
        )
