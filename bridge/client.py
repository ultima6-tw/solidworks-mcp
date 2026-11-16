"""Client side of the desktop bridge, used by server.py in bridge mode.

Enable with the environment variable ``SOLIDWORKS_MCP_BRIDGE``:

    SOLIDWORKS_MCP_BRIDGE=1                       port + token from the host's state file
    SOLIDWORKS_MCP_BRIDGE=http://127.0.0.1:47110  explicit URL (token still from state file)

``SOLIDWORKS_MCP_BRIDGE_TIMEOUT`` (seconds, default 900) bounds one call.
"""
import json
import os
import urllib.error
import urllib.request

from mcp.types import ImageContent, TextContent

from .common import DEFAULT_PORT, TOKEN_HEADER, read_state, state_file

_NOT_RUNNING = (
    "❌ SolidWorks bridge host is not reachable ({detail}). Start it on the "
    "Windows desktop (the session where SOLIDWORKS runs), e.g. run "
    "bridge\\start_host.cmd from the solidworks-mcp folder, then retry."
)


class BridgeClient:
    def __init__(self, url=None, timeout=900.0):
        self._explicit_url = url
        self.timeout = timeout

    def describe(self):
        return self._explicit_url or f"host from {state_file()}"

    def _endpoint(self):
        state = read_state() or {}
        token = state.get("token", "")
        if self._explicit_url:
            return self._explicit_url.rstrip("/"), token
        port = state.get("port") or DEFAULT_PORT
        return f"http://127.0.0.1:{port}", token

    def call_tool(self, name, arguments):
        base, token = self._endpoint()
        body = json.dumps({"name": name, "arguments": arguments or {}}).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/call", data=body, method="POST",
            headers={"Content-Type": "application/json", TOKEN_HEADER: token},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                items = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code == 403:
                return [TextContent(type="text", text=(
                    "❌ Bridge host rejected the token. The host was probably "
                    "restarted; restart this MCP server (or it will re-read the "
                    f"state file on the next call). Detail: {detail}"))]
            return [TextContent(type="text", text=f"❌ Bridge host error {e.code}: {detail}")]
        except (urllib.error.URLError, ConnectionError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason):
                return [TextContent(type="text", text=(
                    f"❌ Bridge call '{name}' timed out after {self.timeout:.0f}s. "
                    "The operation may STILL be running in SOLIDWORKS — do NOT "
                    "retry it blindly (a retried modeling call can create a "
                    "duplicate feature). Check the model state first "
                    "(solidworks_get_state / solidworks_get_feature_tree)."))]
            return [TextContent(type="text", text=_NOT_RUNNING.format(detail=reason))]
        except TimeoutError:
            return [TextContent(type="text", text=(
                f"❌ Bridge call '{name}' timed out after {self.timeout:.0f}s. "
                "It may still be running — check state before retrying."))]
        out = []
        for item in items:
            if item.get("type") == "image":
                out.append(ImageContent.model_validate(item))
            else:
                out.append(TextContent.model_validate(item))
        return out


def client_from_env():
    value = os.environ.get("SOLIDWORKS_MCP_BRIDGE", "").strip()
    if not value or value.lower() in ("0", "false", "no", "off"):
        return None
    timeout = float(os.environ.get("SOLIDWORKS_MCP_BRIDGE_TIMEOUT", "900"))
    url = value if value.lower().startswith("http") else None
    return BridgeClient(url=url, timeout=timeout)
