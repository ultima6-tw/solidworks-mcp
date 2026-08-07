import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from solidworks.csharp_history import SnippetHistory, default_history_path


class SnippetHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "history.sqlite3"
        self.history = SnippetHistory(self.db_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_attempt_retains_source_and_pseudonymizes_document(self):
        identity = r"C:\secret\customer-part.SLDPRT"
        self.history.start_attempt(
            execution_id="exec-1",
            operation="compile",
            code="result = model.GetTitle();",
            options={"timeoutMs": 30000},
            protocol_version=1,
            server_version="0.3.0",
            document_type="part",
            document_identity=identity,
            solidworks_version="33.0",
        )
        row = self.history.get_attempt("exec-1")
        self.assertEqual(row["code"], "result = model.GetTitle();")
        self.assertEqual(row["status"], "started")
        self.assertEqual(row["document_type"], "part")
        self.assertNotEqual(row["document_id"], identity)
        self.assertNotIn("customer-part", row["document_id"])

        with closing(sqlite3.connect(self.db_path)) as db:
            metadata = dict(db.execute("SELECT key, value FROM metadata"))
        self.assertEqual(metadata["schema_version"], "2")
        self.assertNotIn(identity, metadata.values())

    def test_finish_serializes_structured_fields(self):
        self.history.start_attempt(
            execution_id="exec-2",
            operation="execute",
            code="result = 42;",
            options={"rebuild": True},
            protocol_version=1,
            server_version="0.3.0",
        )
        self.history.finish_attempt(
            "exec-2",
            status="completed",
            compiled=True,
            executed=True,
            timed_out=False,
            duration_ms=12,
            diagnostics=[],
            interop_symbols=[{"type": "IModelDoc2", "member": "GetTitle"}],
            result=42,
            state_changes={"featuresAdded": []},
            model_state="known",
        )
        row = self.history.get_attempt("exec-2")
        self.assertEqual(row["compiled"], 1)
        self.assertEqual(row["executed"], 1)
        self.assertEqual(json.loads(row["result_json"]), 42)
        self.assertEqual(
            json.loads(row["interop_symbols_json"])[0]["type"], "IModelDoc2"
        )

    def test_started_attempt_survives_without_finalize(self):
        self.history.start_attempt(
            execution_id="exec-3",
            operation="execute",
            code="throw new Exception();",
            options={},
            protocol_version=1,
            server_version="0.3.0",
        )
        reopened = SnippetHistory(self.db_path).get_attempt("exec-3")
        self.assertEqual(reopened["status"], "started")
        self.assertIsNone(reopened["finished_at_utc"])

    def test_default_path_honors_override(self):
        expected = Path(self.tempdir.name) / "override.sqlite3"
        with patch.dict(os.environ, {"SWMCP_SNIPPET_DB": str(expected)}):
            self.assertEqual(default_history_path(), expected)

    def test_concurrent_first_use_shares_one_pseudonymization_salt(self):
        identities = [r"C:\parts\one.SLDPRT", r"C:\parts\two.SLDPRT"]

        def write(index):
            SnippetHistory(self.db_path).start_attempt(
                execution_id=f"concurrent-{index}",
                operation="compile",
                code="result = 1;",
                options={},
                protocol_version=1,
                server_version="test",
                document_identity=identities[index],
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(write, range(2)))
        with closing(sqlite3.connect(self.db_path)) as db:
            salt_count = db.execute(
                "SELECT COUNT(*) FROM metadata WHERE key='document_hmac_salt'"
            ).fetchone()[0]
            attempt_count = db.execute("SELECT COUNT(*) FROM snippet_attempts").fetchone()[0]
        self.assertEqual(salt_count, 1)
        self.assertEqual(attempt_count, 2)

    def test_migrates_v1_database(self):
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript(
                """
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata VALUES('schema_version', '1');
                CREATE TABLE snippet_attempts(
                    execution_id TEXT PRIMARY KEY,
                    started_at_utc TEXT NOT NULL,
                    finished_at_utc TEXT,
                    operation TEXT NOT NULL,
                    code TEXT NOT NULL,
                    code_sha256 TEXT NOT NULL,
                    code_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    compiled INTEGER, executed INTEGER, timed_out INTEGER,
                    duration_ms INTEGER, diagnostics_json TEXT, exception_json TEXT,
                    interop_symbols_json TEXT, result_json TEXT, options_json TEXT,
                    state_changes_json TEXT, rebuild_json TEXT, model_state TEXT,
                    document_type TEXT, document_id TEXT, protocol_version INTEGER,
                    server_version TEXT, worker_version TEXT, solidworks_version TEXT
                );
                """
            )
            db.commit()
        history = SnippetHistory(self.db_path)
        history.start_attempt(
            execution_id="migrated",
            operation="compile",
            code="",
            options={},
            protocol_version=1,
            server_version="test",
        )
        history.finish_attempt("migrated", status="compiled", captured_output="ok")
        self.assertEqual(history.get_attempt("migrated")["captured_output"], "ok")


if __name__ == "__main__":
    unittest.main()
