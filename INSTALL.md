# Installation

[繁體中文](INSTALL.zh-TW.md)

There is no installer. Give this page to your AI app (Claude Code, Codex, …)
and ask it to set up the server for you, for example:

> Read https://github.com/ultima6-tw/solidworks-mcp/blob/main/INSTALL.md and
> set up this SolidWorks MCP server on my computer.

The AI chooses how to install it on your computer. This page gives it the facts
it needs, the choices it must leave to you, and how to check the result. You can
also follow it yourself.

## Rules for the AI doing the setup

1. **Ask the person; do not decide for them.** Two choices change what this
   server can do on their computer:
   - [Local or SSH](#local-or-ssh): if you are running on the Windows desktop
     (session ID is not 0), use Local — there is nothing to ask. If you are
     running in an SSH session (session 0), Local mode cannot work there: ask
     whether they want SSH mode or would rather run the AI app on the desktop,
     and show them the SSH risks below before they decide.
   - [execute_python](#execute_python): always ask. Show them the risks below
     as written; it stays off unless they clearly choose to enable it.
2. **Show your plan before you change anything**: where you will put the files,
   which Python you will use, and which config files you will edit. Back up a
   config file before you edit it, and keep the other entries in it.
   - Say **where the server will be registered**: which AI app, and at which
     level — for Claude Code, `user` (loaded in every project) or `project` /
     `local` (this folder only).
   - **Check for an existing entry with the same name first** (for example
     `claude mcp list`). If there is one, ask the person what to do; do not
     overwrite or rename it on your own.
3. **Do not close or modify the person's SOLIDWORKS documents.** The checks
   below create their own part. **Do not run `test.py` during setup**: it is for
   developing this project, and it closes **all** open documents **without
   saving** before it starts.
4. **Check the result** with [the checks below](#check-the-installation) and
   report what passed and what did not.

## Requirements

- **Windows** with **SOLIDWORKS** installed and activated. This fork is tested
  only on SOLIDWORKS 2022 SP04 with a Traditional Chinese interface (see
  [TESTING.md](TESTING.md)); the original project was tested on SOLIDWORKS 2025.
- For `recognize_features` (turning an imported STEP/IGES body back into
  features): FeatureWorks, which comes with SOLIDWORKS Professional and
  Premium. Tick it in SOLIDWORKS › Tools › Add-Ins, both boxes (active now and
  Start Up). Seen on SOLIDWORKS 2022: loading it from the server alone
  sometimes reports success without starting it.
- **Python 3.10 or later**, 64-bit. Tested with 3.12 and 3.13.
- An AI app that supports local MCP servers over stdio: Claude Desktop, Claude
  Code, Codex, or another MCP client.

## Install the code

Get the code from https://github.com/ultima6-tw/solidworks-mcp (git clone, or
download the ZIP of `main`) and install its dependencies into a virtual
environment, for example:

```powershell
git clone https://github.com/ultima6-tw/solidworks-mcp.git
cd solidworks-mcp
python -m venv .venv
.venv\Scripts\pip install -e .
```

Any other way (uv, a different folder) is fine. What matters:

- **`mcp` must stay below 2** (`mcp>=1.26,<2`, as in `pyproject.toml` and
  `requirements.txt`). With mcp 2.x the server crashes on start
  (`'Server' object has no attribute 'list_tools'`).
- The server is started as `<python of that environment> <full path>\server.py`.
  Its working files (saved parts, screenshots, DXF) go to `workspace\` next to
  `server.py`; its log is `solidworks_mcp.log` in the same folder.

## Register it with the AI app

Use full paths. Restart the AI app afterwards.

**Claude Code** — options go **before** the server name:

```powershell
claude mcp add -s user --transport stdio solidworks -- C:\path\to\.venv\Scripts\python.exe C:\path\to\server.py
```

Environment variables are added as `-e NAME=value`, also before the name.
`claude mcp get solidworks` shows the entry.

**Codex / ChatGPT desktop:**

```powershell
codex mcp add solidworks -- C:\path\to\.venv\Scripts\python.exe C:\path\to\server.py
```

Environment variables: `--env NAME=value` before `--`.

**Claude Desktop** — add an entry under `mcpServers` in
`claude_desktop_config.json` (Settings → Developer → Edit Config). The file is
`%APPDATA%\Claude\claude_desktop_config.json`, or for the Microsoft Store
version `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json`.
Keep everything else in the file.

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\server.py"],
      "env": {}
    }
  }
}
```

Claude Desktop must be fully restarted (File → Exit, then open it again).

## Local or SSH

| Mode | Use it when | |
|---|---|---|
| **Local** | The AI app runs on the same Windows desktop as SOLIDWORKS. | **Default.** Nothing else to set up. |
| **SSH** | The AI app (Claude Code, Codex CLI) runs in an SSH session to this computer. | Show the person the risks below first. |

How to tell: programs started over SSH (or as a service) run in Windows
**Session 0**. From there, COM cannot reach the SOLIDWORKS on the desktop, so
Local mode does not work in an SSH session. In PowerShell,
`(Get-Process -Id $PID).SessionId` prints `0` in an SSH session.

### Risks of SSH mode — show these to the person

SSH mode lets an AI app that runs over SSH use SOLIDWORKS on this computer's
desktop:

1. **Anyone who can log in to this Windows account over SSH** can read the
   bridge token and call every SOLIDWORKS tool.
2. **The person at this desktop is affected**: tools act on the SOLIDWORKS
   window they are using. `close_document` closes **without saving**, and the
   test suite closes all open documents first.
3. **If execute_python is also enabled**, the remote AI can run any program on
   this computer — including because of a prompt injection in a file or web
   page it reads.
4. **`screenshot` sends the SOLIDWORKS window** (including other open designs)
   to the remote AI service.
5. **Dialogs cannot be seen remotely**: a call may hang until someone at the
   desktop answers it, and that person can interfere with what the AI is doing.

Recommended: don't use it on a computer someone else is working on, save your
work before you start, and give SSH access only to people you trust.

### How SSH mode works

- A **bridge host** runs on the desktop and does all the SOLIDWORKS work, one
  call at a time. It listens on `127.0.0.1` only (port 47110, or
  `SOLIDWORKS_MCP_BRIDGE_PORT`).
- Start it **on the desktop**, not over SSH: run `bridge\start_host.cmd` (it
  uses `SOLIDWORKS_MCP_PYTHON` if set, else `.venv` next to the code, else
  `python`) and keep its window open. It refuses to start in Session 0. To start
  it at login, put a shortcut to it in `shell:startup`. If a host is already
  running, a second one exits and the running one keeps its settings.
- On each start the host writes a new random token to
  `%LOCALAPPDATA%\solidworks-mcp\bridge.json`, readable only by this Windows
  account — which is why risk 1 applies to anyone who can log in as you.
- Register the server in the SSH-side AI app with
  `SOLIDWORKS_MCP_BRIDGE=1`. It then forwards every tool call to the host.
  `SOLIDWORKS_MCP_BRIDGE_TIMEOUT` sets the seconds allowed per call
  (default 900).
- Check: `claude mcp list` (or the Codex equivalent) shows `solidworks`
  connected. If a tool call says the bridge host is not reachable, the host is
  not running on the desktop.

## execute_python

`solidworks_execute_python` runs any Python code the AI writes, inside the
server, with the Windows user's rights. It can reach things the dedicated tools
cannot, but it is not limited to SOLIDWORKS: it can read and write files, use
the network and start programs. **Leave it off unless the person asks for it
after reading this section.**

- **Off by default.** When it is off, the AI does not see the tool, and a direct
  call is refused.
- To turn it on, set `SOLIDWORKS_MCP_ENABLE_PYTHON=1` for the process that runs
  the SOLIDWORKS commands: in Local mode the server registered in the AI app; in
  SSH mode the bridge host (set it before `start_host.cmd`), and also in the
  SSH-side registration so the tool is listed. Without it on the host, the call
  is refused there.
- When it is on, the bridge host prints a warning on the desktop, and every
  piece of code is written to `solidworks_mcp.log`.

**What the switch protects against, and what it does not.** It stops the AI —
or a prompt injection in something the AI reads — from running arbitrary code
on this computer. It does **not** protect against a person who can already log
in to this Windows account (for example over SSH): they can run programs
anyway. Who can log in is controlled by the Windows account and SSH settings,
not by this project.

## Check the installation

Start SOLIDWORKS first (otherwise the first call starts it, which is slow).
Then, in the AI app, check:

1. **Tools are listed**: about 104 tools whose names start with `solidworks_`;
   `solidworks_execute_python` only if it was enabled.
2. **Geometry works**: create a 50 mm cube (sketch on the Front plane, centred
   on the origin, extrude 50 mm) and get its mass properties. The volume must be
   125,000 mm³.
3. **Screenshot works**: `solidworks_screenshot` saves a PNG of the SOLIDWORKS
   window, and the picture shows the cube.
4. **A file export works**: `solidworks_export_dxf` on the face at z = 50
   returns 4 LINE entities and extents (0, 0)–(50, 50) mm.
5. Close the cube with `solidworks_close_document` (it discards changes).

These checks are enough. The project itself has already been tested (see
[TESTING.md](TESTING.md)), so there is no need to run its test suite.

**If a tool call never returns**, read `solidworks_mcp.log`: if the last line is
the tool being called, look on the desktop for a SOLIDWORKS dialog. In SSH mode,
check that the bridge host window is open.

## Updates

There is no automatic update. What changed, and which updates matter, is in
[CHANGELOG.md](CHANGELOG.md).

- **How you find out**: when the server starts it asks GitHub once, in the
  background, whether the branch your copy is on has commits your copy does not
  have. If so, the first tool result of the session carries a note saying so
  (`ℹ Update available …`); tell the person. The check sends one unauthenticated
  request to the public GitHub API (`/repos/<owner>/<repo>/compare/<your commit>...<branch>`,
  generic User-Agent) and nothing about the person, the machine or the documents.
  It gives up after a few seconds, never delays start-up, and only works for a
  git checkout (a downloaded ZIP has no commit to compare). Copies with local
  commits that are not on GitHub are not reported.
- **Turn the check off**: set `SOLIDWORKS_MCP_UPDATE_CHECK=0` in the server's
  environment (the `env` of its registration).
- **Update**: `git pull` in the folder with the code, then restart the MCP server
  (restart the AI app or its MCP connection). In SSH mode also restart the bridge
  host on the desktop. If `requirements.txt` changed, install it again into the
  same virtual environment.

## Uninstall

1. Remove the registration: `claude mcp remove solidworks -s user`,
   `codex mcp remove solidworks`, or the `"solidworks"` entry in
   `claude_desktop_config.json`.
2. Delete the folder with the code (your saved parts are in its `workspace`
   folder — copy them first if you want them).
3. SSH mode: delete the bridge host shortcut from `shell:startup`, and
   `%LOCALAPPDATA%\solidworks-mcp`.
