from __future__ import annotations

from agent_foundation.kernel.errors import FoundationError

from .contracts import GraphOperation


def validate_temporal_operations(operations: tuple[GraphOperation, ...]) -> None:
    """Validate temporal lineage using only the bounded authority command."""

    edges = {
        operation.edge.edge_id: operation.edge
        for operation in operations
        if operation.action == "put_edge" and operation.edge is not None
    }
    for operation in operations:
        edge = operation.edge
        if operation.action != "put_edge" or edge is None or edge.temporal is None:
            continue
        temporal = edge.temporal
        if temporal.lifecycle in {"invalidated", "superseded"} and not temporal.audit_reason:
            raise FoundationError(
                "temporal_relation_invalid", "Temporal lifecycle change needs an audit reason"
            )
        if temporal.lifecycle != "superseded":
            continue
        successor_ref = temporal.successor_ref
        successor = None if successor_ref is None else edges.get(successor_ref.resource_id)
        if (
            successor is None
            or successor_ref != successor.ref()
            or successor.scope != edge.scope
            or successor.relation_type != edge.relation_type
            or successor.from_node_id != edge.from_node_id
            or successor.temporal is None
            or successor.temporal.predecessor_ref != edge.ref()
        ):
            raise FoundationError(
                "temporal_successor_invalid",
                "Temporal successor must be reciprocal and share the same Graph transaction",
            )
