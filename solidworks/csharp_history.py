"""Durable, local-only history for agent-generated C# snippets.

The history database intentionally lives under ``%LOCALAPPDATA%`` rather than
inside the source tree.  Submitted source is useful for deciding which
long-tail SolidWorks API sequences deserve a semantic MCP tool, but it may also
contain document names or literal paths.  Nothing in this module uploads or
logs snippet source.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = "2"
_JSON_COLUMNS = {
    "diagnostics": "diagnostics_json",
    "exception": "exception_json",
    "interop_symbols": "interop_symbols_json",
    "result": "result_json",
    "state_changes": "state_changes_json",
    "rebuild": "rebuild_json",
}
_UPDATE_COLUMNS = {
    "status",
    "compiled",
    "executed",
    "timed_out",
    "duration_ms",
    "model_state",
    "worker_version",
    "captured_output",
    "solidworks_version",
    *_JSON_COLUMNS.keys(),
}


def default_history_path() -> Path:
    """Return the local per-user snippet database path.

    ``SWMCP_SNIPPET_DB`` is primarily useful for tests, farm nodes, and users
    who want the corpus on an encrypted or separately backed-up volume.
    """
    override = os.environ.get("SWMCP_SNIPPET_DB")
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "SolidWorksMCP" / "snippet_history.sqlite3"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class SnippetHistory:
    """Append/finalize access to the versioned SQLite history database."""

    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path is not None else default_history_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema(db)
        return db

    @contextmanager
    def _database(self):
        """Yield a transaction and always release Windows file handles."""
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snippet_attempts (
                execution_id TEXT PRIMARY KEY,
                started_at_utc TEXT NOT NULL,
                finished_at_utc TEXT,
                operation TEXT NOT NULL CHECK(operation IN ('compile', 'execute')),
                code TEXT NOT NULL,
                code_sha256 TEXT NOT NULL,
                code_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                compiled INTEGER,
                executed INTEGER,
                timed_out INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER,
                diagnostics_json TEXT,
                exception_json TEXT,
                interop_symbols_json TEXT,
                result_json TEXT,
                options_json TEXT NOT NULL,
                state_changes_json TEXT,
                rebuild_json TEXT,
                model_state TEXT,
                document_type TEXT,
                document_id TEXT,
                protocol_version INTEGER NOT NULL,
                server_version TEXT,
                worker_version TEXT,
                captured_output TEXT,
                solidworks_version TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_snippet_attempts_started
                ON snippet_attempts(started_at_utc);
            CREATE INDEX IF NOT EXISTS idx_snippet_attempts_hash
                ON snippet_attempts(code_sha256);
            CREATE INDEX IF NOT EXISTS idx_snippet_attempts_status
                ON snippet_attempts(status);
            """
        )
        current = db.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        if current is not None and current[0] == "1":
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(snippet_attempts)")
            }
            if "captured_output" not in columns:
                db.execute(
                    "ALTER TABLE snippet_attempts ADD COLUMN captured_output TEXT"
                )
            db.execute(
                "UPDATE metadata SET value=? WHERE key='schema_version'",
                (SCHEMA_VERSION,),
            )
            current = (SCHEMA_VERSION,)
        if current is not None and current[0] != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported snippet-history schema {current[0]}; "
                f"server supports {SCHEMA_VERSION}"
            )
        db.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        db.commit()

    @staticmethod
    def _salt(db: sqlite3.Connection) -> bytes:
        candidate = secrets.token_hex(32)
        db.execute(
            "INSERT OR IGNORE INTO metadata(key, value) "
            "VALUES('document_hmac_salt', ?)",
            (candidate,),
        )
        row = db.execute(
            "SELECT value FROM metadata WHERE key='document_hmac_salt'"
        ).fetchone()
        if row is None:
            raise RuntimeError("Could not initialize document pseudonymization salt")
        return bytes.fromhex(row[0])

    def start_attempt(
        self,
        *,
        execution_id: str,
        operation: str,
        code: str,
        options: dict,
        protocol_version: int,
        server_version: str,
        document_type: Optional[str] = None,
        document_identity: Optional[str] = None,
        solidworks_version: Optional[str] = None,
    ) -> None:
        if operation not in {"compile", "execute"}:
            raise ValueError(f"Unsupported history operation: {operation}")
        encoded = code.encode("utf-8")
        with self._database() as db:
            document_id = None
            if document_identity:
                document_id = hmac.new(
                    self._salt(db), document_identity.encode("utf-8"), hashlib.sha256
                ).hexdigest()
            db.execute(
                """
                INSERT INTO snippet_attempts(
                    execution_id, started_at_utc, operation, code, code_sha256,
                    code_bytes, status, options_json, document_type, document_id,
                    protocol_version, server_version, solidworks_version
                ) VALUES (?, ?, ?, ?, ?, ?, 'started', ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    _utc_now(),
                    operation,
                    code,
                    hashlib.sha256(encoded).hexdigest(),
                    len(encoded),
                    _json(options),
                    document_type,
                    document_id,
                    protocol_version,
                    server_version,
                    solidworks_version,
                ),
            )

    def finish_attempt(self, execution_id: str, **values: Any) -> None:
        unknown = set(values) - _UPDATE_COLUMNS
        if unknown:
            raise ValueError(f"Unsupported history fields: {sorted(unknown)}")
        assignments = ["finished_at_utc = ?"]
        params: list[Any] = [_utc_now()]
        for key, value in values.items():
            column = _JSON_COLUMNS.get(key, key)
            assignments.append(f"{column} = ?")
            if key in _JSON_COLUMNS:
                value = _json(value)
            elif key in {"compiled", "executed", "timed_out"} and value is not None:
                value = int(bool(value))
            params.append(value)
        params.append(execution_id)
        with self._database() as db:
            cursor = db.execute(
                f"UPDATE snippet_attempts SET {', '.join(assignments)} "
                "WHERE execution_id = ?",
                params,
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown snippet execution ID: {execution_id}")

    def get_attempt(self, execution_id: str) -> Optional[dict]:
        """Read one attempt; primarily used by tests and offline tooling."""
        with self._database() as db:
            row = db.execute(
                "SELECT * FROM snippet_attempts WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return dict(row) if row is not None else None
