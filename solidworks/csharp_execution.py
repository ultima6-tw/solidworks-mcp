"""Roslyn-worker orchestration for trusted local C# snippets.

The Python MCP process never loads user assemblies and never passes its
late-bound ``pywin32`` proxies into .NET.  A fresh worker attaches to the same
running SolidWorks instance for each call, then exits.  This gives the caller a
process boundary that can be timed out without disturbing the existing MCP
server, although it cannot make an already-issued SolidWorks COM call
transactional or safely cancellable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
import uuid
import pythoncom
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Optional

from mcp.types import Tool

from .com_utils import com_prop, verify_rebuild
from .csharp_history import SnippetHistory


logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEFAULT_TIMEOUT_MS = 30_000
MIN_TIMEOUT_MS = 1_000
MAX_TIMEOUT_MS = 120_000
MAX_CODE_BYTES = 64 * 1024
MAX_WORKER_STDOUT_BYTES = 2 * 1024 * 1024
MAX_WORKER_STDERR_BYTES = 256 * 1024


def _server_version() -> str:
    try:
        return metadata.version("solidworks-mcp")
    except metadata.PackageNotFoundError:
        return "0.3.0+source"


def default_worker_path() -> Path:
    override = os.environ.get("SWMCP_CSHARP_WORKER")
    if override:
        return Path(override).expanduser()
    root = Path(__file__).resolve().parent.parent
    return root / "worker" / "publish" / "SolidWorks.CSharpWorker.exe"


class WorkerError(RuntimeError):
    def __init__(self, kind: str, message: str, *, process_started: bool = False):
        super().__init__(message)
        self.kind = kind
        self.process_started = process_started


@dataclass
class WorkerOutcome:
    response: Optional[dict]
    duration_ms: int
    timed_out: bool = False


class CSharpWorkerClient:
    """One-request subprocess client for the versioned worker protocol."""

    def __init__(
        self,
        worker_path: Optional[Path | str] = None,
        *,
        command: Optional[list[str]] = None,
    ):
        self.worker_path = Path(worker_path) if worker_path else default_worker_path()
        self._command = command

    @property
    def command(self) -> list[str]:
        return self._command or [str(self.worker_path)]

    def run(self, request: dict, timeout_ms: int) -> WorkerOutcome:
        if self._command is None and not self.worker_path.is_file():
            raise WorkerError(
                "worker_not_found",
                f"C# worker not found at {self.worker_path}. "
                "Build it with scripts/build_csharp_worker.ps1.",
            )

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=flags,
            )
        except OSError as exc:
            raise WorkerError("worker_start_failed", str(exc)) from exc

        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_exceeded = threading.Event()
        stderr_exceeded = threading.Event()

        def drain(stream, buffer: bytearray, limit: int, exceeded: threading.Event):
            try:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    remaining = limit - len(buffer)
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        exceeded.set()
                        try:
                            process.kill()
                        except OSError:
                            pass
            finally:
                stream.close()

        stdout_thread = threading.Thread(
            target=drain,
            args=(process.stdout, stdout_buffer, MAX_WORKER_STDOUT_BYTES, stdout_exceeded),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=drain,
            args=(process.stderr, stderr_buffer, MAX_WORKER_STDERR_BYTES, stderr_exceeded),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            process.wait(timeout=timeout_ms / 1000.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            return WorkerOutcome(
                response=None,
                duration_ms=round((time.monotonic() - started) * 1000),
                timed_out=True,
            )

        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        duration_ms = round((time.monotonic() - started) * 1000)
        stdout = stdout_buffer.decode("utf-8", errors="replace")
        stderr = stderr_buffer.decode("utf-8", errors="replace")
        if stdout_exceeded.is_set():
            raise WorkerError(
                "worker_output_limit",
                f"C# worker response exceeded {MAX_WORKER_STDOUT_BYTES} bytes",
                process_started=True,
            )
        if stderr_exceeded.is_set():
            raise WorkerError(
                "worker_output_limit",
                f"C# worker stderr exceeded {MAX_WORKER_STDERR_BYTES} bytes",
                process_started=True,
            )
        if process.returncode != 0:
            detail = (stderr or "").strip()[-4000:]
            message = f"C# worker exited with code {process.returncode}"
            if detail:
                message += f": {detail}"
            raise WorkerError("worker_failed", message, process_started=True)

        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise WorkerError(
                "invalid_worker_response",
                "C# worker did not return valid JSON",
                process_started=True,
            ) from exc
        if not isinstance(response, dict):
            raise WorkerError(
                "invalid_worker_response",
                "C# worker response must be a JSON object",
                process_started=True,
            )
        if response.get("protocolVersion") != PROTOCOL_VERSION:
            raise WorkerError(
                "protocol_mismatch",
                f"C# worker protocol {response.get('protocolVersion')} is not "
                f"supported by server protocol {PROTOCOL_VERSION}",
                process_started=True,
            )
        if response.get("executionId") != request.get("executionId"):
            raise WorkerError(
                "execution_id_mismatch",
                "C# worker returned a different execution ID",
                process_started=True,
            )
        for field in ("compiled", "executed"):
            if not isinstance(response.get(field), bool):
                raise WorkerError(
                    "invalid_worker_response",
                    f"C# worker field '{field}' must be a boolean",
                    process_started=True,
                )
        for field in ("diagnostics", "interopSymbols"):
            if not isinstance(response.get(field), list):
                raise WorkerError(
                    "invalid_worker_response",
                    f"C# worker field '{field}' must be an array",
                    process_started=True,
                )
        if response.get("exception") is not None and not isinstance(response["exception"], dict):
            raise WorkerError(
                "invalid_worker_response",
                "C# worker field 'exception' must be an object or null",
                process_started=True,
            )
        if isinstance(response.get("durationMs"), bool) or not isinstance(
            response.get("durationMs"), (int, float)
        ):
            raise WorkerError(
                "invalid_worker_response",
                "C# worker field 'durationMs' must be numeric",
                process_started=True,
            )
        if not isinstance(response.get("capturedOutput", ""), str) or not isinstance(
            response.get("capturedOutputTruncated", False), bool
        ):
            raise WorkerError(
                "invalid_worker_response",
                "C# worker captured-output fields have invalid types",
                process_started=True,
            )
        return WorkerOutcome(response=response, duration_ms=duration_ms)


def _safe_prop(obj, name, default=None):
    try:
        return com_prop(obj, name)
    except Exception:
        return default


def _doc_type(doc) -> str:
    value = _safe_prop(doc, "GetType")
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "unknown"
    return {1: "part", 2: "assembly", 3: "drawing"}.get(value, "unknown")


def _document_context(doc) -> tuple[str, Optional[str]]:
    if doc is None:
        return "none", None
    path = _safe_prop(doc, "GetPathName")
    if path:
        identity = f"path:{str(path).casefold()}"
    else:
        title = _safe_prop(doc, "GetTitle")
        identity = f"unsaved:{title}" if title else None
    return _doc_type(doc), identity


def _document_target(doc) -> Optional[dict]:
    if doc is None:
        return None
    path = str(_safe_prop(doc, "GetPathName") or "")
    title = str(_safe_prop(doc, "GetTitle") or "")
    return {"path": path, "title": title, "type": _doc_type(doc)}


def _same_com_object(left, right) -> bool:
    if left is None or right is None:
        return False
    try:
        left_unknown = left._oleobj_.QueryInterface(pythoncom.IID_IUnknown)
        right_unknown = right._oleobj_.QueryInterface(pythoncom.IID_IUnknown)
        return bool(left_unknown == right_unknown)
    except Exception:
        try:
            return bool(left == right)
        except Exception:
            return False


def capture_live_snapshot(doc) -> dict:
    """Best-effort live state used for before/after comparison.

    Document names and paths are deliberately omitted.  Individual feature
    names remain because they are necessary to explain and reconcile a model
    change.
    """
    snapshot: dict[str, Any] = {"documentType": _doc_type(doc)}
    problems: list[str] = []

    try:
        features = []
        for feature in doc.FeatureManager.GetFeatures(True) or []:
            name = _safe_prop(feature, "Name")
            type_name = _safe_prop(feature, "GetTypeName2")
            if name:
                item = {"name": str(name), "type": str(type_name or "unknown")}
                feature_id = _safe_prop(feature, "GetID")
                if isinstance(feature_id, int):
                    item["swFeatureId"] = feature_id
                features.append(item)
        snapshot["features"] = features
        snapshot["featureCount"] = len(features)
    except Exception as exc:
        problems.append(f"features: {exc}")

    if snapshot["documentType"] == "part":
        try:
            bodies = list(doc.GetBodies2(0, True) or [])
            body_data: dict[str, Any] = {"count": len(bodies)}
            if bodies:
                mins = [float("inf")] * 3
                maxs = [float("-inf")] * 3
                faces = 0
                edges = 0
                for body in bodies:
                    box = body.GetBodyBox()
                    if box:
                        for index in range(3):
                            mins[index] = min(mins[index], float(box[index]) * 1000.0)
                            maxs[index] = max(maxs[index], float(box[index + 3]) * 1000.0)
                    faces += len(body.GetFaces() or [])
                    edges += len(body.GetEdges() or [])
                body_data.update(
                    {
                        "boundingBox": {
                            "min": [round(value, 4) for value in mins],
                            "max": [round(value, 4) for value in maxs],
                            "size": [
                                round(maxs[index] - mins[index], 4)
                                for index in range(3)
                            ],
                        },
                        "faces": faces,
                        "edges": edges,
                    }
                )
            snapshot["body"] = body_data
        except Exception as exc:
            problems.append(f"bodies: {exc}")

        try:
            props = _safe_prop(doc, "GetMassProperties")
            if props and len(props) >= 6:
                snapshot["massProperties"] = {
                    "massKg": round(float(props[5]), 9),
                    "volumeMm3": round(float(props[3]) * 1e9, 4),
                    "surfaceAreaMm2": round(float(props[4]) * 1e6, 4),
                    "centerOfMassMm": [round(float(props[i]) * 1000.0, 5) for i in range(3)],
                }
        except Exception as exc:
            problems.append(f"mass properties: {exc}")
    elif snapshot["documentType"] == "assembly":
        try:
            snapshot["componentCount"] = len(doc.GetComponents(True) or [])
        except Exception as exc:
            problems.append(f"components: {exc}")

    dirty = _safe_prop(doc, "GetSaveFlag")
    if dirty is not None:
        snapshot["dirty"] = bool(dirty)
    if problems:
        snapshot["inspectionProblems"] = problems
    return snapshot


def diff_snapshots(before: Optional[dict], after: Optional[dict]) -> dict:
    if before is None or after is None:
        return {"available": False}
    changes: dict[str, Any] = {"available": True}
    def feature_key(feature):
        feature_id = feature.get("swFeatureId")
        return ("id", feature_id) if feature_id is not None else ("name", feature["name"])

    before_features = {feature_key(f): f for f in before.get("features", [])}
    after_features = {feature_key(f): f for f in after.get("features", [])}
    changes["featuresAdded"] = [
        after_features[key]
        for key in sorted(after_features.keys() - before_features.keys(), key=str)
    ]
    changes["featuresRemoved"] = [
        before_features[key]
        for key in sorted(before_features.keys() - after_features.keys(), key=str)
    ]
    changes["featuresRenamed"] = [
        {
            "swFeatureId": key[1] if key[0] == "id" else None,
            "type": after_features[key].get("type"),
            "before": before_features[key]["name"],
            "after": after_features[key]["name"],
        }
        for key in sorted(before_features.keys() & after_features.keys(), key=str)
        if before_features[key]["name"] != after_features[key]["name"]
    ]

    scalar_paths = {
        "featureCount": (before.get("featureCount"), after.get("featureCount")),
        "bodyCount": (
            (before.get("body") or {}).get("count"),
            (after.get("body") or {}).get("count"),
        ),
        "faceCount": (
            (before.get("body") or {}).get("faces"),
            (after.get("body") or {}).get("faces"),
        ),
        "edgeCount": (
            (before.get("body") or {}).get("edges"),
            (after.get("body") or {}).get("edges"),
        ),
        "massKg": (
            (before.get("massProperties") or {}).get("massKg"),
            (after.get("massProperties") or {}).get("massKg"),
        ),
        "volumeMm3": (
            (before.get("massProperties") or {}).get("volumeMm3"),
            (after.get("massProperties") or {}).get("volumeMm3"),
        ),
    }
    measurements = {}
    for name, (old, new) in scalar_paths.items():
        if old is not None and new is not None and old != new:
            item = {"before": old, "after": new}
            if isinstance(old, (int, float)) and isinstance(new, (int, float)):
                item["delta"] = round(new - old, 9)
            measurements[name] = item
    if measurements:
        changes["measurements"] = measurements
    changes["changed"] = bool(
        changes["featuresAdded"]
        or changes["featuresRemoved"]
        or changes["featuresRenamed"]
        or measurements
    )
    return changes


class CSharpExecutionTools:
    """MCP tool definitions and execution lifecycle for C# snippets."""

    def __init__(
        self,
        connection,
        tracker=None,
        *,
        worker_client: Optional[CSharpWorkerClient] = None,
        history: Optional[SnippetHistory] = None,
    ):
        self.connection = connection
        self.tracker = tracker
        self.worker = worker_client or CSharpWorkerClient()
        self.history = history or SnippetHistory()

    def get_tool_definitions(self) -> list[Tool]:
        retention = (
            "Exact submitted source, returned result, captured console output, and "
            "outcomes are retained in a local snippet-history SQLite database for "
            "later API/tool analysis; these fields can contain sensitive literal "
            "values. Nothing is uploaded automatically."
        )
        return [
            Tool(
                name="solidworks_compile_csharp",
                description=(
                    "Compile a small trusted-local C# snippet against the installed "
                    "SolidWorks interop assemblies without executing it. The snippet "
                    "receives swApp, model, extension, selection, featureManager, and "
                    "sketchManager when later executed. Returns precise Roslyn "
                    f"diagnostics plus labeled resolved/candidate SolidWorks API symbols. {retention}"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "C# statements, not a complete program"},
                        "timeoutMs": {
                            "type": "integer",
                            "minimum": MIN_TIMEOUT_MS,
                            "maximum": MAX_TIMEOUT_MS,
                            "description": "Worker timeout in milliseconds (default 30000)",
                        },
                    },
                    "required": ["code"],
                },
            ),
            Tool(
                name="solidworks_execute_csharp",
                description=(
                    "Compile and execute a small trusted-local C# snippet against the "
                    "active SolidWorks document in a fresh worker process. This is NOT "
                    "a security sandbox and has no transactional rollback. Rebuild and "
                    "state inspection are enabled by default. A timeout means model "
                    f"state is unknown and must be inspected before another mutation. {retention}"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "C# statements, not a complete program"},
                        "timeoutMs": {
                            "type": "integer",
                            "minimum": MIN_TIMEOUT_MS,
                            "maximum": MAX_TIMEOUT_MS,
                            "description": "Worker timeout in milliseconds (default 30000)",
                        },
                        "rebuild": {"type": "boolean", "description": "Rebuild and verify afterward (default true)"},
                        "inspectAfter": {"type": "boolean", "description": "Capture and diff live state afterward (default true)"},
                    },
                    "required": ["code"],
                },
            ),
        ]

    def execute(self, tool_name: str, args: dict) -> str:
        if tool_name == "solidworks_compile_csharp":
            return self._run("compile", args or {})
        if tool_name == "solidworks_execute_csharp":
            return self._run("execute", args or {})
        raise Exception(f"Unknown C# execution tool: {tool_name}")

    @staticmethod
    def _validated_options(operation: str, args: dict) -> tuple[str, dict]:
        code = args.get("code")
        if not isinstance(code, str):
            raise ValueError("code must be a string")
        code_bytes = len(code.encode("utf-8"))
        if code_bytes > MAX_CODE_BYTES:
            raise ValueError(
                f"code is {code_bytes} bytes; maximum is {MAX_CODE_BYTES} bytes"
            )
        timeout = args.get("timeoutMs", DEFAULT_TIMEOUT_MS)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ValueError("timeoutMs must be an integer")
        if not MIN_TIMEOUT_MS <= timeout <= MAX_TIMEOUT_MS:
            raise ValueError(
                f"timeoutMs must be between {MIN_TIMEOUT_MS} and {MAX_TIMEOUT_MS}"
            )
        options = {"timeoutMs": timeout}
        if operation == "execute":
            for key, default in (("rebuild", True), ("inspectAfter", True)):
                value = args.get(key, default)
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be a boolean")
                options[key] = value
        return code, options

    def _live_context(self, *, connect: bool) -> tuple[Any, str, Optional[str], str, int, Optional[dict]]:
        if connect:
            self.connection.ensure_connection()
        elif not self.connection.is_alive():
            return None, "none", None, "unknown", 0, None
        doc = self.connection.get_active_doc()
        doc_type, identity = _document_context(doc)
        version = self.connection.get_sw_version() if self.connection.is_alive() else "unknown"
        process_id = _safe_prop(self.connection.app, "GetProcessID", 0)
        try:
            process_id = int(process_id)
        except (TypeError, ValueError):
            process_id = 0
        return doc, doc_type, identity, version, process_id, _document_target(doc)

    def _finish_history(self, execution_id: str, response: dict, status: str) -> bool:
        try:
            captured_output = response.get("capturedOutput")
            if captured_output and response.get("capturedOutputTruncated"):
                captured_output += "\n...[captured output truncated]"
            self.history.finish_attempt(
                execution_id,
                status=status,
                compiled=response.get("compiled"),
                executed=response.get("executed"),
                timed_out=response.get("timedOut", False),
                duration_ms=response.get("durationMs"),
                diagnostics=response.get("diagnostics"),
                exception=response.get("exception"),
                interop_symbols=response.get("interopSymbols"),
                result=response.get("result"),
                state_changes=response.get("stateChanges"),
                rebuild=response.get("rebuild"),
                model_state=response.get("modelState"),
                worker_version=response.get("workerVersion"),
                captured_output=captured_output,
                solidworks_version=response.get("solidworksVersion"),
            )
            return True
        except Exception as exc:
            logger.warning("Could not finalize C# snippet history for %s: %s", execution_id, exc)
            return False

    def _reconcile_tracker(self, changes: dict, *, same_document: bool) -> dict:
        if not self.tracker:
            return {"applied": False, "reason": "no tracker"}
        if not same_document:
            return {"applied": False, "reason": "active document changed"}
        added_ids = []
        removed_ids = []
        renamed_ids = []
        problems = []
        if not changes.get("available"):
            return {"applied": False, "reason": "state diff unavailable"}
        for feature in changes.get("featuresRenamed", []):
            try:
                tracked_id = self.tracker.rename_object(feature["before"], feature["after"])
                if tracked_id:
                    renamed_ids.append(tracked_id)
                else:
                    tracked_id, created = self.tracker.reconcile_live_feature(
                        feature["after"], feature.get("type", "unknown")
                    )
                    if created:
                        added_ids.append(tracked_id)
            except Exception as exc:
                problems.append(
                    f"rename {feature.get('before')} -> {feature.get('after')}: {exc}"
                )
        for feature in changes.get("featuresRemoved", []):
            try:
                removed_ids.extend(self.tracker.remove_feature(feature["name"]))
            except Exception as exc:
                problems.append(f"remove {feature.get('name')}: {exc}")
        for feature in changes.get("featuresAdded", []):
            try:
                tracked_id, created = self.tracker.reconcile_live_feature(
                    feature["name"], feature.get("type", "unknown")
                )
                if created:
                    added_ids.append(tracked_id)
            except Exception as exc:
                problems.append(f"add {feature.get('name')}: {exc}")
        result = {
            "applied": not problems,
            "addedIds": added_ids,
            "removedIds": removed_ids,
            "renamedIds": renamed_ids,
        }
        if problems:
            result["problems"] = problems
        return result

    def _run(self, operation: str, args: dict) -> str:
        code, options = self._validated_options(operation, args)
        execution_id = str(uuid.uuid4())
        context_error = None
        try:
            (
                doc,
                document_type,
                document_identity,
                sw_version,
                process_id,
                target_document,
            ) = self._live_context(connect=operation == "execute")
        except Exception as exc:
            doc = None
            document_type = "none"
            document_identity = None
            sw_version = "unknown"
            process_id = 0
            target_document = None
            context_error = exc

        history_recorded = True
        try:
            self.history.start_attempt(
                execution_id=execution_id,
                operation=operation,
                code=code,
                options=options,
                protocol_version=PROTOCOL_VERSION,
                server_version=_server_version(),
                document_type=document_type,
                document_identity=document_identity,
                solidworks_version=sw_version,
            )
        except Exception as exc:
            history_recorded = False
            logger.warning("Could not start C# snippet history for %s: %s", execution_id, exc)

        if operation == "execute" and (
            context_error is not None or doc is None or process_id <= 0 or target_document is None
        ):
            error_type = "connection_failed" if context_error is not None else "no_active_document"
            message = str(context_error) if context_error is not None else "No active SolidWorks document"
            response = {
                "executionId": execution_id,
                "compiled": False,
                "executed": False,
                "timedOut": False,
                "durationMs": 0,
                "diagnostics": [],
                "interopSymbols": [],
                "exception": {"type": error_type, "message": message},
                "result": None,
                "capturedOutput": "",
                "capturedOutputTruncated": False,
                "modelState": "known",
                "solidworksVersion": sw_version,
            }
            if history_recorded:
                history_recorded = self._finish_history(
                    execution_id, response, "precondition_failed"
                )
            response["historyRecorded"] = history_recorded
            return json.dumps(response, ensure_ascii=False)

        before = None
        pre_inspection_problem = None
        if operation == "execute":
            try:
                before = capture_live_snapshot(doc)
            except Exception as exc:
                # Snapshotting is observational and must never strand an audit row
                # or prevent a target-validated snippet from running.
                pre_inspection_problem = str(exc)
        request = {
            "protocolVersion": PROTOCOL_VERSION,
            "operation": operation,
            "executionId": execution_id,
            "code": code,
            "timeoutMs": options["timeoutMs"],
        }
        if operation == "execute":
            request["expectedProcessId"] = process_id
            request["expectedDocument"] = target_document
        try:
            outcome = self.worker.run(request, options["timeoutMs"])
        except WorkerError as exc:
            response = {
                "executionId": execution_id,
                "compiled": False,
                "executed": False,
                "timedOut": False,
                "durationMs": None,
                "diagnostics": [],
                "interopSymbols": [],
                "exception": {"type": exc.kind, "message": str(exc)},
                "result": None,
                "capturedOutput": "",
                "capturedOutputTruncated": False,
                "modelState": (
                    "unknown" if operation == "execute" and exc.process_started else "known"
                ),
                "solidworksVersion": sw_version,
            }
            if operation == "execute":
                response["before"] = before
            status = "worker_error"
            if history_recorded:
                history_recorded = self._finish_history(execution_id, response, status)
            response["historyRecorded"] = history_recorded
            return json.dumps(response, ensure_ascii=False)

        if outcome.timed_out:
            response = {
                "executionId": execution_id,
                "compiled": False,
                "executed": False,
                "timedOut": True,
                "durationMs": outcome.duration_ms,
                "diagnostics": [],
                "interopSymbols": [],
                "exception": {"type": "timeout", "message": "C# worker exceeded timeout"},
                "result": None,
                "capturedOutput": "",
                "capturedOutputTruncated": False,
                "modelState": "unknown" if operation == "execute" else "known",
                "solidworksVersion": sw_version,
            }
            if operation == "execute":
                response["before"] = before
            if history_recorded:
                history_recorded = self._finish_history(execution_id, response, "timed_out")
            response["historyRecorded"] = history_recorded
            return json.dumps(response, ensure_ascii=False)

        worker = outcome.response or {}
        response = {
            "executionId": execution_id,
            "compiled": bool(worker.get("compiled")),
            "executed": bool(worker.get("executed")),
            "timedOut": False,
            "durationMs": worker.get("durationMs", outcome.duration_ms),
            "diagnostics": worker.get("diagnostics") or [],
            "interopSymbols": worker.get("interopSymbols") or [],
            "exception": worker.get("exception"),
            "result": worker.get("result"),
            "capturedOutput": worker.get("capturedOutput") or "",
            "capturedOutputTruncated": bool(worker.get("capturedOutputTruncated", False)),
            "workerVersion": worker.get("workerVersion"),
            "solidworksVersion": sw_version,
            "modelState": "known",
        }
        if pre_inspection_problem is not None:
            response["preInspectionProblems"] = [pre_inspection_problem]

        if operation == "execute":
            response["before"] = before
            response["rebuild"] = None
            response["after"] = None
            response["stateChanges"] = {"available": False}
            response["trackerReconciliation"] = {
                "applied": False,
                "reason": "snippet did not enter execution",
            }

            # Compilation and target-validation failures cannot have run user
            # code, so they need no COM post-processing or rebuild.
            if response["executed"]:
                try:
                    if not self.connection.is_alive():
                        raise RuntimeError("SolidWorks connection is no longer alive")
                    post_doc = self.connection.get_active_doc()
                    if post_doc is None:
                        raise RuntimeError("SolidWorks no longer has an active document")
                    same_document = _same_com_object(doc, post_doc)
                    post_target = _document_target(post_doc)
                    if not same_document:
                        response["modelState"] = "unknown"
                        response["stateChanges"] = {
                            "available": False,
                            "activeDocumentChanged": True,
                        }
                        response["trackerReconciliation"] = {
                            "applied": False,
                            "reason": "active document changed",
                        }
                    else:
                        if options["rebuild"]:
                            ok, problems = verify_rebuild(post_doc)
                            response["rebuild"] = {"success": ok, "problems": problems}
                        if options["inspectAfter"]:
                            after = capture_live_snapshot(post_doc)
                            response["after"] = after
                            changes = diff_snapshots(before, after)
                            response["stateChanges"] = changes
                            response["trackerReconciliation"] = self._reconcile_tracker(
                                changes, same_document=True
                            )
                        else:
                            response["trackerReconciliation"] = {
                                "applied": False,
                                "reason": "post-execution inspection disabled",
                            }
                        if (
                            self.tracker
                            and target_document
                            and post_target
                            and target_document.get("title")
                            and post_target.get("title")
                            and target_document["title"] != post_target["title"]
                        ):
                            self.tracker.rename_document(
                                target_document["title"], post_target["title"]
                            )
                except Exception as exc:
                    response["modelState"] = "unknown"
                    response["postProcessingProblems"] = [str(exc)]
                    response["stateChanges"] = {"available": False}
                    response["trackerReconciliation"] = {
                        "applied": False,
                        "reason": "post-execution inspection failed",
                    }

        if not response["compiled"]:
            status = "compile_failed"
        elif operation == "compile":
            status = "compiled"
        elif (
            response["executed"]
            and response["exception"] is None
            and response["modelState"] == "unknown"
        ):
            status = "completed_unverified"
        elif response["executed"] and response["exception"] is None:
            status = "completed"
        else:
            status = "runtime_failed"
        if history_recorded:
            history_recorded = self._finish_history(execution_id, response, status)
        response["historyRecorded"] = history_recorded
        return json.dumps(response, ensure_ascii=False)


def redacted_code_summary(code: Any) -> str:
    """Safe representation for normal logs; source belongs only in SQLite."""
    if not isinstance(code, str):
        return "<non-string>"
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
    return f"<redacted {len(code.encode('utf-8'))} bytes sha256={digest}>"
