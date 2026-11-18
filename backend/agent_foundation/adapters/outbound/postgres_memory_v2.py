from __future__ import annotations

import re
from threading import RLock

from agent_foundation.adapters.outbound.relational_memory_v2 import (
    RelationalMemoryAuthority,
    scope_key,
)


class _HybridRow:
    def __init__(self, values, names: tuple[str, ...]) -> None:
        self._values = tuple(values)
        self._indexes = {name: index for index, name in enumerate(names)}

    def __getitem__(self, key):
        if isinstance(key, str):
            key = self._indexes[key]
        return self._values[key]


def _hybrid_row_factory(cursor):
    names = tuple(column.name for column in cursor.description)

    def make_row(values):
        return _HybridRow(values, names)

    return make_row


class _Cursor:
    def __init__(self, cursor, *, lastrowid: int | None = None) -> None:
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class _PostgresConnectionFacade:
    """Small SQL-dialect bridge for the store-neutral Memory transaction logic."""

    _JSON_EXTRACT = re.compile(
        r"json_extract\((?P<column>[a-zA-Z0-9_.]+), '\$\.(?P<field>[a-zA-Z0-9_]+)'\)"
    )

    def __init__(self, connection) -> None:
        self._connection = connection
        self._transaction = None
        self._scope_locked = False

    def __enter__(self):
        self._transaction = self._connection.transaction()
        self._transaction.__enter__()
        self._scope_locked = False
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            return self._transaction.__exit__(exc_type, exc, traceback)
        finally:
            self._transaction = None
            self._scope_locked = False

    def execute(self, statement: str, parameters=()):
        parameters = tuple(parameters)
        if self._transaction is not None and not self._scope_locked:
            key = next(
                (
                    value
                    for value in parameters
                    if isinstance(value, str) and value.startswith('{"space_id"')
                ),
                None,
            )
            if key is not None:
                self.lock_scope(key)
        translated = self._translate(statement)
        is_audit_insert = translated.lstrip().upper().startswith("INSERT INTO AF_MEMORY_AUDIT")
        if is_audit_insert:
            translated += " RETURNING sequence"
        cursor = self._connection.execute(translated, parameters)
        lastrowid = None
        if is_audit_insert:
            row = cursor.fetchone()
            lastrowid = int(row[0])
        return _Cursor(cursor, lastrowid=lastrowid)

    def lock_scope(self, key: str) -> None:
        if self._scope_locked:
            return
        self._connection.execute(
            "INSERT INTO af_memory_scope_state(scope_key) VALUES (%s) "
            "ON CONFLICT (scope_key) DO NOTHING",
            (key,),
        )
        self._connection.execute(
            "SELECT scope_key FROM af_memory_scope_state WHERE scope_key=%s FOR UPDATE",
            (key,),
        ).fetchone()
        self._scope_locked = True

    def close(self) -> None:
        self._connection.close()

    @classmethod
    def _translate(cls, statement: str) -> str:
        translated = cls._JSON_EXTRACT.sub(
            lambda match: f"({match.group('column')}::jsonb->>'{match.group('field')}')",
            statement,
        )
        return translated.replace("?", "%s")


class PostgresMemoryAuthority(RelationalMemoryAuthority):
    """Full Memory authority over PostgreSQL.

    Memory lifecycle and maintenance semantics use the same relational
    transaction implementation as SQLite. This adapter supplies PostgreSQL transactions,
    a PostgreSQL schema, row locking, and SQL dialect translation. The host
    still owns the physical connection and no other domain can access it.
    """

    def __init__(self, connection) -> None:
        connection.autocommit = True
        connection.row_factory = _hybrid_row_factory
        self._connection = _PostgresConnectionFacade(connection)
        self._lock = RLock()
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_memory_scope_state (
                        scope_key TEXT PRIMARY KEY
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_memory_revisions (
                        scope_key TEXT NOT NULL,
                        memory_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(scope_key, memory_id, revision)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_memory_operations (
                        operation_id TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        command_digest TEXT NOT NULL,
                        action TEXT NOT NULL,
                        result_kind TEXT NOT NULL,
                        result_payload TEXT NOT NULL,
                        resource_ids TEXT NOT NULL,
                        committed_at TIMESTAMPTZ NOT NULL,
                        authority_sequence BIGINT NOT NULL,
                        PRIMARY KEY(scope_key, operation_id)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_memory_feedback_events (
                        scope_key TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        memory_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(scope_key, event_id, revision)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_memory_maintenance_plans (
                        scope_key TEXT NOT NULL,
                        plan_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(scope_key, plan_id)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_memory_dreaming_windows (
                        scope_key TEXT NOT NULL,
                        window_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(scope_key, window_id)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS af_memory_audit (
                        sequence BIGSERIAL PRIMARY KEY,
                        scope_key TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        resource_ids TEXT NOT NULL,
                        occurred_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )

    def _existing(self, scope, operation_id: str, digest: str, kind: str, model):
        self._connection.lock_scope(scope_key(scope))
        return super()._existing(scope, operation_id, digest, kind, model)
