param(
    # Which AI apps to register with. Auto = every one that is detected.
    [ValidateSet('Auto', 'Claude', 'ClaudeDesktop', 'ClaudeCode', 'Codex', 'Both', 'All')]
    [string[]]$Client = 'Auto',

    # Local (default): the AI app runs on this Windows desktop and talks to SOLIDWORKS directly.
    # Ssh: you also use the AI app over SSH; a bridge host on the desktop does the SOLIDWORKS work.
    # Ask: ask on screen (falls back to Local when nobody can answer).
    [ValidateSet('Ask', 'Local', 'Ssh')]
    [string]$Mode = 'Ask',
    [switch]$AcceptSshRisk,

    # solidworks_execute_python lets the AI run any Python code on this computer. Off unless both are given.
    [switch]$EnablePython,
    [switch]$AcceptPythonRisk,

    # SSH mode only: start the bridge host when you log in to Windows.
    [ValidateSet('Ask', 'Yes', 'No')]
    [string]$HostAutoStart = 'Ask',

    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'SolidWorksMCP'),
    [string]$Repo = 'ultima6-tw/solidworks-mcp'
)

# SolidWorks MCP Server — Windows installer
#
# Installs everything needed to use the SolidWorks MCP server with Claude
# Desktop, Claude Code and/or Codex / ChatGPT desktop, with no Python or git
# prerequisite:
#
#   1. Asks how you will use it (Local or SSH) and whether to enable
#      solidworks_execute_python (default: no). Both risky choices need ACCEPT.
#   2. Installs uv (a fast Python manager) if it isn't already installed
#   3. Downloads the latest release of this project from GitHub
#   4. Creates an isolated Python environment and installs dependencies
#   5. Registers the server with detected or explicitly selected AI apps
#   6. SSH mode only: creates the "SolidWorks MCP Bridge Host" Start-menu
#      shortcut (and, if chosen, starts it when you log in)
#
# Usage (paste into PowerShell):
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/ultima6-tw/solidworks-mcp/main/scripts/install.ps1 | iex"
#
# Unattended, with parameters:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/ultima6-tw/solidworks-mcp/main/scripts/install.ps1))) -Mode Local
# SSH mode and execute_python are refused unless -AcceptSshRisk / -AcceptPythonRisk are also given.
#
# Re-running the script updates an existing install and replaces its settings.
# To uninstall: delete  %LOCALAPPDATA%\SolidWorksMCP , remove the "solidworks"
# entry from claude_desktop_config.json, run  claude mcp remove solidworks -s user
# and/or  codex mcp remove solidworks , and delete the "SolidWorks MCP Bridge Host"
# shortcuts from the Start menu (and its Startup folder) if you used SSH mode.
#
# Originally written by Joseph Robertson for HarrierPigeon/Solidworks-MCP-Server.

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$AppDir      = Join-Path $InstallDir 'app'
$VenvDir     = Join-Path $InstallDir 'venv'
$HostLauncher = Join-Path $InstallDir 'start_bridge_host.cmd'
$ShortcutName = 'SolidWorks MCP Bridge Host.lnk'
$ProgramsDir  = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$StartupDir   = Join-Path $ProgramsDir 'Startup'
$PythonFlag   = 'SOLIDWORKS_MCP_ENABLE_PYTHON'

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Note($msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Write-Risk($msg) { Write-Host $msg -ForegroundColor Red }

function Test-CanAsk {
    # Nobody can answer when input is redirected (scheduled task, CI, piped stdin).
    try { return [Environment]::UserInteractive -and -not [Console]::IsInputRedirected } catch { return $false }
}

function Find-ClaudeConfigDir {
    $classic = Join-Path $env:APPDATA 'Claude'
    if (Test-Path $classic) { return $classic }

    $packagesDir = Join-Path $env:LOCALAPPDATA 'Packages'
    if (Test-Path $packagesDir) {
        $msix = Get-ChildItem $packagesDir -Directory -Filter 'Claude*' -ErrorAction SilentlyContinue |
                Select-Object -First 1
        if ($msix) {
            $candidate = Join-Path $msix.FullName 'LocalCache\Roaming\Claude'
            if (Test-Path $candidate) { return $candidate }
        }
    }
    return $null
}

function Test-ClaudeInstalled {
    if (Find-ClaudeConfigDir) { return $true }
    return Test-Path (Join-Path $env:LOCALAPPDATA 'Programs\Claude\Claude.exe')
}

function Test-ClaudeCodeInstalled {
    return [bool](Get-Command claude -ErrorAction SilentlyContinue)
}

function Test-CodexInstalled {
    if (Get-Command codex -ErrorAction SilentlyContinue) { return $true }

    # The ChatGPT desktop app is a Codex host and shares Codex MCP settings.
    $packagesDir = Join-Path $env:LOCALAPPDATA 'Packages'
    if (Test-Path $packagesDir) {
        $chatGptPackage = Get-ChildItem $packagesDir -Directory -ErrorAction SilentlyContinue |
                          Where-Object { $_.Name -match 'OpenAI.*ChatGPT|ChatGPT.*OpenAI' } |
                          Select-Object -First 1
        if ($chatGptPackage) { return $true }
    }
    return Test-Path (Join-Path $env:LOCALAPPDATA 'ChatGPT')
}

$SshRisks = @'
  SSH mode lets an AI app that you run over SSH use SOLIDWORKS on this
  computer's desktop. Before you accept, understand that:

  1. Anyone who can log in to this Windows account over SSH can read the
     bridge token and call every SOLIDWORKS tool.
  2. The person at this desktop is affected: tools act on the SOLIDWORKS
     window they are using (close_document closes WITHOUT saving; the test
     suite closes all open documents first).
  3. If you also enable execute_python, the remote AI can run any program on
     this computer, including because of a prompt injection in what it reads.
  4. The screenshot tool sends the SOLIDWORKS window (including other open
     designs) to the remote AI service.
  5. Dialogs that SOLIDWORKS opens cannot be seen remotely: a call may hang
     until someone at the desktop answers, and that person can interfere.

  Recommended: do not use it on a computer someone else is working on, save
  your work before you start, and give SSH access only to people you trust.
'@

$PythonRisks = @'
  solidworks_execute_python lets the AI run ANY Python code on this computer,
  with your Windows user's rights (files, network, other programs), not only
  SOLIDWORKS commands. A prompt injection in a file or web page the AI reads
  could make it run harmful code. Every snippet is written to
  solidworks_mcp.log, but nothing stops it from running.

  The dedicated SOLIDWORKS tools are enough for normal modelling; enable this
  only if you know you need it. It does NOT protect against a person who can
  already log in to this Windows account - they can run programs anyway.
'@

Write-Host ''
Write-Host 'SolidWorks MCP Server installer' -ForegroundColor White
Write-Host '-------------------------------'

# --- 0. Choices (before anything is downloaded or changed) -----------------
$canAsk = Test-CanAsk
$asked = $false

if ($Mode -eq 'Ask') {
    if ($canAsk) {
        $asked = $true
        Write-Host ''
        Write-Host 'How will you use the AI app with SOLIDWORKS?'
        Write-Host '  [1] Local - the AI app runs on this Windows desktop (recommended)'
        Write-Host '  [2] SSH   - you also run the AI app over SSH from another computer'
        $answer = Read-Host 'Choose 1 or 2 [1]'
        $Mode = if ($answer.Trim() -eq '2') { 'Ssh' } else { 'Local' }
    } else {
        $Mode = 'Local'
        Write-Note 'Nobody can answer questions here; using Local mode (pass -Mode to choose).'
    }
}

if ($Mode -eq 'Ssh') {
    Write-Host ''
    Write-Risk $SshRisks
    if ($AcceptSshRisk) {
        Write-Ok 'SSH risks accepted with -AcceptSshRisk.'
    } elseif ($asked) {
        $answer = Read-Host 'Type ACCEPT to use SSH mode (anything else: choose Local or quit)'
        if ($answer.Trim() -ceq 'ACCEPT') {
            Write-Ok 'SSH risks accepted.'
        } else {
            $answer = Read-Host 'Not accepted. [L] install in Local mode instead, [Q] quit [L]'
            if ($answer.Trim() -match '^[Qq]') { Write-Host 'Nothing was installed.'; return }
            $Mode = 'Local'
        }
    } else {
        throw 'SSH mode needs -AcceptSshRisk after you have read the risks above. Nothing was installed.'
    }
}

$enablePythonTool = $false
if ($EnablePython) {
    Write-Host ''
    Write-Risk $PythonRisks
    if (-not $AcceptPythonRisk) {
        throw 'execute_python needs -AcceptPythonRisk after you have read the risks above. Nothing was installed.'
    }
    $enablePythonTool = $true
    Write-Ok 'execute_python risks accepted with -AcceptPythonRisk.'
} elseif ($asked) {
    Write-Host ''
    $answer = Read-Host 'Enable solidworks_execute_python (the AI can run any Python code)? [y/N]'
    if ($answer.Trim() -match '^[Yy]') {
        Write-Risk $PythonRisks
        $answer = Read-Host 'Type ACCEPT to enable it (anything else: keep it off)'
        $enablePythonTool = $answer.Trim() -ceq 'ACCEPT'
    }
}

$autoStart = $false
if ($Mode -eq 'Ssh') {
    if ($HostAutoStart -eq 'Yes') { $autoStart = $true }
    elseif ($HostAutoStart -eq 'Ask' -and $asked) {
        Write-Host ''
        Write-Host 'Over SSH you cannot start programs on the desktop yourself.'
        $answer = Read-Host 'Start the bridge host automatically when you log in to Windows? [Y/n]'
        $autoStart = -not ($answer.Trim() -match '^[Nn]')
    }
}

$installClaude = $false; $installClaudeCode = $false; $installCodex = $false
foreach ($c in $Client) {
    switch ($c) {
        'Claude'        { $installClaude = $true }
        'ClaudeDesktop' { $installClaude = $true }
        'ClaudeCode'    { $installClaudeCode = $true }
        'Codex'         { $installCodex = $true }
        'Both'          { $installClaude = $true; $installCodex = $true }
        'All'           { $installClaude = $true; $installClaudeCode = $true; $installCodex = $true }
    }
}
if ($Client -contains 'Auto') {
    $installClaude = Test-ClaudeInstalled
    $installClaudeCode = Test-ClaudeCodeInstalled
    $installCodex = Test-CodexInstalled
}
$detected = @()
if ($installClaude) { $detected += 'Claude Desktop' }
if ($installClaudeCode) { $detected += 'Claude Code' }
if ($installCodex) { $detected += 'Codex / ChatGPT desktop' }

Write-Host ''
Write-Step 'Settings'
Write-Ok "Mode: $Mode"
Write-Ok ("execute_python: " + $(if ($enablePythonTool) { 'ENABLED' } else { 'off' }))
if ($Mode -eq 'Ssh') { Write-Ok ("Bridge host starts at login: " + $(if ($autoStart) { 'yes' } else { 'no' })) }
if ($detected.Count) {
    Write-Ok "Register with: $($detected -join ', ')"
} else {
    Write-Note 'No supported AI app was detected; the server will be installed without'
    Write-Note 'changing any app configuration. Re-run with -Client ClaudeDesktop, ClaudeCode, Codex or All.'
}

# --- 1. Ensure uv ----------------------------------------------------------
Write-Step 'Checking for uv...'
$uv = $null
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCmd) { $uv = $uvCmd.Source }
if (-not $uv) {
    Write-Step 'Installing uv (Python environment manager)...'
    try {
        winget install --id=astral-sh.uv -e --silent --accept-source-agreements --accept-package-agreements | Out-Null
    } catch {}
    # Probe the places uv lands (PATH updates don't reach this session)
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe'),
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe')
    )
    foreach ($c in $candidates) { if (-not $uv -and (Test-Path $c)) { $uv = $c } }
    if (-not $uv) {
        # winget unavailable or failed — use Astral's official installer
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $c = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
        if (Test-Path $c) { $uv = $c }
    }
    if (-not $uv) { throw 'Could not install uv. Install it manually from https://docs.astral.sh/uv/ and re-run.' }
}
Write-Ok "uv: $uv"

# --- 2. Download the server ------------------------------------------------
Write-Step 'Downloading SolidWorks MCP Server...'
$zipUrl = "https://github.com/$Repo/archive/refs/heads/main.zip"
try {
    $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
    if ($release.zipball_url) {
        $zipUrl = $release.zipball_url
        Write-Ok "Latest release: $($release.tag_name)"
    }
} catch {
    Write-Ok 'No published release found; using latest main branch.'
}

$tmpZip = Join-Path $env:TEMP 'solidworks-mcp.zip'
$tmpExtract = Join-Path $env:TEMP 'solidworks-mcp-extract'
Invoke-WebRequest $zipUrl -OutFile $tmpZip -UseBasicParsing
if (Test-Path $tmpExtract) { Remove-Item $tmpExtract -Recurse -Force }
Expand-Archive $tmpZip -DestinationPath $tmpExtract

# The zip contains a single versioned root folder — find it by pyproject.toml
$srcDir = Get-ChildItem $tmpExtract -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'pyproject.toml') } | Select-Object -First 1
if (-not $srcDir) { throw 'Downloaded archive did not contain the expected project files.' }

New-Item -ItemType Directory -Force $InstallDir | Out-Null
# Preserve the user's saved parts (app\workspace) across updates
$workspaceKeep = $null
if (Test-Path (Join-Path $AppDir 'workspace')) {
    $workspaceKeep = Join-Path $InstallDir 'workspace-keep'
    if (Test-Path $workspaceKeep) { Remove-Item $workspaceKeep -Recurse -Force }
    Move-Item (Join-Path $AppDir 'workspace') $workspaceKeep
}
if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force }
Move-Item $srcDir.FullName $AppDir
if ($workspaceKeep) { Move-Item $workspaceKeep (Join-Path $AppDir 'workspace') }
Remove-Item $tmpZip -Force
Remove-Item $tmpExtract -Recurse -Force
Write-Ok "Installed to: $AppDir"

# --- 3. Python environment -------------------------------------------------
Write-Step 'Setting up Python environment (uv downloads Python if needed)...'
if (Test-Path $VenvDir) { Remove-Item $VenvDir -Recurse -Force }
& $uv venv $VenvDir --python 3.12
if ($LASTEXITCODE -ne 0) { throw 'Failed to create the Python environment.' }
$venvPython = Join-Path $VenvDir 'Scripts\python.exe'
& $uv pip install --python $venvPython $AppDir
if ($LASTEXITCODE -ne 0) { throw 'Failed to install Python dependencies.' }

# Sanity check the imports that matter (pywin32 COM, MCP SDK, Pillow, ezdxf)
& $venvPython -c "import win32com.client, mcp, PIL, ezdxf"
if ($LASTEXITCODE -ne 0) { throw 'Environment check failed — dependencies did not import cleanly.' }
Write-Ok 'Python environment ready.'

# --- 4. Register with selected AI apps -------------------------------------
$serverPath = Join-Path $AppDir 'server.py'

# Environment for the MCP server process. Claude Desktop always runs on the
# desktop, so it talks to SOLIDWORKS directly. Claude Code and Codex may run
# over SSH, so in SSH mode they go through the bridge host.
$desktopEnv = [ordered]@{}
$remoteEnv = [ordered]@{}
if ($Mode -eq 'Ssh') { $remoteEnv['SOLIDWORKS_MCP_BRIDGE'] = '1' }
if ($enablePythonTool) {
    # In bridge mode the tool LIST comes from this process, but the code runs
    # in the bridge host, which refuses unless its own launcher enables it.
    $desktopEnv[$PythonFlag] = '1'
    $remoteEnv[$PythonFlag] = '1'
}

if ($installClaude) {
    Write-Step 'Registering with Claude Desktop...'
    $cfgDir = Find-ClaudeConfigDir
    if (-not $cfgDir) {
        # Explicit selection may create the conventional location. Auto mode
        # reaches here only after finding credible evidence of an installation.
        $cfgDir = Join-Path $env:APPDATA 'Claude'
        New-Item -ItemType Directory -Force $cfgDir | Out-Null
        Write-Note "Claude config folder was not found; created: $cfgDir"
    }

    $cfgPath = Join-Path $cfgDir 'claude_desktop_config.json'
    $config = $null
    if (Test-Path $cfgPath) {
        Copy-Item $cfgPath "$cfgPath.backup" -Force
        Write-Ok 'Backed up existing config to claude_desktop_config.json.backup'
        $raw = Get-Content $cfgPath -Raw
        if ($raw -and $raw.Trim()) { $config = $raw | ConvertFrom-Json }
    }
    if (-not $config) { $config = [pscustomobject]@{} }
    if (-not ($config.PSObject.Properties.Name -contains 'mcpServers') -or -not $config.mcpServers) {
        $config | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) -Force
    }

    $serverEntry = [pscustomobject]@{
        command = $venvPython
        args    = @($serverPath)
    }
    if ($desktopEnv.Count) {
        $serverEntry | Add-Member -NotePropertyName env -NotePropertyValue ([pscustomobject]$desktopEnv)
    }
    $config.mcpServers | Add-Member -NotePropertyName solidworks -NotePropertyValue $serverEntry -Force

    # BOM-less UTF-8: PS 5.1's Out-File -Encoding utf8 writes a BOM, which
    # some JSON parsers reject.
    [IO.File]::WriteAllText($cfgPath, ($config | ConvertTo-Json -Depth 20),
                            (New-Object System.Text.UTF8Encoding $false))
    Write-Ok "Updated: $cfgPath"
}

if ($installClaudeCode) {
    Write-Step 'Registering with Claude Code (user scope: every project)...'
    $claudeCmd = Get-Command claude -ErrorAction SilentlyContinue
    if ($claudeCmd) {
        # `claude mcp add` refuses an existing name, so replace this entry.
        & $claudeCmd.Source mcp get solidworks *> $null
        if ($LASTEXITCODE -eq 0) {
            & $claudeCmd.Source mcp remove solidworks -s user *> $null
        }
        $addArgs = @('mcp', 'add', '-s', 'user')
        foreach ($k in $remoteEnv.Keys) { $addArgs += @('-e', "$k=$($remoteEnv[$k])") }
        # --transport must sit between --env and the name (see `claude mcp add --help`)
        $addArgs += @('--transport', 'stdio', 'solidworks', '--', $venvPython, $serverPath)
        & $claudeCmd.Source @addArgs
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to register the server with Claude Code (an entry named "solidworks" may exist in another scope: claude mcp list).'
        }
        Write-Ok 'Registered Claude Code MCP server: solidworks'
    } else {
        Write-Note 'Claude Code (claude) was not found on PATH, so registration was skipped.'
    }
}

if ($installCodex) {
    Write-Step 'Registering with Codex / ChatGPT desktop...'
    $codexCmd = Get-Command codex -ErrorAction SilentlyContinue
    if ($codexCmd) {
        # `codex mcp add` refuses duplicate names, so replace this entry on
        # reruns before adding its current installed paths.
        $codexConfig = Join-Path $env:USERPROFILE '.codex\config.toml'
        $codexBackup = "$codexConfig.solidworks-mcp.backup"
        if (Test-Path $codexConfig) {
            Copy-Item $codexConfig $codexBackup -Force
            Write-Ok 'Backed up existing Codex config to config.toml.solidworks-mcp.backup'
        }
        & $codexCmd.Source mcp get solidworks *> $null
        if ($LASTEXITCODE -eq 0) {
            & $codexCmd.Source mcp remove solidworks
            if ($LASTEXITCODE -ne 0) { throw 'Failed to replace the existing Codex MCP entry.' }
        }
        $addArgs = @('mcp', 'add', 'solidworks')
        foreach ($k in $remoteEnv.Keys) { $addArgs += @('--env', "$k=$($remoteEnv[$k])") }
        $addArgs += @('--', $venvPython, $serverPath)
        & $codexCmd.Source @addArgs
        if ($LASTEXITCODE -ne 0) {
            if (Test-Path $codexBackup) {
                Copy-Item $codexBackup $codexConfig -Force
                Write-Note 'Registration failed; restored the previous Codex configuration.'
            }
            throw 'Failed to register the server with Codex.'
        }
        Write-Ok 'Registered Codex MCP server: solidworks'
    } else {
        Write-Note 'Codex CLI was not found on PATH, so automatic registration was skipped.'
        Write-Note 'In ChatGPT desktop, use Settings > MCP servers > Add server > STDIO.'
        Write-Note "Command: $venvPython"
        Write-Note "Arguments: $serverPath"
        foreach ($k in $remoteEnv.Keys) { Write-Note "Environment: $k=$($remoteEnv[$k])" }
    }
}

# --- 5. Bridge host (SSH mode) ---------------------------------------------
$programsLink = Join-Path $ProgramsDir $ShortcutName
$startupLink  = Join-Path $StartupDir $ShortcutName

function New-HostShortcut($path) {
    New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $HostLauncher
    $lnk.WorkingDirectory = $AppDir
    $lnk.Description = 'Runs SOLIDWORKS tool calls for AI apps used over SSH. Keep it open on the desktop.'
    $lnk.Save()
}

if ($Mode -eq 'Ssh') {
    Write-Step 'Setting up the bridge host (runs on this desktop)...'
    # This launcher is the ONLY place the bridge host reads the
    # execute_python switch from; the SSH side cannot turn it on.
    $pyLine = if ($enablePythonTool) { "set `"$PythonFlag=1`"" } else { "set `"$PythonFlag=`"" }
    $launcher = @"
@echo off
rem Generated by the SolidWorks MCP installer. Re-run the installer to change these settings.
set "SOLIDWORKS_MCP_PYTHON=$venvPython"
$pyLine
call "$AppDir\bridge\start_host.cmd" %*
"@
    [IO.File]::WriteAllText($HostLauncher, $launcher, (New-Object System.Text.UTF8Encoding $false))
    New-HostShortcut $programsLink
    Write-Ok "Start menu: SolidWorks MCP Bridge Host"
    if ($autoStart) {
        New-HostShortcut $startupLink
        Write-Ok 'Starts automatically when you log in.'
    } elseif (Test-Path $startupLink) {
        Remove-Item $startupLink -Force
        Write-Ok 'Removed the old automatic start at login.'
    }
} else {
    # Local mode: remove what an earlier SSH-mode install created.
    foreach ($p in @($HostLauncher, $programsLink, $startupLink)) {
        if (Test-Path $p) { Remove-Item $p -Force; Write-Ok "Removed from an earlier SSH-mode install: $p" }
    }
}

# --- Done ------------------------------------------------------------------
Write-Host ''
Write-Host 'Done! Final steps:' -ForegroundColor White
$step = 1
if ($installClaude) {
    Write-Host "  $step. Restart Claude Desktop completely (File -> Exit, then reopen)."
    $step++
}
if ($installClaudeCode) {
    Write-Host "  $step. Start a new Claude Code session (claude mcp list shows 'solidworks')."
    $step++
}
if ($installCodex) {
    Write-Host "  $step. Restart ChatGPT desktop, Codex CLI, or the Codex IDE extension."
    $step++
}
if ($Mode -eq 'Ssh') {
    $how = if ($autoStart) { 'log in again, or start' } else { 'start' }
    Write-Host "  $step. On this desktop, $how 'SolidWorks MCP Bridge Host' from the Start menu and keep it open."
    $step++
}
Write-Host "  $step. Make sure SolidWorks is installed. Starting it before your AI app helps."
$step++
Write-Host "  $step. Ask: `"Create a 50mm cube in SolidWorks`"."
if ($enablePythonTool) {
    Write-Host ''
    Write-Note 'execute_python is ENABLED. Re-run the installer without it to turn it off.'
}
Write-Host ''
