from __future__ import annotations

import sqlite3

from agent_foundation.adapters.outbound.relational_memory_v2 import (
    RelationalMemoryAuthority,
)


class SqliteMemoryAuthority(RelationalMemoryAuthority):
    """SQLite binding for the shared relational Memory authority behavior."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        super().__init__(connection)
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS af_memory_revisions (
                    scope_key TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, memory_id, revision)
                );
                CREATE TABLE IF NOT EXISTS af_memory_operations (
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
                CREATE TABLE IF NOT EXISTS af_memory_feedback_events (
                    scope_key TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    memory_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, event_id, revision)
                );
                CREATE TABLE IF NOT EXISTS af_memory_maintenance_plans (
                    scope_key TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, plan_id)
                );
                CREATE TABLE IF NOT EXISTS af_memory_dreaming_windows (
                    scope_key TEXT NOT NULL,
                    window_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(scope_key, window_id)
                );
                CREATE TABLE IF NOT EXISTS af_memory_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource_ids TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )
