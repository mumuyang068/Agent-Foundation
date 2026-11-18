from __future__ import annotations

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ResourceRef, stable_fingerprint
from agent_foundation.modules.graph.contracts import GraphEdge, GraphNode, GraphOperation


class GraphMemoryLinkPort:
    """Turns a Memory link action into a normal Graph proposal and tracks its lifecycle."""

    def __init__(self, graph) -> None:
        self._graph = graph

    def propose_link(self, token, source, action, *, operation_id):
        if action.link_target is None or action.link_target.resource_type != "graph_node":
            raise FoundationError(
                "memory_dreaming_link_invalid", "Memory link target must be a Graph node"
            )
        evidence_refs = tuple(
            ref for ref in source.source_refs if ref.domain == "evidence"
        )
        if not evidence_refs or len(evidence_refs) != len(source.source_refs):
            raise FoundationError(
                "memory_dreaming_link_sources_invalid",
                "Memory link needs current Evidence source references",
            )
        node_id = f"memory:{source.memory_id}"
        node = GraphNode(
            node_id=node_id,
            scope=source.scope,
            node_type="memory",
            label=source.content[:1000],
            properties={"memory_id": source.memory_id, "memory_revision": source.revision},
            source_refs=evidence_refs,
        )
        edge_id = "memory-link:" + stable_fingerprint({
            "action_id": action.action_id,
            "source": source.ref().model_dump(mode="json"),
            "target": action.link_target.model_dump(mode="json"),
            "relation": action.link_relation,
        })[:32]
        edge = GraphEdge(
            edge_id=edge_id,
            scope=source.scope,
            relation_type=action.link_relation,
            from_node_id=node_id,
            to_node_id=action.link_target.resource_id,
            properties={"memory_action_id": action.action_id},
            source_refs=evidence_refs,
        )
        proposal = self._graph.propose(
            token,
            operations=(
                GraphOperation(action="put_node", node=node),
                GraphOperation(action="put_edge", edge=edge),
            ),
            evidence_refs=evidence_refs,
            operation_id=operation_id,
            schema_id=action.link_schema_id,
            schema_version=action.link_schema_version,
        )
        current = self._graph.get_proposal(token, proposal.proposal_id)
        return (
            "applied" if current.status == "applied" else "graph_pending",
            ResourceRef(
                domain="graph",
                scope=current.scope,
                resource_type="graph_proposal",
                resource_id=current.proposal_id,
                revision=str(current.revision),
            ),
        )
