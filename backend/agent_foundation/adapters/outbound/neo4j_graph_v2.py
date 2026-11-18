from __future__ import annotations

from datetime import datetime, timezone
import json

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ScopeRef
from agent_foundation.modules.graph.contracts import (
    GraphEdge,
    GraphNode,
    GraphOperation,
    GraphOperationReceipt,
)
from agent_foundation.modules.graph.domain import validate_temporal_operations


def scope_key(scope: ScopeRef) -> str:
    return json.dumps(scope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class Neo4jGraphWriteAuthority:
    """Neo4j graph authority with graph mutations and write receipts in one transaction."""

    def __init__(self, driver, *, database: str = "neo4j") -> None:
        self._driver = driver
        self._database = database
        self._driver.verify_connectivity()
        with self._driver.session(database=self._database) as session:
            for statement in (
                "CREATE CONSTRAINT af_graph_node_revision_unique IF NOT EXISTS "
                "FOR (r:AFGraphNodeRevision) REQUIRE "
                "(r.scope_key, r.resource_id, r.revision) IS UNIQUE",
                "CREATE CONSTRAINT af_graph_edge_revision_unique IF NOT EXISTS "
                "FOR (r:AFGraphEdgeRevision) REQUIRE "
                "(r.scope_key, r.resource_id, r.revision) IS UNIQUE",
                "CREATE CONSTRAINT af_graph_write_receipt_unique IF NOT EXISTS "
                "FOR (r:AFGraphWriteReceipt) REQUIRE "
                "(r.scope_key, r.operation_id) IS UNIQUE",
                "CREATE CONSTRAINT af_graph_authority_scope_unique IF NOT EXISTS "
                "FOR (s:AFGraphAuthorityScope) REQUIRE s.scope_key IS UNIQUE",
            ):
                session.run(statement).consume()

    def apply_operations(
        self,
        scope: ScopeRef,
        operations: tuple[GraphOperation, ...],
        *,
        operation_id: str,
        command_digest: str,
    ) -> GraphOperationReceipt:
        if any(
            resource is not None and resource.scope != scope
            for operation in operations
            for resource in (operation.node, operation.edge)
        ):
            raise FoundationError(
                "graph_operation_scope_invalid", "Graph operation is outside authority scope"
            )
        validate_temporal_operations(operations)
        try:
            with self._driver.session(database=self._database) as session:
                return session.execute_write(
                    self._apply_transaction,
                    scope,
                    operations,
                    operation_id,
                    command_digest,
                )
        except FoundationError:
            raise
        except Exception as error:
            error_name = type(error).__name__
            if error_name in {"ClientError", "CypherSyntaxError", "ConstraintError"}:
                raise FoundationError(
                    "graph_authority_contract_invalid",
                    "Neo4j rejected the Graph authority command",
                    retryable=False,
                ) from error
            raise FoundationError(
                "graph_authority_unavailable",
                "Neo4j Graph authority could not commit the operation",
                retryable=True,
            ) from error

    def get_operation_receipt(
        self, scope: ScopeRef, operation_id: str
    ) -> GraphOperationReceipt | None:
        with self._driver.session(database=self._database) as session:
            row = session.run(
                "MATCH (r:AFGraphWriteReceipt {scope_key:$scope_key, operation_id:$operation_id}) "
                "RETURN r.payload AS payload",
                scope_key=scope_key(scope),
                operation_id=operation_id,
            ).single()
        return None if row is None else GraphOperationReceipt.model_validate_json(row["payload"])

    def get_node(self, scope: ScopeRef, node_id: str) -> GraphNode | None:
        return self._latest("AFGraphNodeRevision", scope, node_id, GraphNode)

    def list_nodes(self, scope: ScopeRef, *, limit: int) -> tuple[GraphNode, ...]:
        return self._list_current("AFGraphNodeRevision", scope, GraphNode, limit)

    def candidates(self, scope: ScopeRef, *, limit: int) -> tuple[GraphNode, ...]:
        return self.list_nodes(scope, limit=limit)

    def get_node_revision(
        self, scope: ScopeRef, node_id: str, revision: int
    ) -> GraphNode | None:
        return self._exact("AFGraphNodeRevision", scope, node_id, revision, GraphNode)

    def get_edge(self, scope: ScopeRef, edge_id: str) -> GraphEdge | None:
        return self._latest("AFGraphEdgeRevision", scope, edge_id, GraphEdge)

    def list_edges(self, scope: ScopeRef, *, limit: int) -> tuple[GraphEdge, ...]:
        return self._list_current("AFGraphEdgeRevision", scope, GraphEdge, limit)

    def get_edge_revision(
        self, scope: ScopeRef, edge_id: str, revision: int
    ) -> GraphEdge | None:
        return self._exact("AFGraphEdgeRevision", scope, edge_id, revision, GraphEdge)

    def incident_edges(self, scope: ScopeRef, node_id: str) -> tuple[GraphEdge, ...]:
        return tuple(
            edge
            for edge in self._list_current("AFGraphEdgeRevision", scope, GraphEdge, None)
            if edge.status == "active"
            and node_id in {edge.from_node_id, edge.to_node_id}
        )

    def authority_watermark(self, scope: ScopeRef) -> int:
        with self._driver.session(database=self._database) as session:
            row = session.run(
                "MATCH (s:AFGraphAuthorityScope {scope_key:$scope_key}) "
                "RETURN s.authority_watermark AS watermark",
                scope_key=scope_key(scope),
            ).single()
        return 0 if row is None else int(row["watermark"])

    def close(self) -> None:
        self._driver.close()

    @staticmethod
    def _apply_transaction(
        tx,
        scope: ScopeRef,
        operations: tuple[GraphOperation, ...],
        operation_id: str,
        command_digest: str,
    ) -> GraphOperationReceipt:
        key = scope_key(scope)
        tx.run(
            "MERGE (s:AFGraphAuthorityScope {scope_key:$scope_key}) "
            "ON CREATE SET s.authority_watermark=0, s.lock_sequence=0 "
            "SET s.lock_sequence=coalesce(s.lock_sequence, 0)+1",
            scope_key=key,
        ).consume()
        existing = tx.run(
            "MATCH (r:AFGraphWriteReceipt {scope_key:$scope_key, operation_id:$operation_id}) "
            "RETURN r.payload AS payload",
            scope_key=key,
            operation_id=operation_id,
        ).single()
        if existing is not None:
            receipt = GraphOperationReceipt.model_validate_json(existing["payload"])
            if receipt.command_digest != command_digest:
                raise FoundationError(
                    "operation_id_conflict", "Operation ID was used for another command"
                )
            return receipt

        changed: list[str] = []
        for operation in operations:
            if operation.action == "put_node":
                assert operation.node is not None
                Neo4jGraphWriteAuthority._put_node(
                    tx, operation.node, operation.expected_revision
                )
                changed.append(operation.node.node_id)
            elif operation.action == "put_edge":
                assert operation.edge is not None
                if not Neo4jGraphWriteAuthority._active_node(
                    tx, scope, operation.edge.from_node_id
                ) or not Neo4jGraphWriteAuthority._active_node(
                    tx, scope, operation.edge.to_node_id
                ):
                    raise FoundationError(
                        "graph_edge_endpoint_missing", "Graph edge endpoints must be active"
                    )
                Neo4jGraphWriteAuthority._put_edge(
                    tx, operation.edge, operation.expected_revision
                )
                changed.append(operation.edge.edge_id)
            elif operation.action == "delete_edge":
                target_id = str(operation.target_id)
                Neo4jGraphWriteAuthority._delete_edge(
                    tx, scope, target_id, operation.expected_revision
                )
                changed.append(target_id)
            else:
                changed.extend(
                    Neo4jGraphWriteAuthority._delete_node(
                        tx, scope, str(operation.target_id), operation.expected_revision
                    )
                )

        row = tx.run(
            "MATCH (s:AFGraphAuthorityScope {scope_key:$scope_key}) "
            "SET s.authority_watermark=s.authority_watermark+1 "
            "RETURN s.authority_watermark AS watermark",
            scope_key=key,
        ).single()
        committed_at = datetime.now(timezone.utc)
        receipt = GraphOperationReceipt(
            operation_id=operation_id,
            command_digest=command_digest,
            action="write",
            resource_ids=tuple(changed),
            committed_at=committed_at,
            authority_watermark=int(row["watermark"]),
        )
        tx.run(
            "CREATE (:AFGraphWriteReceipt {scope_key:$scope_key, operation_id:$operation_id, "
            "command_digest:$command_digest, committed_at:$committed_at, payload:$payload})",
            scope_key=key,
            operation_id=operation_id,
            command_digest=command_digest,
            committed_at=committed_at.isoformat(),
            payload=receipt.model_dump_json(),
        ).consume()
        return receipt

    @staticmethod
    def _put_node(tx, requested: GraphNode, expected_revision: int | None) -> None:
        current = Neo4jGraphWriteAuthority._latest_tx(
            tx, "AFGraphNodeRevision", requested.scope, requested.node_id, GraphNode
        )
        if current is None:
            if expected_revision is not None:
                raise FoundationError("graph_version_conflict", "Graph node does not exist")
            stored = requested.model_copy(update={"revision": 1, "status": "active"})
        else:
            if current.status != "active" or expected_revision != current.revision:
                raise FoundationError("graph_version_conflict", "Graph node revision changed")
            stored = requested.model_copy(update={
                "revision": current.revision + 1,
                "status": "active",
                "created_at": current.created_at,
                "updated_at": datetime.now(timezone.utc),
            })
        Neo4jGraphWriteAuthority._insert_tx(
            tx, "AFGraphNodeRevision", stored.scope, stored.node_id, stored.revision,
            stored.status, stored.model_dump_json()
        )

    @staticmethod
    def _put_edge(tx, requested: GraphEdge, expected_revision: int | None) -> None:
        current = Neo4jGraphWriteAuthority._latest_tx(
            tx, "AFGraphEdgeRevision", requested.scope, requested.edge_id, GraphEdge
        )
        if current is None:
            if expected_revision is not None:
                raise FoundationError("graph_version_conflict", "Graph edge does not exist")
            stored = requested.model_copy(update={"revision": 1, "status": "active"})
        else:
            if current.status != "active" or expected_revision != current.revision:
                raise FoundationError("graph_version_conflict", "Graph edge revision changed")
            stored = requested.model_copy(update={
                "revision": current.revision + 1,
                "status": "active",
                "created_at": current.created_at,
                "updated_at": datetime.now(timezone.utc),
            })
        Neo4jGraphWriteAuthority._validate_temporal_cardinality(tx, stored)
        Neo4jGraphWriteAuthority._insert_tx(
            tx, "AFGraphEdgeRevision", stored.scope, stored.edge_id, stored.revision,
            stored.status, stored.model_dump_json()
        )

    @staticmethod
    def _delete_node(
        tx, scope: ScopeRef, node_id: str, expected_revision: int | None
    ) -> tuple[str, ...]:
        current = Neo4jGraphWriteAuthority._latest_tx(
            tx, "AFGraphNodeRevision", scope, node_id, GraphNode
        )
        if current is None or current.status != "active" or current.revision != expected_revision:
            raise FoundationError("graph_version_conflict", "Graph node revision changed")
        incident = tuple(
            edge
            for edge in Neo4jGraphWriteAuthority._list_current_tx(
                tx, "AFGraphEdgeRevision", scope, GraphEdge, None
            )
            if edge.status == "active" and node_id in {edge.from_node_id, edge.to_node_id}
        )
        for edge in incident:
            Neo4jGraphWriteAuthority._delete_edge(tx, scope, edge.edge_id, edge.revision)
        deleted = current.model_copy(update={
            "revision": current.revision + 1,
            "status": "deleted",
            "updated_at": datetime.now(timezone.utc),
        })
        Neo4jGraphWriteAuthority._insert_tx(
            tx, "AFGraphNodeRevision", scope, node_id, deleted.revision,
            deleted.status, deleted.model_dump_json()
        )
        return (node_id, *(edge.edge_id for edge in incident))

    @staticmethod
    def _delete_edge(tx, scope: ScopeRef, edge_id: str, expected_revision: int | None) -> None:
        current = Neo4jGraphWriteAuthority._latest_tx(
            tx, "AFGraphEdgeRevision", scope, edge_id, GraphEdge
        )
        if current is None or current.status != "active" or current.revision != expected_revision:
            raise FoundationError("graph_version_conflict", "Graph edge revision changed")
        deleted = current.model_copy(update={
            "revision": current.revision + 1,
            "status": "deleted",
            "updated_at": datetime.now(timezone.utc),
        })
        Neo4jGraphWriteAuthority._insert_tx(
            tx, "AFGraphEdgeRevision", scope, edge_id, deleted.revision,
            deleted.status, deleted.model_dump_json()
        )

    @staticmethod
    def _active_node(tx, scope: ScopeRef, node_id: str) -> bool:
        node = Neo4jGraphWriteAuthority._latest_tx(
            tx, "AFGraphNodeRevision", scope, node_id, GraphNode
        )
        return node is not None and node.status == "active"

    @staticmethod
    def _validate_temporal_cardinality(tx, requested: GraphEdge) -> None:
        temporal = requested.temporal
        if temporal is None or temporal.lifecycle != "confirmed" or temporal.cardinality != "single":
            return
        for other in Neo4jGraphWriteAuthority._list_current_tx(
            tx, "AFGraphEdgeRevision", requested.scope, GraphEdge, None
        ):
            other_temporal = other.temporal
            if (
                other.edge_id == requested.edge_id
                or other.status != "active"
                or other.relation_type != requested.relation_type
                or other.from_node_id != requested.from_node_id
                or other_temporal is None
                or other_temporal.lifecycle != "confirmed"
            ):
                continue
            latest_start = max(temporal.valid_from, other_temporal.valid_from)
            ends = tuple(
                value
                for value in (temporal.valid_until, other_temporal.valid_until)
                if value is not None
            )
            earliest_end = min(ends) if ends else None
            if earliest_end is None or latest_start < earliest_end:
                raise FoundationError(
                    "temporal_cardinality_conflict",
                    "A single-valued temporal relation already covers this interval",
                )

    def _latest(self, label: str, scope: ScopeRef, resource_id: str, model):
        with self._driver.session(database=self._database) as session:
            row = session.run(
                f"MATCH (r:{label} {{scope_key:$scope_key, resource_id:$resource_id}}) "
                "RETURN r.payload AS payload ORDER BY r.revision DESC LIMIT 1",
                scope_key=scope_key(scope),
                resource_id=resource_id,
            ).single()
        return None if row is None else model.model_validate_json(row["payload"])

    def _exact(
        self, label: str, scope: ScopeRef, resource_id: str, revision: int, model
    ):
        with self._driver.session(database=self._database) as session:
            row = session.run(
                f"MATCH (r:{label} {{scope_key:$scope_key, resource_id:$resource_id, "
                "revision:$revision}) RETURN r.payload AS payload",
                scope_key=scope_key(scope),
                resource_id=resource_id,
                revision=revision,
            ).single()
        return None if row is None else model.model_validate_json(row["payload"])

    def _list_current(self, label: str, scope: ScopeRef, model, limit: int | None) -> tuple:
        limit_clause = " LIMIT $limit" if limit is not None else ""
        with self._driver.session(database=self._database) as session:
            rows = session.run(
                f"MATCH (r:{label} {{scope_key:$scope_key}}) "
                "WITH r.resource_id AS resource_id, max(r.revision) AS revision "
                f"MATCH (current:{label} {{scope_key:$scope_key, resource_id:resource_id, "
                "revision:revision}) RETURN current.payload AS payload "
                "ORDER BY resource_id" + limit_clause,
                scope_key=scope_key(scope),
                limit=limit,
            )
            return tuple(model.model_validate_json(row["payload"]) for row in rows)

    @staticmethod
    def _latest_tx(tx, label: str, scope: ScopeRef, resource_id: str, model):
        row = tx.run(
            f"MATCH (r:{label} {{scope_key:$scope_key, resource_id:$resource_id}}) "
            "RETURN r.payload AS payload ORDER BY r.revision DESC LIMIT 1",
            scope_key=scope_key(scope),
            resource_id=resource_id,
        ).single()
        return None if row is None else model.model_validate_json(row["payload"])

    @staticmethod
    def _list_current_tx(tx, label: str, scope: ScopeRef, model, limit: int | None) -> tuple:
        limit_clause = " LIMIT $limit" if limit is not None else ""
        rows = tx.run(
            f"MATCH (r:{label} {{scope_key:$scope_key}}) "
            "WITH r.resource_id AS resource_id, max(r.revision) AS revision "
            f"MATCH (current:{label} {{scope_key:$scope_key, resource_id:resource_id, "
            "revision:revision}) RETURN current.payload AS payload "
            "ORDER BY resource_id" + limit_clause,
            scope_key=scope_key(scope),
            limit=limit,
        )
        return tuple(model.model_validate_json(row["payload"]) for row in rows)

    @staticmethod
    def _insert_tx(
        tx,
        label: str,
        scope: ScopeRef,
        resource_id: str,
        revision: int,
        status: str,
        payload: str,
    ) -> None:
        tx.run(
            f"CREATE (r:{label} {{scope_key:$scope_key, resource_id:$resource_id, "
            "revision:$revision, status:$status, payload:$payload})",
            scope_key=scope_key(scope),
            resource_id=resource_id,
            revision=revision,
            status=status,
            payload=payload,
        ).consume()
