"""
SolidWorks MCP Server - Modular Edition
Organized with separate modules for better maintainability
"""

from mcp.server import Server
from mcp.types import Tool, TextContent, ImageContent
import mcp.server.stdio
from typing import Any
import logging
import sys
import asyncio
from pathlib import Path
import pythoncom

from solidworks.feature_errors import check_new_features, describe, feature_names
from solidworks import update_check
from solidworks.com_utils import com_prop
from solidworks import (
    SolidWorksConnection,
    StateTracker,
    SketchingTools,
    ModelingTools,
    FeatureTools,
    CutFeatureTools,
    AppliedFeatureTools,
    PatternTools,
    HoleFeatureTools,
    ReferenceGeometryTools,
    GeometryQueryTools,
    StateQueryTools,
    DocumentManagerTools,
    AssemblyTools,
    ConfigurationTools,
    FeatureTreeTools,
    CamTools,
    PythonExecTools,
)

# Configure logging
_log_path = Path(__file__).parent / 'solidworks_mcp.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger(__name__)


class SolidWorksMCPServer:
    """MCP Server for SolidWorks with modular architecture"""

    def __init__(self, bridge=None):
        self.server = Server("solidworks-mcp")
        # bridge: a bridge.client.BridgeClient when SolidWorks must be reached
        # through bridge/host.py (SSH / non-desktop sessions); None = direct COM.
        self.bridge = bridge

        # Initialize connection, state tracker, and tool modules
        self.connection = SolidWorksConnection()
        self.tracker = StateTracker()
        self.sketching = SketchingTools(self.connection, self.tracker)
        self.modeling = ModelingTools(self.connection, self.tracker)
        self.features = FeatureTools(self.connection, self.tracker)
        self.cut_features = CutFeatureTools(self.connection, self.tracker)
        self.applied_features = AppliedFeatureTools(self.connection, self.tracker)
        self.patterns = PatternTools(self.connection, self.tracker)
        self.hole_features = HoleFeatureTools(self.connection, self.tracker)
        self.reference_geometry = ReferenceGeometryTools(self.connection, self.tracker)
        self.geometry_query = GeometryQueryTools(self.connection, self.tracker)
        self.state_query = StateQueryTools(self.tracker, self.connection)
        self.document_manager = DocumentManagerTools(self.connection, self.tracker)
        self.assembly = AssemblyTools(self.connection, self.tracker)
        self.configurations = ConfigurationTools(self.connection, self.tracker)
        self.feature_tree = FeatureTreeTools(self.connection, self.tracker)
        self.cam = CamTools(self.connection, self.tracker)
        self.python_exec = PythonExecTools(self.connection, self.tracker)   # opt-in, listed only when enabled

        # All modules (order matters for tool listing)
        self._modules = [
            self.sketching,
            self.modeling,
            self.features,
            self.cut_features,
            self.applied_features,
            self.patterns,
            self.hole_features,
            self.reference_geometry,
            self.geometry_query,
            self.state_query,
            self.document_manager,
            self.assembly,
            self.configurations,
            self.feature_tree,
            self.cam,
            self.python_exec,
        ]

        # Build dispatch map: tool_name -> module
        self._route_map = {}
        for module in self._modules:
            for tool_def in module.get_tool_definitions():
                self._route_map[tool_def.name] = module
        # Not listed when disabled, but still routed so a direct call gets the
        # module's explanation (how to enable it) instead of 'Unknown tool'.
        self._route_map.setdefault("solidworks_execute_python", self.python_exec)

        self.setup_handlers()

    def setup_handlers(self):
        """Setup MCP tool handlers"""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            tools = []
            for module in self._modules:
                tools.extend(module.get_tool_definitions())
            tools.append(Tool(
                name="solidworks_batch",
                description=(
                    "Execute several solidworks_* tool calls in ONE request, "
                    "sequentially. Use for INDEPENDENT operations whose arguments "
                    "don't depend on each other's results (e.g. several sketch "
                    "entities, several dimensions). Results from earlier calls in "
                    "the batch canNOT be referenced by later calls — if you need a "
                    "returned ID or measurement, split the batch there. On error: "
                    "stops by default (stopOnError=false to continue); the response "
                    "reports each call's result so you know exactly what applied. "
                    "Do NOT put look_at_model in a batch — its screenshot is only "
                    "attached as an image when called standalone."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "calls": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 25,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "tool": {"type": "string",
                                             "description": "solidworks_* tool name"},
                                    "args": {"type": "object",
                                             "description": "That tool's arguments"}
                                },
                                "required": ["tool"]
                            }
                        },
                        "stopOnError": {"type": "boolean",
                                        "description": "Stop at first failed call (default true)"}
                    },
                    "required": ["calls"]
                }
            ))
            return tools

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Any) -> list[TextContent]:
            if self.bridge is not None:
                # Bridge mode: this process may not be in the desktop session
                # (e.g. over SSH), so never touch COM here — forward the call
                # to bridge/host.py running on the interactive desktop.
                logger.info(f"Tool called (via bridge): {name}")
                content = await asyncio.to_thread(self.bridge.call_tool, name, arguments)
            else:
                content = self.handle_call(name, arguments)
            # One-time "update available" notice from the start-up check
            # (solidworks/update_check.py), as an extra text item.
            notice = update_check.take_notice()
            if notice:
                content = list(content) + [TextContent(type="text", text="ℹ " + notice)]
            return content

    def handle_call(self, name: str, arguments: Any) -> list:
        """Run one tool call against SolidWorks and return MCP content items.
        Shared by the stdio server and the desktop bridge host."""
        logger.info(f"Tool called: {name} with arguments: {arguments}")

        # Ensure connection
        if not self.connection.app:
            if not self.connection.connect():
                return [TextContent(
                    type="text",
                    text="❌ Failed to connect to SolidWorks"
                )]

        try:
            # Route to appropriate module
            result = self._route_tool(name, arguments)
            # IMAGE_FILE convention: first line names a PNG to attach as
            # image content (look_at_model — visual feedback mid-build)
            if result.startswith("IMAGE_FILE:"):
                first, _, rest = result.partition("\n")
                img_path = first[len("IMAGE_FILE:"):].strip()
                try:
                    import base64
                    with open(img_path, "rb") as f:
                        data = base64.b64encode(f.read()).decode("ascii")
                    return [ImageContent(type="image", data=data,
                                         mimeType="image/png"),
                            TextContent(type="text", text=rest)]
                except Exception as e:
                    logger.warning(f"image attach failed: {e}")
                    return [TextContent(type="text", text=rest)]
            return [TextContent(type="text", text=result)]

        except Exception as e:
            error_msg = f"❌ Error in {name}: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return [TextContent(type="text", text=error_msg)]

    def _route_tool(self, name: str, arguments: Any) -> str:
        """Route tool calls to appropriate module"""
        arguments = arguments or {}
        if name == "solidworks_batch":
            return self._execute_batch(arguments)
        module = self._route_map.get(name)
        if module is None:
            raise Exception(f"Unknown tool: {name}")

        # Every tool of a feature-creating module gets the feature error check:
        # SOLIDWORKS can return a feature that is in error or carries a warning
        # and did nothing, while the tool reports success (see feature_errors).
        doc, names_before = None, None
        if module in (self.modeling, self.features, self.cut_features, self.applied_features,
                      self.patterns, self.hole_features, self.reference_geometry):
            try:
                doc = self.connection.get_active_doc()
                if doc is not None and int(com_prop(doc, "GetType")) in (1, 2):
                    names_before = feature_names(doc)
            except Exception as e:
                logger.debug(f"feature snapshot before {name} skipped: {e}")
                names_before = None

        # Modeling module needs a reference to sketching tools
        if module is self.modeling:
            out = module.execute(name, arguments, self.sketching)
        else:
            out = module.execute(name, arguments)
        if names_before is None:
            return out
        return self._apply_feature_check(name, doc, names_before, out)

    def _apply_feature_check(self, name, doc, names_before, out):
        """Errors: the feature is removed and the call fails. Warnings: the
        feature is kept and the result is marked ⚠ with the official text."""
        import json as _json
        try:
            errors, warnings = check_new_features(doc, names_before, self.tracker)
        except Exception as e:
            logger.warning(f"feature error check after {name} failed: {e}")
            return out
        if errors:
            listed = "; ".join(f"'{n}' ({describe(c)})" for n, c in errors)
            raise Exception(
                f"{name}: SOLIDWORKS created the feature in an error state, so it did not "
                f"do what was asked -- {listed}. The feature was removed (its sketch is kept).")
        if not warnings:
            return out
        note = (" ⚠ SOLIDWORKS flags "
                + "; ".join(f"'{n}' with a warning ({describe(c)})" for n, c in warnings)
                + ". The feature was kept but may not have done everything asked "
                  "(e.g. pattern/mirror copies outside the body) -- check the geometry.")
        try:
            data = _json.loads(out)
            if isinstance(data, dict) and isinstance(data.get("result"), str):
                r = data["result"]
                data["result"] = ("⚠" + r[1:] if r.startswith("✓") else r) + note
                data["featureWarnings"] = [{"feature": n, "code": c, "description": describe(c)}
                                           for n, c in warnings]
                return _json.dumps(data)
        except Exception:
            pass
        return out + note

    def _execute_batch(self, arguments: dict) -> str:
        """Sequentially execute a list of tool calls in one MCP request.
        No result chaining between calls — the agent is told to split the
        batch wherever it needs a returned value."""
        import json as _json
        calls = arguments.get("calls") or []
        if not calls:
            raise Exception("solidworks_batch requires a non-empty calls list")
        if any(c.get("tool") == "solidworks_batch" for c in calls):
            raise Exception("solidworks_batch cannot nest itself")
        stop_on_error = arguments.get("stopOnError", True)
        results = []
        ok_count = 0
        for i, call in enumerate(calls):
            tool = call.get("tool") or ""
            try:
                out = self._route_tool(tool, call.get("args") or {})
                results.append({"i": i, "tool": tool, "ok": True, "result": out})
                ok_count += 1
            except Exception as e:
                results.append({"i": i, "tool": tool, "ok": False,
                                "error": str(e)})
                if stop_on_error:
                    for j in range(i + 1, len(calls)):
                        results.append({"i": j, "tool": calls[j].get("tool") or "",
                                        "ok": False, "error": "skipped (earlier call failed)"})
                    break
        summary = f"{'✓' if ok_count == len(calls) else '⚠'} batch: {ok_count}/{len(calls)} calls succeeded"
        return _json.dumps({"result": summary, "calls": results})


async def main():
    """Main entry point"""
    logger.info("Starting SolidWorks MCP Server (Modular Edition)...")
    from bridge.client import client_from_env
    bridge = client_from_env()
    if bridge is not None:
        logger.info(f"Bridge mode: forwarding tool calls to {bridge.describe()}")
    server = SolidWorksMCPServer(bridge=bridge)
    update_check.start(str(Path(__file__).resolve().parent))

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.server.run(
            read_stream,
            write_stream,
            server.server.create_initialization_options()
        )


def run():
    """Synchronous entry point (console script + python server.py)."""
    pythoncom.CoInitialize()
    asyncio.run(main())


if __name__ == "__main__":
    run()
