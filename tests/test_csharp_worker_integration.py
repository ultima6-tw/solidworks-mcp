import asyncio
import unittest
import uuid
import json
import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from mcp.types import CallToolRequest, CallToolRequestParams

from solidworks.csharp_execution import CSharpWorkerClient, PROTOCOL_VERSION, default_worker_path


@unittest.skipUnless(default_worker_path().is_file(), "C# worker has not been published")
class PublishedWorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = CSharpWorkerClient()

    def compile(self, code):
        execution_id = str(uuid.uuid4())
        return self.client.run(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "operation": "compile",
                "executionId": execution_id,
                "code": code,
                "timeoutMs": 30000,
            },
            30000,
        ).response

    def test_brief_select_by_id_example_compiles_and_extracts_symbols(self):
        response = self.compile(
            'result = model.Extension.SelectByID2("Top Plane", "PLANE", '
            '0, 0, 0, false, 0, null, 0);'
        )
        self.assertTrue(response["compiled"])
        signatures = [symbol["signature"] for symbol in response["interopSymbols"]]
        self.assertTrue(any("SelectByID2" in signature for signature in signatures))

    def test_diagnostic_maps_to_snippet_line(self):
        response = self.compile("result =\nmodel.NoSuchMember();")
        self.assertFalse(response["compiled"])
        diagnostic = next(item for item in response["diagnostics"] if item["severity"] == "error")
        self.assertEqual(diagnostic["line"], 2)
        self.assertEqual(diagnostic["source"], "snippet")

    def test_failed_overload_is_labeled_as_candidate_usage(self):
        response = self.compile("result = featureManager.FeatureExtrusion3();")
        self.assertFalse(response["compiled"])
        candidates = [
            symbol
            for symbol in response["interopSymbols"]
            if symbol.get("member") == "FeatureExtrusion3"
        ]
        self.assertTrue(candidates)
        self.assertTrue(all(item["resolution"] == "candidate" for item in candidates))
        self.assertTrue(all(item["occurrenceCount"] >= 1 for item in candidates))

    def test_worker_uses_installed_interops_and_release_zip_has_expected_layout(self):
        publish = default_worker_path().parent
        self.assertFalse((publish / "SolidWorks.Interop.sldworks.dll").exists())
        self.assertFalse((publish / "SolidWorks.Interop.swconst.dll").exists())
        package = (
            Path(__file__).resolve().parent.parent
            / "dist"
            / "solidworks-csharp-worker-win-x64.zip"
        )
        if not package.is_file():
            self.skipTest("release ZIP was not produced by the build script")
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
        self.assertIn("SolidWorks.CSharpWorker.exe", names)
        self.assertNotIn("SolidWorks.Interop.sldworks.dll", names)
        self.assertNotIn("SolidWorks.Interop.swconst.dll", names)

    def test_registered_mcp_handler_compiles_offline_and_records_history(self):
        from server import SolidWorksMCPServer

        with tempfile.TemporaryDirectory() as directory:
            history_path = os.path.join(directory, "history.sqlite3")
            with patch.dict(os.environ, {"SWMCP_SNIPPET_DB": history_path}):
                server = SolidWorksMCPServer()
                server.connection.connect = Mock(
                    side_effect=AssertionError("offline compilation must not connect")
                )
                handler = server.server.request_handlers[CallToolRequest]
                request = CallToolRequest(
                    params=CallToolRequestParams(
                        name="solidworks_compile_csharp",
                        arguments={"code": "result = 7;"},
                    )
                )
                with self.assertLogs("server", level="INFO") as captured:
                    result = asyncio.run(handler(request))
                response = json.loads(result.root.content[0].text)
            self.assertTrue(response["compiled"])
            self.assertTrue(response["historyRecorded"])
            self.assertNotIn("result = 7;", "\n".join(captured.output))
            server.connection.connect.assert_not_called()
            self.assertFalse(server._requires_connection("solidworks_compile_csharp", {}))


if __name__ == "__main__":
    unittest.main()
