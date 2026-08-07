import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solidworks.csharp_execution import (
    CSharpExecutionTools,
    CSharpWorkerClient,
    MAX_WORKER_STDERR_BYTES,
    MAX_WORKER_STDOUT_BYTES,
    PROTOCOL_VERSION,
    WorkerError,
    diff_snapshots,
)
from solidworks.csharp_history import SnippetHistory
from solidworks.state_tracker import StateTracker
from server import _arguments_for_log


def _fake_worker_script(response_expression: str) -> str:
    return (
        "import json,sys; "
        "request=json.load(sys.stdin); "
        f"response={response_expression}; "
        "json.dump(response,sys.stdout)"
    )


class _DisconnectedConnection:
    def is_alive(self):
        return False


class _App:
    def GetProcessID(self):
        return 4321


class _FakeDoc:
    def GetPathName(self):
        return r"C:\parts\target.SLDPRT"

    def GetTitle(self):
        return "target.SLDPRT"

    def GetType(self):
        return 1


class _Connection:
    def __init__(self, doc=None):
        self.app = _App()
        self.doc = doc
        self.alive = True

    def ensure_connection(self):
        return None

    def is_alive(self):
        return self.alive

    def get_active_doc(self):
        return self.doc

    def get_sw_version(self):
        return "33.0"


class _ResponseWorker:
    def __init__(self, response, on_run=None):
        self.response = response
        self.on_run = on_run
        self.request = None

    def run(self, request, timeout_ms):
        self.request = request
        if self.on_run:
            self.on_run()
        response = dict(self.response)
        response.update(
            {
                "protocolVersion": 1,
                "executionId": request["executionId"],
                "diagnostics": response.get("diagnostics", []),
                "interopSymbols": response.get("interopSymbols", []),
            }
        )
        from solidworks.csharp_execution import WorkerOutcome

        return WorkerOutcome(response=response, duration_ms=1)


class _TimeoutWorker:
    def run(self, request, timeout_ms):
        from solidworks.csharp_execution import WorkerOutcome

        return WorkerOutcome(response=None, duration_ms=timeout_ms, timed_out=True)


class WorkerClientTests(unittest.TestCase):
    def test_valid_response(self):
        expression = (
            "{'protocolVersion':1,'executionId':request['executionId'],"
            "'compiled':True,'executed':False,'diagnostics':[],"
            "'interopSymbols':[],'durationMs':2}"
        )
        client = CSharpWorkerClient(
            command=[sys.executable, "-c", _fake_worker_script(expression)]
        )
        outcome = client.run(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "operation": "compile",
                "executionId": "abc",
                "code": "result = 1;",
            },
            5_000,
        )
        self.assertFalse(outcome.timed_out)
        self.assertTrue(outcome.response["compiled"])

    def test_timeout_kills_worker(self):
        client = CSharpWorkerClient(
            command=[sys.executable, "-c", "import time; time.sleep(5)"]
        )
        outcome = client.run(
            {"protocolVersion": 1, "executionId": "slow"}, 50
        )
        self.assertTrue(outcome.timed_out)

    def test_rejects_mismatched_protocol(self):
        expression = "{'protocolVersion':99,'executionId':request['executionId']}"
        client = CSharpWorkerClient(
            command=[sys.executable, "-c", _fake_worker_script(expression)]
        )
        with self.assertRaisesRegex(WorkerError, "not supported"):
            client.run({"protocolVersion": 1, "executionId": "abc"}, 5_000)

    def test_rejects_mismatched_execution_id(self):
        expression = "{'protocolVersion':1,'executionId':'other','compiled':False,'executed':False,'diagnostics':[],'interopSymbols':[]}"
        client = CSharpWorkerClient(
            command=[sys.executable, "-c", _fake_worker_script(expression)]
        )
        with self.assertRaisesRegex(WorkerError, "different execution ID"):
            client.run({"protocolVersion": 1, "executionId": "abc"}, 5_000)

    def test_rejects_malformed_json_and_non_boolean_status(self):
        malformed = CSharpWorkerClient(
            command=[sys.executable, "-c", "print('not-json')"]
        )
        with self.assertRaisesRegex(WorkerError, "valid JSON"):
            malformed.run({"protocolVersion": 1, "executionId": "abc"}, 5_000)

        expression = (
            "{'protocolVersion':1,'executionId':request['executionId'],"
            "'compiled':'false','executed':False,'diagnostics':[],'interopSymbols':[]}"
        )
        wrong_type = CSharpWorkerClient(
            command=[sys.executable, "-c", _fake_worker_script(expression)]
        )
        with self.assertRaisesRegex(WorkerError, "must be a boolean"):
            wrong_type.run({"protocolVersion": 1, "executionId": "abc"}, 5_000)

    def test_nonzero_exit_and_output_limit(self):
        failed = CSharpWorkerClient(
            command=[sys.executable, "-c", "import sys; print('failed',file=sys.stderr); sys.exit(7)"]
        )
        with self.assertRaisesRegex(WorkerError, "code 7"):
            failed.run({"protocolVersion": 1, "executionId": "abc"}, 5_000)

        oversized = CSharpWorkerClient(
            command=[
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write('x'*{MAX_WORKER_STDOUT_BYTES + 1})",
            ]
        )
        with self.assertRaisesRegex(WorkerError, "exceeded"):
            oversized.run({"protocolVersion": 1, "executionId": "abc"}, 5_000)

        oversized_error = CSharpWorkerClient(
            command=[
                sys.executable,
                "-c",
                f"import sys; sys.stderr.write('x'*{MAX_WORKER_STDERR_BYTES + 1})",
            ]
        )
        with self.assertRaisesRegex(WorkerError, "stderr exceeded"):
            oversized_error.run({"protocolVersion": 1, "executionId": "abc"}, 5_000)


class ServerRegistrationTests(unittest.TestCase):
    def test_all_97_tool_names_are_registered_without_duplicates(self):
        from server import SolidWorksMCPServer

        server = SolidWorksMCPServer()
        module_names = [
            tool.name
            for module in server._modules
            for tool in module.get_tool_definitions()
        ]
        all_names = module_names + ["solidworks_batch"]
        self.assertEqual(len(all_names), 97)
        self.assertEqual(len(set(all_names)), 97)
        self.assertEqual(set(module_names), set(server._route_map))


class CSharpExecutionToolTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "history.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_compile_result_is_recorded(self):
        expression = (
            "{'protocolVersion':1,'executionId':request['executionId'],"
            "'compiled':True,'executed':False,'diagnostics':[],"
            "'interopSymbols':[{'type':'IModelDoc2','member':'GetTitle'}],"
            "'durationMs':4,'workerVersion':'test'}"
        )
        history = SnippetHistory(self.db_path)
        tools = CSharpExecutionTools(
            _DisconnectedConnection(),
            worker_client=CSharpWorkerClient(
                command=[sys.executable, "-c", _fake_worker_script(expression)]
            ),
            history=history,
        )
        result = json.loads(
            tools.execute(
                "solidworks_compile_csharp", {"code": "result = model.GetTitle();"}
            )
        )
        self.assertTrue(result["compiled"])
        self.assertTrue(result["historyRecorded"])
        row = history.get_attempt(result["executionId"])
        self.assertEqual(row["status"], "compiled")
        self.assertEqual(row["code"], "result = model.GetTitle();")

    def test_compile_failure_is_recorded(self):
        expression = (
            "{'protocolVersion':1,'executionId':request['executionId'],"
            "'compiled':False,'executed':False,"
            "'diagnostics':[{'severity':'error','code':'CS1002','message':'; expected'}],"
            "'interopSymbols':[],'durationMs':3}"
        )
        history = SnippetHistory(self.db_path)
        tools = CSharpExecutionTools(
            _DisconnectedConnection(),
            worker_client=CSharpWorkerClient(
                command=[sys.executable, "-c", _fake_worker_script(expression)]
            ),
            history=history,
        )
        result = json.loads(
            tools.execute("solidworks_compile_csharp", {"code": "result ="})
        )
        row = history.get_attempt(result["executionId"])
        self.assertEqual(row["status"], "compile_failed")
        self.assertEqual(json.loads(row["diagnostics_json"])[0]["code"], "CS1002")

    def test_validation_rejects_oversized_source(self):
        tools = CSharpExecutionTools(_DisconnectedConnection())
        with self.assertRaisesRegex(ValueError, "maximum"):
            tools.execute("solidworks_compile_csharp", {"code": "x" * 70000})

    def test_log_redaction_covers_direct_and_batch_calls(self):
        source = "result = model.GetTitle();"
        direct = _arguments_for_log("solidworks_compile_csharp", {"code": source})
        self.assertNotIn(source, direct["code"])
        batch = _arguments_for_log(
            "solidworks_batch",
            {"calls": [{"tool": "solidworks_execute_csharp", "args": {"code": source}}]},
        )
        self.assertNotIn(source, batch["calls"][0]["args"]["code"])

    def test_tracker_reconciles_live_objects_without_activating_sketch(self):
        tracker = StateTracker()
        tracker.new_document("Part1", "part")
        sketch_id, created = tracker.reconcile_live_feature("Sketch1", "ProfileFeature")
        feature_id, feature_created = tracker.reconcile_live_feature(
            "Boss-Extrude1", "Extrusion"
        )
        self.assertTrue(created)
        self.assertTrue(feature_created)
        self.assertEqual(sketch_id, "sketch:Sketch1")
        self.assertEqual(feature_id, "feat:Boss-Extrude1")
        self.assertIsNone(tracker._scope().active_sketch)

    def test_feature_id_diff_detects_rename_and_preserves_tracker_metadata(self):
        before = {
            "features": [{"name": "Old Boss", "type": "Extrusion", "swFeatureId": 17}],
            "featureCount": 1,
        }
        after = {
            "features": [{"name": "Mounting Boss", "type": "Extrusion", "swFeatureId": 17}],
            "featureCount": 1,
        }
        changes = diff_snapshots(before, after)
        self.assertEqual(changes["featuresAdded"], [])
        self.assertEqual(changes["featuresRemoved"], [])
        self.assertEqual(changes["featuresRenamed"][0]["before"], "Old Boss")

        tracker = StateTracker()
        tracker.new_document("Part1", "part")
        sketch_id = tracker.register_sketch("Sketch1", "Front")
        tracker.close_sketch("Sketch1")
        tracker.register_feature(
            "Old Boss", "extrusion", source_sketch=sketch_id, parameters={"depth": 12}
        )
        tools = CSharpExecutionTools(_DisconnectedConnection(), tracker)
        reconciliation = tools._reconcile_tracker(changes, same_document=True)
        record = tracker.resolve_id("feat:Mounting Boss")
        self.assertTrue(reconciliation["applied"])
        self.assertEqual(record.parameters["depth"], 12)
        self.assertIsNotNone(tracker.resolve_id(sketch_id))

    def test_no_active_document_is_structured_and_recorded(self):
        connection = _Connection(doc=None)
        history = SnippetHistory(self.db_path)
        tools = CSharpExecutionTools(connection, history=history)
        result = json.loads(
            tools.execute("solidworks_execute_csharp", {"code": "result = 1;"})
        )
        self.assertEqual(result["exception"]["type"], "no_active_document")
        self.assertTrue(result["historyRecorded"])
        self.assertEqual(
            history.get_attempt(result["executionId"])["status"], "precondition_failed"
        )

    def test_execute_request_pins_process_and_document(self):
        connection = _Connection(doc=_FakeDoc())
        worker = _ResponseWorker(
            {
                "compiled": True,
                "executed": False,
                "exception": {"type": "target_mismatch", "message": "changed"},
                "result": None,
                "durationMs": 1,
            }
        )
        tools = CSharpExecutionTools(
            connection, worker_client=worker, history=SnippetHistory(self.db_path)
        )
        with patch("solidworks.csharp_execution.capture_live_snapshot", return_value={}):
            tools.execute("solidworks_execute_csharp", {"code": "result = 1;"})
        self.assertEqual(worker.request["expectedProcessId"], 4321)
        self.assertEqual(
            worker.request["expectedDocument"]["path"], r"C:\parts\target.SLDPRT"
        )

    def test_post_execution_connection_loss_is_structured_and_finalized(self):
        connection = _Connection(doc=_FakeDoc())
        worker = _ResponseWorker(
            {
                "compiled": True,
                "executed": True,
                "exception": None,
                "result": None,
                "durationMs": 1,
            },
            on_run=lambda: setattr(connection, "alive", False),
        )
        history = SnippetHistory(self.db_path)
        tools = CSharpExecutionTools(connection, worker_client=worker, history=history)
        with patch("solidworks.csharp_execution.capture_live_snapshot", return_value={}):
            result = json.loads(
                tools.execute("solidworks_execute_csharp", {"code": "result = 1;"})
            )
        self.assertEqual(result["modelState"], "unknown")
        self.assertIn("postProcessingProblems", result)
        self.assertEqual(
            history.get_attempt(result["executionId"])["status"], "completed_unverified"
        )

    def test_compile_failure_does_not_rebuild_or_reconcile(self):
        connection = _Connection(doc=_FakeDoc())
        worker = _ResponseWorker(
            {
                "compiled": False,
                "executed": False,
                "exception": None,
                "result": None,
                "durationMs": 1,
            }
        )
        tools = CSharpExecutionTools(
            connection, worker_client=worker, history=SnippetHistory(self.db_path)
        )
        with (
            patch("solidworks.csharp_execution.capture_live_snapshot", return_value={}),
            patch("solidworks.csharp_execution.verify_rebuild") as rebuild,
        ):
            result = json.loads(
                tools.execute("solidworks_execute_csharp", {"code": "result = ;"})
            )
        rebuild.assert_not_called()
        self.assertFalse(result["trackerReconciliation"]["applied"])

    def test_execute_timeout_is_recorded_as_unknown_state(self):
        connection = _Connection(doc=_FakeDoc())
        history = SnippetHistory(self.db_path)
        tools = CSharpExecutionTools(
            connection, worker_client=_TimeoutWorker(), history=history
        )
        with patch("solidworks.csharp_execution.capture_live_snapshot", return_value={}):
            result = json.loads(
                tools.execute(
                    "solidworks_execute_csharp",
                    {"code": "result = 1;", "timeoutMs": 1000},
                )
            )
        self.assertTrue(result["timedOut"])
        self.assertEqual(result["modelState"], "unknown")
        self.assertEqual(history.get_attempt(result["executionId"])["status"], "timed_out")

    def test_runtime_exception_rebuilds_partial_mutation(self):
        connection = _Connection(doc=_FakeDoc())
        worker = _ResponseWorker(
            {
                "compiled": True,
                "executed": True,
                "exception": {"type": "System.Exception", "message": "after mutation"},
                "result": None,
                "durationMs": 1,
            }
        )
        history = SnippetHistory(self.db_path)
        tools = CSharpExecutionTools(connection, worker_client=worker, history=history)
        snapshots = [
            {"features": [], "featureCount": 0},
            {
                "features": [
                    {"name": "Boss-Extrude1", "type": "Extrusion", "swFeatureId": 10}
                ],
                "featureCount": 1,
            },
        ]
        with (
            patch("solidworks.csharp_execution.capture_live_snapshot", side_effect=snapshots),
            patch("solidworks.csharp_execution.verify_rebuild", return_value=(True, [])) as rebuild,
        ):
            result = json.loads(
                tools.execute("solidworks_execute_csharp", {"code": "throw new Exception();"})
            )
        rebuild.assert_called_once()
        self.assertEqual(result["stateChanges"]["featuresAdded"][0]["name"], "Boss-Extrude1")
        self.assertEqual(history.get_attempt(result["executionId"])["status"], "runtime_failed")

    def test_inspection_can_be_disabled_without_claiming_reconciliation(self):
        connection = _Connection(doc=_FakeDoc())
        worker = _ResponseWorker(
            {
                "compiled": True,
                "executed": True,
                "exception": None,
                "result": 1,
                "durationMs": 1,
            }
        )
        tools = CSharpExecutionTools(
            connection, worker_client=worker, history=SnippetHistory(self.db_path)
        )
        with (
            patch("solidworks.csharp_execution.capture_live_snapshot", return_value={}),
            patch("solidworks.csharp_execution.verify_rebuild", return_value=(True, [])),
        ):
            result = json.loads(
                tools.execute(
                    "solidworks_execute_csharp",
                    {"code": "result = 1;", "inspectAfter": False},
                )
            )
        self.assertFalse(result["trackerReconciliation"]["applied"])
        self.assertEqual(
            result["trackerReconciliation"]["reason"],
            "post-execution inspection disabled",
        )


if __name__ == "__main__":
    unittest.main()
