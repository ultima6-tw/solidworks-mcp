import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.export_csharp_history import read_attempts, summarize, write_csv, write_jsonl
from solidworks.csharp_history import SnippetHistory


class HistoryExportTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "history.sqlite3"
        history = SnippetHistory(self.path)
        for index, status in enumerate(("completed", "compile_failed", "completed")):
            execution_id = f"attempt-{index}"
            history.start_attempt(
                execution_id=execution_id,
                operation="execute" if status == "completed" else "compile",
                code="result = model.GetTitle();" if index != 1 else "result = ;",
                options={},
                protocol_version=1,
                server_version="test",
            )
            history.finish_attempt(
                execution_id,
                status=status,
                compiled=status == "completed",
                executed=status == "completed",
                interop_symbols=[
                    {
                        "assembly": "SolidWorks.Interop.sldworks",
                        "type": "IModelDoc2",
                        "member": "GetTitle",
                        "signature": "IModelDoc2.GetTitle()",
                        "resolution": "resolved",
                        "occurrenceCount": 2,
                    }
                ] if status == "completed" else [],
            )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_summary_counts_outcomes_symbols_and_repeated_hashes(self):
        summary = summarize(read_attempts(self.path))
        self.assertEqual(summary["attempts"], 3)
        self.assertEqual(summary["byStatus"]["completed"], 2)
        self.assertEqual(summary["topInteropSymbols"][0]["attempts"], 2)
        self.assertEqual(summary["topInteropSymbols"][0]["occurrences"], 4)
        self.assertEqual(summary["topInteropSymbols"][0]["resolution"], "resolved")
        self.assertEqual(summary["repeatedHashes"][0]["attempts"], 2)

    def test_status_filter_and_jsonl_without_source(self):
        rows = read_attempts(self.path, statuses=["compile_failed"])
        output = io.StringIO()
        write_jsonl(rows, output, include_code=False)
        record = json.loads(output.getvalue())
        self.assertEqual(record["status"], "compile_failed")
        self.assertNotIn("code", record)

    def test_csv_export(self):
        output = io.StringIO()
        write_csv(read_attempts(self.path), output, include_code=True)
        self.assertIn("execution_id", output.getvalue().splitlines()[0])
        self.assertIn("result = model.GetTitle();", output.getvalue())

    def test_csv_hardens_spreadsheet_formula_cells(self):
        output = io.StringIO()
        write_csv([{"code": "=HYPERLINK(\"https://example.invalid\")"}], output, include_code=True)
        self.assertIn("'=HYPERLINK", output.getvalue())


if __name__ == "__main__":
    unittest.main()
