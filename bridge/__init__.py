"""Desktop bridge for running the MCP server outside the interactive desktop.

Why this exists
---------------
SOLIDWORKS is a COM server that lives in the interactive desktop session.
Windows isolates COM's Running Object Table per logon session, so an MCP
server started from a *different* session — most commonly an SSH login,
which runs in Session 0 — cannot see the SOLIDWORKS window the user is
working in. Worse, ``Dispatch("SldWorks.Application")`` from there silently
launches a second, invisible SOLIDWORKS in Session 0.

The bridge splits the server in two:

    MCP client ──stdio──> server.py  (any session; SOLIDWORKS_MCP_BRIDGE=1)
                             │  HTTP on 127.0.0.1, token-authenticated
                             ▼
                          bridge/host.py  (desktop session) ──COM──> SOLIDWORKS

Tool schemas are listed locally (no COM needed); every tool *call* is
forwarded to the host, which owns the COM connection and the state tracker.

Not needed for Claude Desktop or for a Claude Code terminal opened on the
desktop itself — those already run in the desktop session and talk to
SOLIDWORKS directly.
"""
