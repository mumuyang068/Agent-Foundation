from __future__ import annotations

from datetime import datetime, timezone
import json

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ScopeRef
from agent_foundation.modules.graph.contracts import (
    GraphApplyIntent,
    GraphOperationReceipt,
    GraphProposal,
    GraphSchema,
)


def _scope_key(scope: ScopeRef) -> str:
    return json.dumps(
        scope.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def _payload(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class PostgresGraphControlStore:
    """PostgreSQL proposal, apply-intent and Schema authority.

    This adapter deliberately exposes no Graph node or edge write method. Each
    mutation serializes one scope and stores its idempotency receipt in the
    same PostgreSQL transaction as the control-side state change.
    """

    def __init__(self, connection) -> None:
        self._connection = connection
        self._connection.autocommit = True
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_graph_control_scope_state (
                        scope_key TEXT PRIMARY KEY,
                        authority_sequence BIGINT NOT NULL DEFAULT 0
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_graph_proposals (
                        scope_key TEXT NOT NULL,
                        proposal_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        payload JSONB NOT NULL,
                        PRIMARY KEY(scope_key, proposal_id, revision)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_graph_schemas (
                        scope_key TEXT NOT NULL,
                        schema_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        payload JSONB NOT NULL,
                        PRIMARY KEY(scope_key, schema_id, version)
                    )
                    """
                )
                cursor.execute(
                    """
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
                        created_at TIMESTAMPTZ NOT NULL,
                        PRIMARY KEY(scope_key, proposal_id),
                        UNIQUE(scope_key, operation_id)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_graph_control_operations (
                        scope_key TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        command_digest TEXT NOT NULL,
                        action TEXT NOT NULL,
                        result_kind TEXT NOT NULL,
                        result_payload JSONB NOT NULL,
                        resource_ids JSONB NOT NULL,
                        committed_at TIMESTAMPTZ NOT NULL,
                        authority_sequence BIGINT NOT NULL,
                        PRIMARY KEY(scope_key, operation_id)
                    )
                    """
                )

    def save_proposal(
        self, proposal: GraphProposal, *, operation_id: str, command_digest: str
    ) -> GraphProposal:
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                self._lock_scope(cursor, proposal.scope)
                existing = self._existing(
                    cursor,
                    proposal.scope,
                    operation_id,
                    command_digest,
                    "proposal",
                    GraphProposal,
                )
                if existing is not None:
                    return existing
                if self._get_proposal(cursor, proposal.scope, proposal.proposal_id) is not None:
                    raise FoundationError(
                        "graph_proposal_conflict", "Graph proposal identity exists"
                    )
                self._insert_proposal(cursor, proposal)
                self._record(
                    cursor,
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
        with self._connection.cursor() as cursor:
            return self._get_proposal(cursor, scope, proposal_id)

    def list_proposals(
        self, scope: ScopeRef, *, states: tuple[str, ...], limit: int
    ) -> tuple[GraphProposal, ...]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT p.payload FROM af_graph_proposals p WHERE p.scope_key=%s "
                "AND p.revision=(SELECT MAX(x.revision) FROM af_graph_proposals x "
                "WHERE x.scope_key=p.scope_key AND x.proposal_id=p.proposal_id) "
                "AND p.payload->>'status'=ANY(%s) "
                "ORDER BY p.payload->>'updated_at' DESC,p.proposal_id LIMIT %s",
                (_scope_key(scope), list(states), limit),
            )
            rows = cursor.fetchall()
        return tuple(GraphProposal.model_validate_json(_payload(row[0])) for row in rows)

    def review_proposal(
        self,
        proposal: GraphProposal,
        *,
        expected_revision: int,
        operation_id: str,
        command_digest: str,
    ) -> GraphProposal:
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                self._lock_scope(cursor, proposal.scope)
                existing = self._existing(
                    cursor,
                    proposal.scope,
                    operation_id,
                    command_digest,
                    "proposal",
                    GraphProposal,
                )
                if existing is not None:
                    return existing
                current = self._expect_proposal(
                    cursor, proposal.scope, proposal.proposal_id, expected_revision
                )
                if current.status != "pending":
                    raise FoundationError(
                        "graph_proposal_not_reviewable",
                        "Only pending proposals can be reviewed",
                    )
                if proposal.revision != expected_revision + 1:
                    raise FoundationError(
                        "graph_proposal_invalid", "Graph proposal revision is invalid"
                    )
                self._insert_proposal(cursor, proposal)
                self._record(
                    cursor,
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
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                self._lock_scope(cursor, scope)
                current = self._expect_proposal(
                    cursor, scope, proposal_id, expected_revision
                )
                if current.status != "approved":
                    raise FoundationError(
                        "graph_proposal_not_approved", "Graph proposal is not approved"
                    )
                cursor.execute(
                    "SELECT expected_revision,operation_id,command_digest,"
                    "write_operation_id,write_command_digest,applied_by,grant_fingerprint,"
                    "policy_fingerprint,created_at FROM af_graph_apply_intents "
                    "WHERE scope_key=%s AND proposal_id=%s",
                    (_scope_key(scope), proposal_id),
                )
                row = cursor.fetchone()
                expected = (
                    expected_revision,
                    operation_id,
                    command_digest,
                    write_operation_id,
                    write_command_digest,
                )
                if row is not None:
                    actual = (int(row[0]), row[1], row[2], row[3], row[4])
                    if actual != expected:
                        raise FoundationError(
                            "graph_apply_intent_conflict",
                            "Graph proposal is already bound to another apply operation",
                        )
                    return self._intent_from_row(scope, proposal_id, row)
                cursor.execute(
                    "SELECT proposal_id FROM af_graph_apply_intents "
                    "WHERE scope_key=%s AND operation_id=%s",
                    (_scope_key(scope), operation_id),
                )
                if cursor.fetchone() is not None:
                    raise FoundationError(
                        "operation_id_conflict",
                        "Operation ID was used for another command",
                    )
                created_at = datetime.now(timezone.utc)
                cursor.execute(
                    "INSERT INTO af_graph_apply_intents(scope_key,proposal_id,"
                    "expected_revision,operation_id,command_digest,write_operation_id,"
                    "write_command_digest,applied_by,grant_fingerprint,policy_fingerprint,"
                    "created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        _scope_key(scope),
                        proposal_id,
                        expected_revision,
                        operation_id,
                        command_digest,
                        write_operation_id,
                        write_command_digest,
                        applied_by,
                        grant_fingerprint,
                        policy_fingerprint,
                        created_at,
                    ),
                )
                return GraphApplyIntent(
                    scope=scope,
                    proposal_id=proposal_id,
                    expected_revision=expected_revision,
                    operation_id=operation_id,
                    command_digest=command_digest,
                    write_operation_id=write_operation_id,
                    write_command_digest=write_command_digest,
                    applied_by=applied_by,
                    grant_fingerprint=grant_fingerprint,
                    policy_fingerprint=policy_fingerprint,
                    created_at=created_at,
                )

    def get_apply_intent(
        self, scope: ScopeRef, proposal_id: str
    ) -> GraphApplyIntent | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT expected_revision,operation_id,command_digest,write_operation_id,"
                "write_command_digest,applied_by,grant_fingerprint,policy_fingerprint,"
                "created_at FROM af_graph_apply_intents "
                "WHERE scope_key=%s AND proposal_id=%s",
                (_scope_key(scope), proposal_id),
            )
            row = cursor.fetchone()
        return None if row is None else self._intent_from_row(scope, proposal_id, row)

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
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                self._lock_scope(cursor, scope)
                existing = self._existing(
                    cursor,
                    scope,
                    operation_id,
                    command_digest,
                    "proposal",
                    GraphProposal,
                )
                if existing is not None:
                    return existing
                cursor.execute(
                    "SELECT expected_revision,operation_id,command_digest,"
                    "write_operation_id,write_command_digest,applied_by "
                    "FROM af_graph_apply_intents WHERE scope_key=%s AND proposal_id=%s",
                    (_scope_key(scope), proposal_id),
                )
                intent = cursor.fetchone()
                if intent is None:
                    raise FoundationError(
                        "graph_apply_intent_missing", "Graph apply intent does not exist"
                    )
                if (
                    int(intent[0]) != expected_revision
                    or intent[1] != operation_id
                    or intent[2] != command_digest
                    or intent[3] != write_receipt.operation_id
                    or intent[4] != write_receipt.command_digest
                    or write_receipt.action != "write"
                ):
                    raise FoundationError(
                        "graph_apply_intent_conflict", "Graph apply intent differs"
                    )
                current = self._expect_proposal(
                    cursor, scope, proposal_id, expected_revision
                )
                if current.status != "approved":
                    raise FoundationError(
                        "graph_proposal_not_approved", "Graph proposal is not approved"
                    )
                proposal = current.model_copy(
                    update={
                        "revision": current.revision + 1,
                        "status": "applied",
                        "applied_by": intent[5],
                        "updated_at": write_receipt.committed_at,
                    }
                )
                self._insert_proposal(cursor, proposal)
                self._record(
                    cursor,
                    scope,
                    operation_id,
                    command_digest,
                    "apply",
                    "proposal",
                    proposal,
                    (proposal_id, *write_receipt.resource_ids),
                    committed_at=write_receipt.committed_at,
                )
                return proposal

    def publish_schema(
        self, schema: GraphSchema, *, operation_id: str, command_digest: str
    ) -> GraphSchema:
        with self._connection.transaction():
            with self._connection.cursor() as cursor:
                self._lock_scope(cursor, schema.scope)
                existing = self._existing(
                    cursor,
                    schema.scope,
                    operation_id,
                    command_digest,
                    "schema",
                    GraphSchema,
                )
                if existing is not None:
                    return existing
                current = self._get_schema(
                    cursor, schema.scope, schema.schema_id, schema.version
                )
                if current is not None and current != schema:
                    raise FoundationError(
                        "graph_schema_conflict", "Graph schema identity differs"
                    )
                if current is None:
                    cursor.execute(
                        "INSERT INTO af_graph_schemas(scope_key,schema_id,version,payload) "
                        "VALUES(%s,%s,%s,%s::jsonb)",
                        (
                            _scope_key(schema.scope),
                            schema.schema_id,
                            schema.version,
                            schema.model_dump_json(),
                        ),
                    )
                    current = schema
                self._record(
                    cursor,
                    schema.scope,
                    operation_id,
                    command_digest,
                    "schema_publish",
                    "schema",
                    current,
                    (f"{schema.schema_id}:{schema.version}",),
                )
                return current

    def get_schema(
        self, scope: ScopeRef, schema_id: str, version: int
    ) -> GraphSchema | None:
        with self._connection.cursor() as cursor:
            return self._get_schema(cursor, scope, schema_id, version)

    def list_schemas(
        self, scope: ScopeRef, *, schema_id: str | None, limit: int
    ) -> tuple[GraphSchema, ...]:
        statement = "SELECT payload FROM af_graph_schemas WHERE scope_key=%s"
        parameters: list[object] = [_scope_key(scope)]
        if schema_id is not None:
            statement += " AND schema_id=%s"
            parameters.append(schema_id)
        statement += " ORDER BY schema_id,version DESC LIMIT %s"
        parameters.append(limit)
        with self._connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            rows = cursor.fetchall()
        return tuple(GraphSchema.model_validate_json(_payload(row[0])) for row in rows)

    def get_operation_receipt(
        self, scope: ScopeRef, operation_id: str
    ) -> GraphOperationReceipt | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT operation_id,command_digest,action,resource_ids,committed_at,"
                "authority_sequence FROM af_graph_control_operations "
                "WHERE scope_key=%s AND operation_id=%s",
                (_scope_key(scope), operation_id),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        resource_ids = row[3] if isinstance(row[3], list) else json.loads(row[3])
        return GraphOperationReceipt(
            operation_id=row[0],
            command_digest=row[1],
            action=row[2],
            resource_ids=tuple(resource_ids),
            committed_at=row[4],
            authority_watermark=int(row[5]),
        )

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _lock_scope(cursor, scope: ScopeRef) -> None:
        key = _scope_key(scope)
        cursor.execute(
            "INSERT INTO af_graph_control_scope_state(scope_key,authority_sequence) "
            "VALUES(%s,0) ON CONFLICT(scope_key) DO NOTHING",
            (key,),
        )
        cursor.execute(
            "SELECT authority_sequence FROM af_graph_control_scope_state "
            "WHERE scope_key=%s FOR UPDATE",
            (key,),
        )
        cursor.fetchone()

    @staticmethod
    def _get_proposal(cursor, scope: ScopeRef, proposal_id: str) -> GraphProposal | None:
        cursor.execute(
            "SELECT payload FROM af_graph_proposals WHERE scope_key=%s AND proposal_id=%s "
            "ORDER BY revision DESC LIMIT 1",
            (_scope_key(scope), proposal_id),
        )
        row = cursor.fetchone()
        return None if row is None else GraphProposal.model_validate_json(_payload(row[0]))

    @classmethod
    def _expect_proposal(
        cls, cursor, scope: ScopeRef, proposal_id: str, revision: int
    ) -> GraphProposal:
        proposal = cls._get_proposal(cursor, scope, proposal_id)
        if proposal is None:
            raise FoundationError(
                "graph_proposal_not_found", "Graph proposal does not exist"
            )
        if proposal.revision != revision:
            raise FoundationError(
                "graph_version_conflict", "Graph proposal revision changed"
            )
        return proposal

    @staticmethod
    def _insert_proposal(cursor, proposal: GraphProposal) -> None:
        cursor.execute(
            "INSERT INTO af_graph_proposals(scope_key,proposal_id,revision,payload) "
            "VALUES(%s,%s,%s,%s::jsonb)",
            (
                _scope_key(proposal.scope),
                proposal.proposal_id,
                proposal.revision,
                proposal.model_dump_json(),
            ),
        )

    @staticmethod
    def _get_schema(
        cursor, scope: ScopeRef, schema_id: str, version: int
    ) -> GraphSchema | None:
        cursor.execute(
            "SELECT payload FROM af_graph_schemas WHERE scope_key=%s AND schema_id=%s "
            "AND version=%s",
            (_scope_key(scope), schema_id, version),
        )
        row = cursor.fetchone()
        return None if row is None else GraphSchema.model_validate_json(_payload(row[0]))

    @staticmethod
    def _existing(
        cursor,
        scope: ScopeRef,
        operation_id: str,
        digest: str,
        kind: str,
        model,
    ):
        cursor.execute(
            "SELECT command_digest,result_kind,result_payload "
            "FROM af_graph_control_operations WHERE scope_key=%s AND operation_id=%s",
            (_scope_key(scope), operation_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise FoundationError(
                "operation_id_conflict", "Operation ID was used for another command"
            )
        if row[1] != kind:
            raise FoundationError(
                "graph_receipt_invalid", "Graph operation result kind differs"
            )
        return model.model_validate_json(_payload(row[2]))

    @staticmethod
    def _record(
        cursor,
        scope: ScopeRef,
        operation_id: str,
        digest: str,
        action: str,
        result_kind: str,
        result,
        resource_ids: tuple[str, ...],
        *,
        committed_at: datetime | None = None,
    ) -> None:
        instant = committed_at or datetime.now(timezone.utc)
        key = _scope_key(scope)
        cursor.execute(
            "UPDATE af_graph_control_scope_state SET authority_sequence=authority_sequence+1 "
            "WHERE scope_key=%s RETURNING authority_sequence",
            (key,),
        )
        sequence = int(cursor.fetchone()[0])
        cursor.execute(
            "INSERT INTO af_graph_control_operations(scope_key,operation_id,command_digest,"
            "action,result_kind,result_payload,resource_ids,committed_at,authority_sequence) "
            "VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)",
            (
                key,
                operation_id,
                digest,
                action,
                result_kind,
                result.model_dump_json(),
                json.dumps(resource_ids),
                instant,
                sequence,
            ),
        )

    @staticmethod
    def _intent_from_row(
        scope: ScopeRef, proposal_id: str, row
    ) -> GraphApplyIntent:
        return GraphApplyIntent(
            scope=scope,
            proposal_id=proposal_id,
            expected_revision=int(row[0]),
            operation_id=row[1],
            command_digest=row[2],
            write_operation_id=row[3],
            write_command_digest=row[4],
            applied_by=row[5],
            grant_fingerprint=row[6],
            policy_fingerprint=row[7],
            created_at=row[8],
        )
