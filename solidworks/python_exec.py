"""
Opt-in escape hatch: run arbitrary Python against the live SOLIDWORKS session.

DISABLED BY DEFAULT. It is only offered when the process that talks to SOLIDWORKS
was started with SOLIDWORKS_MCP_ENABLE_PYTHON=1:
  - local use: the MCP server itself (env in the Claude Desktop / Claude Code config);
  - SSH bridge: the desktop bridge host. Setting the variable on the SSH side
    does nothing — the host refuses unless IT was started with the variable.
When disabled the tool is not listed at all, and a direct call is refused.

What the switch protects against: an AI agent (or a prompt injection in what it
reads) running arbitrary code on this computer. What it does NOT protect
against: a person who can already log in to this Windows account (e.g. over
SSH) — they can run any program anyway. Every executed snippet is written to
solidworks_mcp.log.

Ported from the alisamsam SolidWorks MCP server (execute_python), which exposed
it unconditionally.
"""

import contextlib
import io
import json
import logging
import math
import os
import traceback

import pythoncom
import win32com.client
from win32com.client import VARIANT
from mcp.types import Tool

logger = logging.getLogger(__name__)

ENV_FLAG = "SOLIDWORKS_MCP_ENABLE_PYTHON"
TOOL_NAME = "solidworks_execute_python"
MAX_OUTPUT = 20000


def python_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip() == "1"


class PythonExecTools:
    """solidworks_execute_python — listed only when enabled."""

    def __init__(self, connection, tracker=None):
        self.connection = connection
        self.tracker = tracker
        if python_enabled():
            logger.warning(f"{TOOL_NAME} is ENABLED ({ENV_FLAG}=1): the AI can run arbitrary Python on this computer")

    def get_tool_definitions(self) -> list[Tool]:
        if not python_enabled():
            return []
        return [Tool(
            name=TOOL_NAME,
            description="LAST RESORT: run Python code in the server process against the live SOLIDWORKS session — only when no "
                        "dedicated tool can do the job. Names available: sw (ISldWorks), doc (active document or None), win32com, "
                        "pythoncom, VARIANT, math. Late-bound COM: properties are read without parentheses (doc.GetTitle), methods "
                        "are called (doc.ForceRebuild3(True)); byref/out arguments need VARIANT. Units are metres and radians. "
                        "Returns print() output and the value of a variable named `result`; an exception returns its traceback. "
                        "Features created here get no tracked IDs (get_feature_tree still sees them). A code that never ends blocks "
                        "the server until it is restarted. Every snippet is logged. Enabled by the user at install time "
                        f"({ENV_FLAG}=1); it runs with this Windows user's rights.",
            inputSchema={
                "type": "object",
                "properties": {"code": {"type": "string", "description": "Python source to execute"}},
                "required": ["code"],
            },
        )]

    def execute(self, tool_name: str, args: dict) -> str:
        if tool_name != TOOL_NAME:
            raise Exception(f"Unknown tool: {tool_name}")
        if not python_enabled():
            raise Exception(f"{TOOL_NAME} is disabled. It runs arbitrary code, so it is off unless the user enables it at "
                            f"install time ({ENV_FLAG}=1 for the process that talks to SOLIDWORKS — with the SSH bridge, "
                            "the desktop bridge host).")
        self.connection.ensure_connection()
        code = args.get("code", "")
        logger.warning(f"{TOOL_NAME} code:\n{code}")
        sw = self.connection.app
        env = {"__name__": "__solidworks_mcp_exec__", "sw": sw, "doc": sw.ActiveDoc if sw else None,
               "win32com": win32com, "pythoncom": pythoncom, "VARIANT": VARIANT, "math": math}
        out, err = io.StringIO(), io.StringIO()
        tb = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                exec(compile(code, "<execute_python>", "exec"), env)
            except Exception:
                tb = traceback.format_exc()

        def cut(s):
            return s if len(s) <= MAX_OUTPUT else s[:MAX_OUTPUT] + f"\n... [truncated, {len(s)} chars]"

        d = {"result": "✓ Code executed" if tb is None else f"❌ Code raised {tb.strip().splitlines()[-1]}",
             "stdout": cut(out.getvalue())}
        if err.getvalue():
            d["stderr"] = cut(err.getvalue())
        if "result" in env and env["result"] is not None:
            d["value"] = cut(repr(env["result"]))
        if tb:
            d["traceback"] = cut(tb)
        d["type"] = "python"
        return json.dumps(d, ensure_ascii=False)
