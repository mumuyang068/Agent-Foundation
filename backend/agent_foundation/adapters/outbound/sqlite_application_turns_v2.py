from __future__ import annotations

from datetime import datetime
import sqlite3
import threading

from agent_foundation.kernel.errors import FoundationError
from agent_foundation.kernel.primitives import ScopeRef
from agent_foundation.modules.applications.contracts import AgentTurn, TurnOperationReceipt


def _scope_key(scope: ScopeRef) -> str:
    return scope.model_dump_json()


class SqliteTurnAuthority:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS af_application_turns (
                    scope_key TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope_key, turn_id)
                );
                CREATE TABLE IF NOT EXISTS af_application_turn_operations (
                    scope_key TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    command_digest TEXT NOT NULL,
                    action TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    PRIMARY KEY(scope_key, operation_id)
                );
                """
            )

    def create(self, turn, *, operation_id, command_digest):
        with self._transaction():
            replay = self._replay(turn.scope, operation_id, command_digest)
            if replay is not None:
                return self._required(turn.scope, replay.turn_id)
            self._connection.execute(
                "INSERT INTO af_application_turns VALUES (?,?,?,?,?,?)",
                (
                    _scope_key(turn.scope), turn.turn_id, turn.revision, turn.state,
                    turn.model_dump_json(), turn.updated_at.isoformat(),
                ),
            )
            self._insert_receipt(turn.scope, operation_id, command_digest, "prepare", turn.turn_id, turn.updated_at)
        return turn

    def get(self, scope: ScopeRef, turn_id: str):
        row = self._connection.execute(
            "SELECT payload_json FROM af_application_turns WHERE scope_key=? AND turn_id=?",
            (_scope_key(scope), turn_id),
        ).fetchone()
        return None if row is None else AgentTurn.model_validate_json(row["payload_json"])

    def record_observation(self, turn, *, expected_revision, operation_id, command_digest):
        return self._update(
            turn, expected_revision=expected_revision, operation_id=operation_id,
            command_digest=command_digest, action="observe", expected_state="prepared",
        )

    def record_outcome(self, turn, *, expected_revision, operation_id, command_digest):
        return self._update(
            turn, expected_revision=expected_revision, operation_id=operation_id,
            command_digest=command_digest, action="outcome", expected_state="observed",
        )

    def get_receipt(self, scope, operation_id):
        row = self._connection.execute(
            "SELECT * FROM af_application_turn_operations WHERE scope_key=? AND operation_id=?",
            (_scope_key(scope), operation_id),
        ).fetchone()
        return None if row is None else self._receipt(row)

    def close(self) -> None:
        self._connection.close()

    def _update(self, turn, *, expected_revision, operation_id, command_digest, action, expected_state):
        with self._transaction():
            replay = self._replay(turn.scope, operation_id, command_digest)
            if replay is not None:
                return self._required(turn.scope, replay.turn_id)
            current = self._required(turn.scope, turn.turn_id)
            if current.revision != expected_revision or current.state != expected_state:
                raise FoundationError("application_turn_conflict", "Agent turn state changed")
            changed = self._connection.execute(
                """
                UPDATE af_application_turns SET revision=?,state=?,payload_json=?,updated_at=?
                WHERE scope_key=? AND turn_id=? AND revision=?
                """,
                (
                    turn.revision, turn.state, turn.model_dump_json(), turn.updated_at.isoformat(),
                    _scope_key(turn.scope), turn.turn_id, expected_revision,
                ),
            ).rowcount
            if changed != 1:
                raise FoundationError("application_turn_conflict", "Agent turn revision changed")
            self._insert_receipt(
                turn.scope, operation_id, command_digest, action, turn.turn_id, turn.updated_at
            )
        return turn

    def _required(self, scope, turn_id):
        turn = self.get(scope, turn_id)
        if turn is None:
            raise FoundationError("application_turn_not_found", "Agent turn does not exist")
        return turn

    def _replay(self, scope, operation_id, digest):
        receipt = self.get_receipt(scope, operation_id)
        if receipt is not None and receipt.command_digest != digest:
            raise FoundationError("operation_id_conflict", "Operation ID has different turn content")
        return receipt

    def _insert_receipt(self, scope, operation_id, digest, action, turn_id, now):
        self._connection.execute(
            "INSERT INTO af_application_turn_operations VALUES (?,?,?,?,?,?)",
            (_scope_key(scope), operation_id, digest, action, turn_id, now.isoformat()),
        )

    @staticmethod
    def _receipt(row):
        return TurnOperationReceipt(
            operation_id=row["operation_id"], command_digest=row["command_digest"],
            action=row["action"], turn_id=row["turn_id"],
            committed_at=datetime.fromisoformat(row["committed_at"]),
        )

    class _Transaction:
        def __init__(self, outer):
            self.outer = outer
        def __enter__(self):
            self.outer._lock.acquire()
            self.outer._connection.execute("BEGIN IMMEDIATE")
        def __exit__(self, kind, value, traceback):
            try:
                self.outer._connection.execute("COMMIT" if kind is None else "ROLLBACK")
            finally:
                self.outer._lock.release()

    def _transaction(self):
        return self._Transaction(self)
