from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from threading import RLock

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ScopeRef
from agent_foundation.modules.graph.contracts import (
    GraphApplyIntent,
    GraphEdge,
    GraphNode,
    GraphOperation,
    GraphOperationReceipt,
    GraphProposal,
    GraphSchema,
)
from agent_foundation.modules.graph.domain import validate_temporal_operations


def scope_key(scope: ScopeRef) -> str:
    return json.dumps(scope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class SqliteGraphAuthority:
    def __init__(
        self, connection: sqlite3.Connection, *, role: str = "combined"
    ) -> None:
        if role not in {"combined", "control", "write"}:
            raise ValueError("Graph SQLite role must be combined, control or write")
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._role = role
        suffix = "" if role == "combined" else f"_{role}"
        self._operations_table = f"af_graph{suffix}_operations"
        self._audit_table = f"af_graph{suffix}_audit"
        definitions = []
        if role in {"combined", "control"}:
            definitions.append(
                """
                CREATE TABLE IF NOT EXISTS af_graph_proposals (
                    scope_key TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, proposal_id, revision)
                );
                CREATE TABLE IF NOT EXISTS af_graph_schemas (
                    scope_key TEXT NOT NULL,
                    schema_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, schema_id, version)
                );
                CREATE TABLE IF NOT EXISTS af_graph_apply_intents (
                    scope_key TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL,
                    operation_id TEXT NOT NULL,
                    command_digest TEXT NOT NULL,
                    write_operation_id TEXT NOT NULL,
                    write_command_digest TEXT NOT NULL,
                    applied_by TEXT NOT NULL,
                    grant_fingerprint TEXT NOT NULL,
                    policy_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(scope_key, proposal_id),
                    UNIQUE(scope_key, operation_id)
                );
                """
            )
        if role in {"combined", "write"}:
            definitions.append(
                """
                CREATE TABLE IF NOT EXISTS af_graph_nodes (
                    scope_key TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, node_id, revision)
                );
                CREATE TABLE IF NOT EXISTS af_graph_edges (
                    scope_key TEXT NOT NULL,
                    edge_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, edge_id, revision)
                );
                """
            )
        definitions.append(
            f"""
            CREATE TABLE IF NOT EXISTS {self._operations_table} (
                operation_id TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                command_digest TEXT NOT NULL,
                action TEXT NOT NULL,
                result_kind TEXT NOT NULL,
                result_payload TEXT NOT NULL,
                resource_ids TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                authority_sequence INTEGER NOT NULL,
                PRIMARY KEY(scope_key, operation_id)
            );
            CREATE TABLE IF NOT EXISTS {self._audit_table} (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_key TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                action TEXT NOT NULL,
                resource_ids TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            """
        )
        with self._connection:
            self._connection.executescript("\n".join(definitions))

    def save_proposal(
        self, proposal: GraphProposal, *, operation_id: str, command_digest: str
    ) -> GraphProposal:
        with self._lock, self._connection:
            existing = self._existing(
                proposal.scope, operation_id, command_digest, "proposal", GraphProposal
            )
            if existing is not None:
                return existing
            if self.get_proposal(proposal.scope, proposal.proposal_id) is not None:
                raise FoundationError("graph_proposal_conflict", "Graph proposal identity exists")
            self._insert_proposal(proposal)
            self._record(
                proposal.scope,
                operation_id,
                command_digest,
                "propose",
                "proposal",
                proposal,
                (proposal.proposal_id,),
            )
            return proposal

    def get_proposal(self, scope: ScopeRef, proposal_id: str) -> GraphProposal | None:
        return self._latest("af_graph_proposals", scope, "proposal_id", proposal_id, GraphProposal)

    def list_proposals(
        self, scope: ScopeRef, *, states: tuple[str, ...], limit: int
    ) -> tuple[GraphProposal, ...]:
        return self._list_current(
            "af_graph_proposals", scope, "proposal_id", GraphProposal, states, limit
        )

    def review_proposal(
        self,
        proposal: GraphProposal,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
    ) -> GraphProposal:
        with self._lock, self._connection:
            existing = self._existing(
                proposal.scope, operation_id, command_digest, "proposal", GraphProposal
            )
            if existing is not None:
                return existing
            current = self._expect_proposal(proposal.scope, proposal.proposal_id, expected_revision)
            if current.status != "pending":
                raise FoundationError("graph_proposal_not_reviewable", "Only pending proposals can be reviewed")
            if proposal.revision != expected_revision + 1:
                raise FoundationError("graph_proposal_invalid", "Graph proposal revision is invalid")
            self._insert_proposal(proposal)
            self._record(
                proposal.scope,
                operation_id,
                command_digest,
                "review",
                "proposal",
                proposal,
                (proposal.proposal_id,),
            )
            return proposal

    def prepare_apply(
        self,
        scope: ScopeRef,
        proposal_id: str,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
        write_operation_id: str,
        write_command_digest: str,
        applied_by: str,
        grant_fingerprint: str,
        policy_fingerprint: str,
    ) -> GraphApplyIntent:
        with self._lock, self._connection:
            current = self._expect_proposal(scope, proposal_id, expected_revision)
            if current.status != "approved":
                raise FoundationError("graph_proposal_not_approved", "Graph proposal is not approved")
            row = self._connection.execute(
                "SELECT expected_revision,operation_id,command_digest,write_operation_id,"
                "write_command_digest,applied_by,grant_fingerprint,policy_fingerprint,created_at "
                "FROM af_graph_apply_intents WHERE scope_key=? AND proposal_id=?",
                (scope_key(scope), proposal_id),
            ).fetchone()
            expected = (
                expected_revision,
                operation_id,
                command_digest,
                write_operation_id,
                write_command_digest,
            )
            if row is not None:
                actual = (
                    int(row["expected_revision"]),
                    row["operation_id"],
                    row["command_digest"],
                    row["write_operation_id"],
                    row["write_command_digest"],
                )
                if actual != expected:
                    raise FoundationError(
                        "graph_apply_intent_conflict",
                        "Graph proposal is already bound to another apply operation",
                    )
                return self._intent_from_row(scope, proposal_id, row)
            operation_row = self._connection.execute(
                "SELECT proposal_id FROM af_graph_apply_intents WHERE scope_key=? AND operation_id=?",
                (scope_key(scope), operation_id),
            ).fetchone()
            if operation_row is not None:
                raise FoundationError(
                    "operation_id_conflict", "Operation ID was used for another command"
                )
            self._connection.execute(
                "INSERT INTO af_graph_apply_intents(scope_key,proposal_id,expected_revision,"
                "operation_id,command_digest,write_operation_id,write_command_digest,applied_by,"
                "grant_fingerprint,policy_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scope_key(scope),
                    proposal_id,
                    expected_revision,
                    operation_id,
                    command_digest,
                    write_operation_id,
                    write_command_digest,
                    applied_by,
                    grant_fingerprint,
                    policy_fingerprint,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            intent = self.get_apply_intent(scope, proposal_id)
            assert intent is not None
            return intent

    def get_apply_intent(
        self, scope: ScopeRef, proposal_id: str
    ) -> GraphApplyIntent | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT expected_revision,operation_id,command_digest,write_operation_id,"
                "write_command_digest,applied_by,grant_fingerprint,policy_fingerprint,created_at "
                "FROM af_graph_apply_intents WHERE scope_key=? AND proposal_id=?",
                (scope_key(scope), proposal_id),
            ).fetchone()
        return None if row is None else self._intent_from_row(scope, proposal_id, row)

    def apply_operations(
        self,
        scope: ScopeRef,
        operations: tuple[GraphOperation, ...],
        *,
        operation_id: str,
        command_digest: str,
    ) -> GraphOperationReceipt:
        with self._lock, self._connection:
            existing = self._existing(
                scope, operation_id, command_digest, "write_receipt", GraphOperationReceipt
            )
            if existing is not None:
                return existing
            validate_temporal_operations(operations)
            changed: list[str] = []
            for operation in operations:
                if operation.action == "put_node":
                    assert operation.node is not None
                    self._put_node(operation.node, operation.expected_revision)
                    changed.append(operation.node.node_id)
                elif operation.action == "put_edge":
                    assert operation.edge is not None
                    if not self._active_node(scope, operation.edge.from_node_id) or not self._active_node(
                        scope, operation.edge.to_node_id
                    ):
                        raise FoundationError("graph_edge_endpoint_missing", "Graph edge endpoints must be active")
                    self._put_edge(operation.edge, operation.expected_revision)
                    changed.append(operation.edge.edge_id)
                elif operation.action == "delete_edge":
                    self._delete_edge(scope, str(operation.target_id), operation.expected_revision)
                    changed.append(str(operation.target_id))
                else:
                    changed.extend(self._delete_node(
                        scope, str(operation.target_id), operation.expected_revision
                    ))
            return self._record_write(
                scope, operation_id, command_digest, tuple(changed)
            )

    def complete_apply(
        self,
        scope: ScopeRef,
        proposal_id: str,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
        write_receipt: GraphOperationReceipt,
    ) -> GraphProposal:
        with self._lock, self._connection:
            existing = self._existing(
                scope, operation_id, command_digest, "proposal", GraphProposal
            )
            if existing is not None:
                return existing
            intent = self._connection.execute(
                "SELECT expected_revision,operation_id,command_digest,write_operation_id,"
                "write_command_digest,applied_by "
                "FROM af_graph_apply_intents WHERE scope_key=? AND proposal_id=?",
                (scope_key(scope), proposal_id),
            ).fetchone()
            if intent is None:
                raise FoundationError("graph_apply_intent_missing", "Graph apply intent does not exist")
            if (
                int(intent["expected_revision"]) != expected_revision
                or intent["operation_id"] != operation_id
                or intent["command_digest"] != command_digest
                or intent["write_operation_id"] != write_receipt.operation_id
                or intent["write_command_digest"] != write_receipt.command_digest
                or write_receipt.action != "write"
            ):
                raise FoundationError("graph_apply_intent_conflict", "Graph apply intent differs")
            current = self._expect_proposal(scope, proposal_id, expected_revision)
            if current.status != "approved":
                raise FoundationError("graph_proposal_not_approved", "Graph proposal is not approved")
            proposal = current.model_copy(update={
                "revision": current.revision + 1,
                "status": "applied",
                "applied_by": intent["applied_by"],
                "updated_at": write_receipt.committed_at,
            })
            self._insert_proposal(proposal)
            self._record(
                proposal.scope,
                operation_id,
                command_digest,
                "apply",
                "proposal",
                proposal,
                (proposal.proposal_id, *write_receipt.resource_ids),
            )
            return proposal

    @staticmethod
    def _intent_from_row(
        scope: ScopeRef, proposal_id: str, row: sqlite3.Row
    ) -> GraphApplyIntent:
        return GraphApplyIntent(
            scope=scope,
            proposal_id=proposal_id,
            expected_revision=int(row["expected_revision"]),
            operation_id=row["operation_id"],
            command_digest=row["command_digest"],
            write_operation_id=row["write_operation_id"],
            write_command_digest=row["write_command_digest"],
            applied_by=row["applied_by"],
            grant_fingerprint=row["grant_fingerprint"],
            policy_fingerprint=row["policy_fingerprint"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_node(self, scope: ScopeRef, node_id: str) -> GraphNode | None:
        return self._latest("af_graph_nodes", scope, "node_id", node_id, GraphNode)

    def list_nodes(self, scope: ScopeRef, *, limit: int) -> tuple[GraphNode, ...]:
        return self.candidates(scope, limit=limit)

    def get_node_revision(
        self, scope: ScopeRef, node_id: str, revision: int
    ) -> GraphNode | None:
        return self._exact("af_graph_nodes", scope, "node_id", node_id, revision, GraphNode)

    def get_edge(self, scope: ScopeRef, edge_id: str) -> GraphEdge | None:
        return self._latest("af_graph_edges", scope, "edge_id", edge_id, GraphEdge)

    def list_edges(self, scope: ScopeRef, *, limit: int) -> tuple[GraphEdge, ...]:
        return self._list_current(
            "af_graph_edges", scope, "edge_id", GraphEdge, ("active",), limit
        )

    def get_edge_revision(
        self, scope: ScopeRef, edge_id: str, revision: int
    ) -> GraphEdge | None:
        return self._exact("af_graph_edges", scope, "edge_id", edge_id, revision, GraphEdge)

    def candidates(self, scope: ScopeRef, *, limit: int) -> tuple[GraphNode, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT n.payload FROM af_graph_nodes n WHERE n.scope_key=? AND "
                "n.revision=(SELECT MAX(x.revision) FROM af_graph_nodes x WHERE "
                "x.scope_key=n.scope_key AND x.node_id=n.node_id) AND "
                "json_extract(n.payload, '$.status')='active' ORDER BY "
                "json_extract(n.payload, '$.updated_at') DESC, n.node_id LIMIT ?",
                (scope_key(scope), limit),
            ).fetchall()
        return tuple(GraphNode.model_validate_json(row[0]) for row in rows)

    def incident_edges(self, scope: ScopeRef, node_id: str) -> tuple[GraphEdge, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT e.payload FROM af_graph_edges e WHERE e.scope_key=? AND "
                "e.revision=(SELECT MAX(x.revision) FROM af_graph_edges x WHERE "
                "x.scope_key=e.scope_key AND x.edge_id=e.edge_id) AND "
                "json_extract(e.payload, '$.status')='active' AND "
                "(json_extract(e.payload, '$.from_node_id')=? OR "
                "json_extract(e.payload, '$.to_node_id')=?) ORDER BY e.edge_id",
                (scope_key(scope), node_id, node_id),
            ).fetchall()
        return tuple(GraphEdge.model_validate_json(row[0]) for row in rows)

    def publish_schema(
        self, schema: GraphSchema, *, operation_id: str, command_digest: str
    ) -> GraphSchema:
        with self._lock, self._connection:
            existing = self._existing(
                schema.scope, operation_id, command_digest, "schema", GraphSchema
            )
            if existing is not None:
                return existing
            current = self.get_schema(schema.scope, schema.schema_id, schema.version)
            if current is not None:
                if current != schema:
                    raise FoundationError("graph_schema_conflict", "Graph schema identity differs")
                self._record(
                    schema.scope,
                    operation_id,
                    command_digest,
                    "schema_publish",
                    "schema",
                    current,
                    (f"{schema.schema_id}:{schema.version}",),
                )
                return current
            self._connection.execute(
                "INSERT INTO af_graph_schemas(scope_key,schema_id,version,payload) VALUES(?,?,?,?)",
                (scope_key(schema.scope), schema.schema_id, schema.version, schema.model_dump_json()),
            )
            self._record(
                schema.scope,
                operation_id,
                command_digest,
                "schema_publish",
                "schema",
                schema,
                (f"{schema.schema_id}:{schema.version}",),
            )
            return schema

    def get_schema(self, scope: ScopeRef, schema_id: str, version: int) -> GraphSchema | None:
        return self._exact("af_graph_schemas", scope, "schema_id", schema_id, version, GraphSchema, "version")

    def list_schemas(self, scope: ScopeRef, *, schema_id: str | None, limit: int):
        query = "SELECT payload FROM af_graph_schemas WHERE scope_key=?"
        parameters: list[object] = [scope_key(scope)]
        if schema_id is not None:
            query += " AND schema_id=?"
            parameters.append(schema_id)
        query += " ORDER BY schema_id,version DESC LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, tuple(parameters)).fetchall()
        return tuple(GraphSchema.model_validate_json(row["payload"]) for row in rows)

    def get_operation_receipt(
        self, scope: ScopeRef, operation_id: str
    ) -> GraphOperationReceipt | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT operation_id,command_digest,action,resource_ids,committed_at,authority_sequence "
                f"FROM {self._operations_table} WHERE scope_key=? AND operation_id=?",
                (scope_key(scope), operation_id),
            ).fetchone()
        if row is None:
            return None
        return GraphOperationReceipt(
            operation_id=row["operation_id"],
            command_digest=row["command_digest"],
            action=row["action"],
            resource_ids=tuple(json.loads(row["resource_ids"])),
            committed_at=row["committed_at"],
            authority_watermark=row["authority_sequence"],
        )

    def authority_watermark(self, scope: ScopeRef) -> int:
        with self._lock:
            row = self._connection.execute(
                f"SELECT COALESCE(MAX(sequence),0) FROM {self._audit_table} WHERE scope_key=?",
                (scope_key(scope),),
            ).fetchone()
        return int(row[0])

    def close(self) -> None:
        self._connection.close()

    def _put_node(self, requested: GraphNode, expected_revision: int | None) -> None:
        current = self.get_node(requested.scope, requested.node_id)
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
        self._insert_resource("af_graph_nodes", "node_id", stored.node_id, stored)

    def _put_edge(self, requested: GraphEdge, expected_revision: int | None) -> None:
        current = self.get_edge(requested.scope, requested.edge_id)
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
        self._validate_temporal_cardinality(stored)
        self._insert_resource("af_graph_edges", "edge_id", stored.edge_id, stored)

    def _validate_temporal_cardinality(self, requested: GraphEdge) -> None:
        temporal = requested.temporal
        if temporal is None or temporal.lifecycle != "confirmed" or temporal.cardinality != "single":
            return
        for other in self._list_current(
            "af_graph_edges", requested.scope, "edge_id", GraphEdge, ("active",), None
        ):
            other_temporal = other.temporal
            if (
                other.edge_id == requested.edge_id
                or other.relation_type != requested.relation_type
                or other.from_node_id != requested.from_node_id
                or other_temporal is None
                or other_temporal.lifecycle != "confirmed"
            ):
                continue
            latest_start = max(temporal.valid_from, other_temporal.valid_from)
            ends = tuple(
                value for value in (temporal.valid_until, other_temporal.valid_until) if value is not None
            )
            earliest_end = min(ends) if ends else None
            if earliest_end is None or latest_start < earliest_end:
                raise FoundationError(
                    "temporal_cardinality_conflict",
                    "A single-valued temporal relation already covers this interval",
                )

    def _delete_node(
        self, scope: ScopeRef, node_id: str, expected_revision: int | None
    ) -> tuple[str, ...]:
        current = self.get_node(scope, node_id)
        if current is None or current.status != "active" or current.revision != expected_revision:
            raise FoundationError("graph_version_conflict", "Graph node revision changed")
        deleted = current.model_copy(update={
            "revision": current.revision + 1,
            "status": "deleted",
            "updated_at": datetime.now(timezone.utc),
        })
        removed_edges = self.incident_edges(scope, node_id)
        for edge in removed_edges:
            self._delete_edge(scope, edge.edge_id, edge.revision)
        self._insert_resource("af_graph_nodes", "node_id", node_id, deleted)
        return (node_id, *(edge.edge_id for edge in removed_edges))

    def _delete_edge(self, scope: ScopeRef, edge_id: str, expected_revision: int | None) -> None:
        current = self.get_edge(scope, edge_id)
        if current is None or current.status != "active" or current.revision != expected_revision:
            raise FoundationError("graph_version_conflict", "Graph edge revision changed")
        deleted = current.model_copy(update={
            "revision": current.revision + 1,
            "status": "deleted",
            "updated_at": datetime.now(timezone.utc),
        })
        self._insert_resource("af_graph_edges", "edge_id", edge_id, deleted)

    def _active_node(self, scope: ScopeRef, node_id: str) -> bool:
        node = self.get_node(scope, node_id)
        return node is not None and node.status == "active"

    def _expect_proposal(self, scope: ScopeRef, proposal_id: str, revision: int) -> GraphProposal:
        proposal = self.get_proposal(scope, proposal_id)
        if proposal is None:
            raise FoundationError("graph_proposal_not_found", "Graph proposal does not exist")
        if proposal.revision != revision:
            raise FoundationError("graph_version_conflict", "Graph proposal revision changed")
        return proposal

    def _insert_proposal(self, proposal: GraphProposal) -> None:
        self._insert_resource(
            "af_graph_proposals", "proposal_id", proposal.proposal_id, proposal
        )

    def _insert_resource(self, table: str, id_column: str, resource_id: str, model) -> None:
        self._connection.execute(
            f"INSERT INTO {table}(scope_key,{id_column},revision,payload) VALUES(?,?,?,?)",
            (scope_key(model.scope), resource_id, model.revision, model.model_dump_json()),
        )

    def _latest(self, table: str, scope: ScopeRef, id_column: str, resource_id: str, model):
        with self._lock:
            row = self._connection.execute(
                f"SELECT payload FROM {table} WHERE scope_key=? AND {id_column}=? "
                "ORDER BY revision DESC LIMIT 1",
                (scope_key(scope), resource_id),
            ).fetchone()
        return None if row is None else model.model_validate_json(row[0])

    def _list_current(
        self,
        table: str,
        scope: ScopeRef,
        id_column: str,
        model,
        states: tuple[str, ...],
        limit: int | None,
    ) -> tuple:
        parameters: list[object] = [scope_key(scope), *states]
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT r.payload FROM {table} r WHERE r.scope_key=? AND "
                f"r.revision=(SELECT MAX(x.revision) FROM {table} x WHERE "
                f"x.scope_key=r.scope_key AND x.{id_column}=r.{id_column}) AND "
                f"json_extract(r.payload, '$.status') IN ({','.join('?' for _ in states)}) "
                f"ORDER BY json_extract(r.payload, '$.updated_at') DESC, r.{id_column}{limit_clause}",
                parameters,
            ).fetchall()
        return tuple(model.model_validate_json(row[0]) for row in rows)

    def _exact(
        self,
        table: str,
        scope: ScopeRef,
        id_column: str,
        resource_id: str,
        revision: int,
        model,
        revision_column: str = "revision",
    ):
        with self._lock:
            row = self._connection.execute(
                f"SELECT payload FROM {table} WHERE scope_key=? AND {id_column}=? AND {revision_column}=?",
                (scope_key(scope), resource_id, revision),
            ).fetchone()
        return None if row is None else model.model_validate_json(row[0])

    def _existing(self, scope: ScopeRef, operation_id: str, digest: str, kind: str, model):
        row = self._connection.execute(
            f"SELECT command_digest,result_kind,result_payload FROM {self._operations_table} "
            "WHERE scope_key=? AND operation_id=?",
            (scope_key(scope), operation_id),
        ).fetchone()
        if row is None:
            return None
        if row["command_digest"] != digest:
            raise FoundationError("operation_id_conflict", "Operation ID was used for another command")
        if row["result_kind"] != kind:
            raise FoundationError("graph_receipt_invalid", "Graph operation result kind differs")
        return model.model_validate_json(row["result_payload"])

    def _record(
        self,
        scope: ScopeRef,
        operation_id: str,
        digest: str,
        action: str,
        result_kind: str,
        result,
        resource_ids: tuple[str, ...],
    ) -> None:
        instant = datetime.now(timezone.utc)
        cursor = self._connection.execute(
            f"INSERT INTO {self._audit_table}(scope_key,operation_id,action,resource_ids,occurred_at) "
            "VALUES(?,?,?,?,?)",
            (scope_key(scope), operation_id, action, json.dumps(resource_ids), instant.isoformat()),
        )
        self._connection.execute(
            f"INSERT INTO {self._operations_table}(operation_id,scope_key,command_digest,action,result_kind,"
            "result_payload,resource_ids,committed_at,authority_sequence) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                operation_id,
                scope_key(scope),
                digest,
                action,
                result_kind,
                result.model_dump_json(),
                json.dumps(resource_ids),
                instant.isoformat(),
                int(cursor.lastrowid),
            ),
        )

    def _record_write(
        self,
        scope: ScopeRef,
        operation_id: str,
        digest: str,
        resource_ids: tuple[str, ...],
    ) -> GraphOperationReceipt:
        instant = datetime.now(timezone.utc)
        cursor = self._connection.execute(
            f"INSERT INTO {self._audit_table}(scope_key,operation_id,action,resource_ids,occurred_at) "
            "VALUES(?,?,?,?,?)",
            (
                scope_key(scope),
                operation_id,
                "write",
                json.dumps(resource_ids),
                instant.isoformat(),
            ),
        )
        receipt = GraphOperationReceipt(
            operation_id=operation_id,
            command_digest=digest,
            action="write",
            resource_ids=resource_ids,
            committed_at=instant,
            authority_watermark=int(cursor.lastrowid),
        )
        self._connection.execute(
            f"INSERT INTO {self._operations_table}(operation_id,scope_key,command_digest,action,result_kind,"
            "result_payload,resource_ids,committed_at,authority_sequence) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                operation_id,
                scope_key(scope),
                digest,
                "write",
                "write_receipt",
                receipt.model_dump_json(),
                json.dumps(resource_ids),
                instant.isoformat(),
                int(cursor.lastrowid),
            ),
        )
        return receipt


class SqliteGraphControlStore:
    """Control-side adapter exposing proposal, intent and Schema operations only."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._implementation = SqliteGraphAuthority(connection, role="control")

    def save_proposal(self, *args, **kwargs):
        return self._implementation.save_proposal(*args, **kwargs)

    def get_proposal(self, *args, **kwargs):
        return self._implementation.get_proposal(*args, **kwargs)

    def list_proposals(self, *args, **kwargs):
        return self._implementation.list_proposals(*args, **kwargs)

    def review_proposal(self, *args, **kwargs):
        return self._implementation.review_proposal(*args, **kwargs)

    def prepare_apply(self, *args, **kwargs):
        return self._implementation.prepare_apply(*args, **kwargs)

    def get_apply_intent(self, *args, **kwargs):
        return self._implementation.get_apply_intent(*args, **kwargs)

    def complete_apply(self, *args, **kwargs):
        return self._implementation.complete_apply(*args, **kwargs)

    def publish_schema(self, *args, **kwargs):
        return self._implementation.publish_schema(*args, **kwargs)

    def get_schema(self, *args, **kwargs):
        return self._implementation.get_schema(*args, **kwargs)

    def list_schemas(self, *args, **kwargs):
        return self._implementation.list_schemas(*args, **kwargs)

    def get_operation_receipt(self, *args, **kwargs):
        return self._implementation.get_operation_receipt(*args, **kwargs)

    def close(self) -> None:
        self._implementation.close()


class SqliteGraphWriteAuthority:
    """Write-side adapter exposing graph authority operations only."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._implementation = SqliteGraphAuthority(connection, role="write")

    def apply_operations(self, *args, **kwargs):
        return self._implementation.apply_operations(*args, **kwargs)

    def get_node(self, *args, **kwargs):
        return self._implementation.get_node(*args, **kwargs)

    def list_nodes(self, *args, **kwargs):
        return self._implementation.list_nodes(*args, **kwargs)

    def get_node_revision(self, *args, **kwargs):
        return self._implementation.get_node_revision(*args, **kwargs)

    def get_edge(self, *args, **kwargs):
        return self._implementation.get_edge(*args, **kwargs)

    def list_edges(self, *args, **kwargs):
        return self._implementation.list_edges(*args, **kwargs)

    def get_edge_revision(self, *args, **kwargs):
        return self._implementation.get_edge_revision(*args, **kwargs)

    def incident_edges(self, *args, **kwargs):
        return self._implementation.incident_edges(*args, **kwargs)

    def get_operation_receipt(self, *args, **kwargs):
        return self._implementation.get_operation_receipt(*args, **kwargs)

    def authority_watermark(self, *args, **kwargs):
        return self._implementation.authority_watermark(*args, **kwargs)

    def close(self) -> None:
        self._implementation.close()
