# Installation

[繁體中文](INSTALL.zh-TW.md)

## Requirements

- **Windows** with **SOLIDWORKS** installed and activated. This fork is tested
  only on SOLIDWORKS 2022 SP04 with a Traditional Chinese interface (see
  [TESTING.md](TESTING.md)); the original project was tested on SOLIDWORKS 2025.
- An AI app that supports local MCP servers: **Claude Desktop**, **Claude Code**,
  or **Codex / ChatGPT desktop**.

The installer takes care of Python and everything else.

## Which mode?

| Mode | Use it when | |
|---|---|---|
| **Local** | The AI app runs on the same Windows desktop as SOLIDWORKS. | **Default. Choose this unless you know you need SSH.** |
| **SSH** | You also run the AI app (Claude Code, Codex CLI) in an SSH session to this computer. | Read [SSH mode](#ssh-mode) first: it has risks Local mode does not. |

## Install

Open **PowerShell** (press `Win`, type "PowerShell", press Enter) and paste:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/ultima6-tw/solidworks-mcp/main/scripts/install.ps1 | iex"
```

Before it downloads anything, the installer asks:

1. **Local or SSH** — Enter keeps Local. SSH shows the risks and continues only
   if you type `ACCEPT`; otherwise you can install in Local mode or quit.
2. **Enable `solidworks_execute_python`?** — Enter keeps it off (recommended).
   Yes shows the risks and enables it only if you type `ACCEPT`. See
   [execute_python](#execute_python).
3. *(SSH only)* **Start the bridge host when you log in?** — Enter means yes.

Then it installs [uv](https://docs.astral.sh/uv/) (which provides Python),
downloads the latest release of this project into
`%LOCALAPPDATA%\SolidWorksMCP`, and registers the server with every AI app it
detects. Restart the AI app completely and ask it to *"create a 50 mm cube in
SolidWorks"*.

To **update** or **change your answers**, run the same command again.

### Unattended install

Pass parameters instead of answering questions:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/ultima6-tw/solidworks-mcp/main/scripts/install.ps1))) -Mode Local
```

| Parameter | Values | Default |
|---|---|---|
| `-Mode` | `Ask`, `Local`, `Ssh` | `Ask` (`Local` when nobody can answer) |
| `-AcceptSshRisk` | switch | required with `-Mode Ssh`, otherwise the installer refuses |
| `-EnablePython` | switch | off |
| `-AcceptPythonRisk` | switch | required with `-EnablePython`, otherwise the installer refuses |
| `-HostAutoStart` | `Ask`, `Yes`, `No` | `Ask` (`No` when nobody can answer) |
| `-Client` | `Auto`, `ClaudeDesktop`, `ClaudeCode`, `Codex`, `All` (also `Claude`, `Both` from the original installer) | `Auto` (every detected app) |
| `-InstallDir` | folder | `%LOCALAPPDATA%\SolidWorksMCP` |

When a risky choice is refused, nothing is downloaded or changed.

## SSH mode

### Risks — read before you accept

SSH mode lets an AI app that you run over SSH use SOLIDWORKS on this computer's
desktop:

1. **Anyone who can log in to this Windows account over SSH** can read the
   bridge token and call every SOLIDWORKS tool.
2. **The person at this desktop is affected**: tools act on the SOLIDWORKS
   window they are using. `close_document` closes **without saving**, and the
   test suite closes all open documents first.
3. **If you also enable `execute_python`**, the remote AI can run any program on
   this computer — including because of a prompt injection in a file or web
   page it reads.
4. **`screenshot` sends the SOLIDWORKS window** (including other open designs)
   to the remote AI service.
5. **Dialogs cannot be seen remotely**: a call may hang until someone at the
   desktop answers it, and that person can interfere with what the AI is doing.

Recommended: don't use it on a computer someone else is working on, save your
work before you start, and give SSH access only to people you trust.

### How it works

Programs started over SSH run in a separate Windows session and cannot reach
the SOLIDWORKS on the desktop. So:

- A **bridge host** runs on the desktop (Start menu: *SolidWorks MCP Bridge
  Host*; keep its window open). It listens on `127.0.0.1` only and does all the
  SOLIDWORKS work, one call at a time.
- Claude Code and Codex are registered with `SOLIDWORKS_MCP_BRIDGE=1`, so they
  forward every tool call to the host. Claude Desktop always runs on the
  desktop and talks to SOLIDWORKS directly.
- On each start the host writes a new random token to
  `%LOCALAPPDATA%\solidworks-mcp\bridge.json`. Only this Windows account can
  read it — which is exactly why risk 1 applies to anyone who can log in as you.
- The host refuses to start from an SSH / service session (Session 0). If a
  host is already running, a second one exits and the running one keeps its
  settings — close it and start it again after re-running the installer.

To check from the SSH session: `claude mcp list` should show `solidworks`
connected. If a tool call says the bridge host is not reachable, start the host
on the desktop.

## execute_python

`solidworks_execute_python` runs any Python code the AI writes, inside the
server, with your Windows user's rights. It can reach things the dedicated
tools cannot, but it is not limited to SOLIDWORKS: it can read and write files,
use the network and start programs.

- **Off by default.** When it is off, the AI does not see the tool at all, and
  a direct call is refused.
- It is on only if the process that runs SOLIDWORKS commands was started with
  `SOLIDWORKS_MCP_ENABLE_PYTHON=1` — in Local mode the server registered in your
  AI app; in SSH mode the bridge host, whose launcher
  (`%LOCALAPPDATA%\SolidWorksMCP\start_bridge_host.cmd`) is written by the
  installer. Setting the variable on the SSH side cannot turn it on.
- When it is on, the bridge host prints a warning on the desktop, and every
  piece of code it runs is written to `solidworks_mcp.log`.

**What the switch protects against, and what it does not.** It stops the AI —
or a prompt injection in something the AI reads — from running arbitrary code
on this computer. It does **not** protect against a person who can already log
in to this Windows account (for example over SSH): they can run programs
anyway, including editing the launcher. Who can log in is controlled by your
Windows account and SSH settings, not by this project.

To turn it off, run the installer again and answer no.

## Manual install (for developers)

Requires [Python](https://www.python.org/downloads/) 3.10 or later and
[Git](https://git-scm.com/downloads/win).

```powershell
git clone https://github.com/ultima6-tw/solidworks-mcp.git
cd solidworks-mcp
python -m venv .venv
.venv\Scripts\pip install -e .
```

Register it (replace the paths):

```powershell
# Claude Code (every project)
claude mcp add -s user --transport stdio solidworks -- C:\path\to\solidworks-mcp\.venv\Scripts\python.exe C:\path\to\solidworks-mcp\server.py
# Codex / ChatGPT desktop
codex mcp add solidworks -- C:\path\to\solidworks-mcp\.venv\Scripts\python.exe C:\path\to\solidworks-mcp\server.py
```

For Claude Desktop, edit `%APPDATA%\Claude\claude_desktop_config.json`
(Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "solidworks": {
      "command": "C:\\path\\to\\solidworks-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\solidworks-mcp\\server.py"]
    }
  }
}
```

(Microsoft Store installs of Claude Desktop keep this file under
`%LOCALAPPDATA%\Packages\Claude...\LocalCache\Roaming\Claude`.)

For SSH mode by hand: add `-e SOLIDWORKS_MCP_BRIDGE=1` (Claude Code) or
`--env SOLIDWORKS_MCP_BRIDGE=1` (Codex) before the server name, and run
`bridge\start_host.cmd` on the desktop. Other settings:
`SOLIDWORKS_MCP_BRIDGE_PORT` (default 47110) and `SOLIDWORKS_MCP_BRIDGE_TIMEOUT`
(seconds per call, default 900).

## Uninstall

1. Delete `%LOCALAPPDATA%\SolidWorksMCP` (your saved parts are in its
   `app\workspace` folder — copy them first if you want them).
2. Remove the registrations: the `"solidworks"` entry in
   `claude_desktop_config.json`, `claude mcp remove solidworks -s user`,
   `codex mcp remove solidworks`.
3. SSH mode: delete the *SolidWorks MCP Bridge Host* shortcut from the Start
   menu and from `shell:startup`, and `%LOCALAPPDATA%\solidworks-mcp`.
