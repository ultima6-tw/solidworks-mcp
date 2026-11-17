"""Desktop-session host for the SolidWorks MCP bridge.

Run this on the Windows desktop where SOLIDWORKS runs (NOT over SSH):

    python -m bridge.host            (from the repository root)
    bridge\\start_host.cmd           (same thing, picks the repo's venv)

It listens on 127.0.0.1 only, owns the single COM connection and the state
tracker, and executes forwarded tool calls one at a time on a dedicated COM
thread. Each start writes a fresh random token to
%LOCALAPPDATA%\\solidworks-mcp\\bridge.json; clients must send it back.

Environment:
    SOLIDWORKS_MCP_BRIDGE_PORT   listen port (default 47110)
"""
import ctypes
import json
import logging
import os
import queue
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bridge.common import DEFAULT_PORT, TOKEN_HEADER, read_state, state_dir, state_file  # noqa: E402

logger = logging.getLogger("bridge.host")


def current_session_id():
    sid = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    return sid.value if ok else None


class ComWorker(threading.Thread):
    """Owns COM + the MCP server object; runs calls strictly one at a time."""

    def __init__(self):
        super().__init__(name="com-worker", daemon=True)
        self.jobs = queue.Queue()
        self.ready = threading.Event()
        self.busy_with = None
        self.init_error = None

    def run(self):
        import pythoncom
        pythoncom.CoInitialize()
        try:
            from server import SolidWorksMCPServer
            self.mcp = SolidWorksMCPServer(bridge=None)
        except Exception as e:  # surfaced to callers
            self.init_error = e
            logger.exception("failed to initialise MCP server")
        self.ready.set()
        while True:
            name, arguments, done = self.jobs.get()
            self.busy_with = name
            try:
                if self.init_error:
                    raise self.init_error
                items = self.mcp.handle_call(name, arguments)
                done["result"] = [i.model_dump(exclude_none=True) for i in items]
            except Exception as e:
                logger.exception("call %s failed", name)
                done["result"] = [{"type": "text", "text": f"❌ Bridge host error in {name}: {e}"}]
            finally:
                self.busy_with = None
                done["event"].set()

    def submit(self, name, arguments):
        done = {"event": threading.Event()}
        self.jobs.put((name, arguments, done))
        done["event"].wait()
        return done["result"]


def make_handler(worker, token):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True, "pid": os.getpid(),
                                 "busy": worker.busy_with,
                                 "queued": worker.jobs.qsize()})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/call":
                return self._send(404, {"error": "not found"})
            if not secrets.compare_digest(self.headers.get(TOKEN_HEADER, ""), token):
                return self._send(403, {"error": "bad or missing token"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            name = req.get("name")
            if not name:
                return self._send(400, {"error": "missing 'name'"})
            self._send(200, worker.submit(name, req.get("arguments") or {}))

        def log_message(self, fmt, *args):
            logger.info("%s - %s", self.address_string(), fmt % args)

    return Handler


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    sid = current_session_id()
    if sid == 0 and "--allow-session-0" not in argv:
        print("ERROR: bridge host is running in Session 0 (a service/SSH session).\n"
              "It must run on the interactive desktop where SOLIDWORKS is open;\n"
              "from Session 0 it cannot see that SOLIDWORKS and could start a\n"
              "hidden second copy. Start it from the desktop instead.",
              file=sys.stderr)
        return 2

    old = read_state()
    if old and old.get("pid") and old.get("pid") != os.getpid():
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{old['port']}/health", timeout=2) as r:
                if json.loads(r.read()).get("ok"):
                    print(f"Bridge host already running (pid {old['pid']}, port {old['port']}).")
                    return 0
        except Exception:
            pass  # stale state file — take over

    port = int(os.environ.get("SOLIDWORKS_MCP_BRIDGE_PORT", DEFAULT_PORT))
    token = secrets.token_urlsafe(32)

    worker = ComWorker()
    worker.start()
    worker.ready.wait()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(worker, token))
    state_dir().mkdir(parents=True, exist_ok=True)
    state_file().write_text(json.dumps({"port": port, "token": token, "pid": os.getpid()}),
                            encoding="utf-8")
    print(f"SolidWorks MCP bridge host listening on 127.0.0.1:{port} "
          f"(session {sid}, pid {os.getpid()}). Keep this window open; Ctrl+C to stop.")
    from solidworks.python_exec import ENV_FLAG, python_enabled
    if python_enabled():
        print("
" + "!" * 72 + f"
  WARNING: solidworks_execute_python is ENABLED ({ENV_FLAG}=1).
"
              "  Any client that reaches this host (e.g. an AI agent over SSH) can run
"
              "  arbitrary Python on THIS computer with this Windows user's rights.
"
              "  Every snippet is logged to solidworks_mcp.log.
" + "!" * 72 + "
")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
