# 安裝說明

[English](INSTALL.md)

## 需求

- **Windows**，並已安裝、啟用 **SOLIDWORKS**。這個 fork 只在 SOLIDWORKS 2022 SP04 繁體中文介面
  測試過（見 [TESTING.zh-TW.md](TESTING.zh-TW.md)）；原作者的版本是在 SOLIDWORKS 2025 測試的。
- 支援本機 MCP server 的 AI 程式：**Claude Desktop**、**Claude Code**，或 **Codex / ChatGPT 桌面版**。

Python 和其他所有東西都由安裝程式處理。

## 選哪種模式？

| 模式 | 適用情況 | |
|---|---|---|
| **本機（Local）** | AI 程式和 SOLIDWORKS 在同一台 Windows 桌面上執行。 | **預設。除非確定需要 SSH，否則請選這個。** |
| **SSH** | 你也會從別台電腦用 SSH 連進這台電腦，在 SSH 裡執行 AI 程式（Claude Code、Codex CLI）。 | 請先讀 [SSH 模式](#ssh-模式)：它有本機模式沒有的風險。 |

## 安裝

打開 **PowerShell**（按 `Win`，輸入「PowerShell」，按 Enter），貼上：

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/ultima6-tw/solidworks-mcp/main/scripts/install.ps1 | iex"
```

安裝程式在下載任何東西之前會先問：

1. **本機或 SSH**：直接按 Enter 就是本機。選 SSH 會先列出風險，要輸入 `ACCEPT` 才會繼續；
   不接受的話可以改成本機模式安裝，或結束。
2. **要不要啟用 `solidworks_execute_python`？**：直接按 Enter 就是不啟用（建議）。
   選要的話會列出風險，輸入 `ACCEPT` 才會啟用。見 [execute_python](#execute_python)。
3. *（只有 SSH 模式）* **登入 Windows 時自動啟動 bridge host？**：直接按 Enter 就是要。

接著它會安裝 [uv](https://docs.astral.sh/uv/)（由它提供 Python），把這個專案的最新版本下載到
`%LOCALAPPDATA%\SolidWorksMCP`，並登記到偵測到的每個 AI 程式。把 AI 程式完全關掉再重開，
然後請它「在 SolidWorks 畫一個 50 mm 的立方體」。

要**更新**或**改變當初的選擇**，再執行一次同樣的指令即可。

### 無人值守安裝

用參數代替回答問題：

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/ultima6-tw/solidworks-mcp/main/scripts/install.ps1))) -Mode Local
```

| 參數 | 值 | 預設 |
|---|---|---|
| `-Mode` | `Ask`、`Local`、`Ssh` | `Ask`（沒人能回答時用 `Local`） |
| `-AcceptSshRisk` | 開關 | 搭配 `-Mode Ssh` 必須給，否則安裝程式拒絕 |
| `-EnablePython` | 開關 | 不啟用 |
| `-AcceptPythonRisk` | 開關 | 搭配 `-EnablePython` 必須給，否則安裝程式拒絕 |
| `-HostAutoStart` | `Ask`、`Yes`、`No` | `Ask`（沒人能回答時用 `No`） |
| `-Client` | `Auto`、`ClaudeDesktop`、`ClaudeCode`、`Codex`、`All`（也接受原版安裝程式的 `Claude`、`Both`） | `Auto`（偵測到的每個程式） |
| `-InstallDir` | 資料夾 | `%LOCALAPPDATA%\SolidWorksMCP` |

有風險的選項被拒絕時，什麼都不會下載、不會修改。

## SSH 模式

### 風險：接受之前請先讀

SSH 模式讓你在 SSH 裡執行的 AI 程式，可以操作這台電腦桌面上的 SOLIDWORKS：

1. **任何能用 SSH 登入這個 Windows 帳號的人**，都讀得到 bridge 的 token，可以呼叫所有
   SOLIDWORKS 工具。
2. **坐在這台電腦前的人會受影響**：工具操作的就是他正在用的 SOLIDWORKS 視窗。
   `close_document` 會**不存檔**就關閉，測試套件也會先關掉所有開著的文件。
3. **如果同時啟用 `execute_python`**，遠端的 AI 就能在這台電腦上執行任何程式，
   包括被 AI 讀到的檔案或網頁裡的 prompt injection 誘導而執行。
4. **`screenshot` 會把 SOLIDWORKS 視窗**（包含其他開著的設計圖）傳給遠端的 AI 服務。
5. **遠端看不到對話框**：一個呼叫可能卡住，直到有人在桌面上回應；桌面前的人也可能干擾 AI 正在做的事。

建議：不要在別人正在使用的電腦上用、開始前先存檔、SSH 權限只給信任的人。

### 運作方式

從 SSH 啟動的程式在另一個 Windows session 執行，碰不到桌面上的 SOLIDWORKS。所以：

- 桌面上要執行一個 **bridge host**（開始功能表：*SolidWorks MCP Bridge Host*，視窗要保持開著）。
  它只聽 `127.0.0.1`，所有 SOLIDWORKS 操作都由它一次一個執行。
- Claude Code 和 Codex 會用 `SOLIDWORKS_MCP_BRIDGE=1` 登記，把每個工具呼叫轉給 host。
  Claude Desktop 本來就在桌面上執行，一律直接操作 SOLIDWORKS。
- host 每次啟動會把一個新的隨機 token 寫到 `%LOCALAPPDATA%\solidworks-mcp\bridge.json`。
  只有這個 Windows 帳號讀得到——這正是風險 1 會套用到「任何能以你的身分登入的人」的原因。
- host 拒絕從 SSH／服務的 session（Session 0）啟動。如果已經有一個 host 在跑，第二個會直接結束，
  由正在跑的那個保留它原本的設定——重跑安裝程式後，要把舊的 host 關掉再重新啟動。

在 SSH 裡檢查：`claude mcp list` 應該顯示 `solidworks` 已連線。如果工具呼叫回報 bridge host
連不到，請到桌面上啟動 host。

## execute_python

`solidworks_execute_python` 會在 server 裡、以你的 Windows 使用者權限，執行 AI 寫的任何 Python
程式碼。它能做到專用工具做不到的事，但它不限於 SOLIDWORKS：可以讀寫檔案、連網路、啟動程式。

- **預設關閉**。關閉時 AI 完全看不到這個工具，硬叫也會被拒絕。
- 只有「實際執行 SOLIDWORKS 指令的那個程式」啟動時帶著 `SOLIDWORKS_MCP_ENABLE_PYTHON=1`，
  它才會啟用——本機模式是登記在 AI 程式裡的 server；SSH 模式是 bridge host，它的啟動檔
  （`%LOCALAPPDATA%\SolidWorksMCP\start_bridge_host.cmd`）由安裝程式寫入。在 SSH 那端設定這個
  變數沒辦法打開它。
- 啟用時，bridge host 會在桌面視窗印出警告，執行過的每段程式碼都會記在 `solidworks_mcp.log`。

**這個開關防得住什麼、防不住什麼。** 它防止 AI——或 AI 讀到的內容裡的 prompt injection——在這台
電腦上執行任意程式碼。它**防不住**本來就能登入這個 Windows 帳號的人（例如透過 SSH）：那個人本來
就能執行任何程式，包括直接修改啟動檔。誰能登入，是由你的 Windows 帳號和 SSH 設定決定，
不是這個專案能管的。

要關掉它，重跑安裝程式並回答不要。

## 手動安裝（開發者）

需要 [Python](https://www.python.org/downloads/) 3.10 以上和 [Git](https://git-scm.com/downloads/win)。

```powershell
git clone https://github.com/ultima6-tw/solidworks-mcp.git
cd solidworks-mcp
python -m venv .venv
.venv\Scripts\pip install -e .
```

登記（請換成實際路徑）：

```powershell
# Claude Code（所有專案都能用）
claude mcp add -s user --transport stdio solidworks -- C:\path\to\solidworks-mcp\.venv\Scripts\python.exe C:\path\to\solidworks-mcp\server.py
# Codex / ChatGPT 桌面版
codex mcp add solidworks -- C:\path\to\solidworks-mcp\.venv\Scripts\python.exe C:\path\to\solidworks-mcp\server.py
```

Claude Desktop 要編輯 `%APPDATA%\Claude\claude_desktop_config.json`
（Settings → Developer → Edit Config）：

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

（從 Microsoft Store 安裝的 Claude Desktop，這個檔案在
`%LOCALAPPDATA%\Packages\Claude...\LocalCache\Roaming\Claude`。）

手動設定 SSH 模式：在 server 名稱前加上 `-e SOLIDWORKS_MCP_BRIDGE=1`（Claude Code）或
`--env SOLIDWORKS_MCP_BRIDGE=1`（Codex），並在桌面上執行 `bridge\start_host.cmd`。
其他設定：`SOLIDWORKS_MCP_BRIDGE_PORT`（預設 47110）、`SOLIDWORKS_MCP_BRIDGE_TIMEOUT`
（每次呼叫的秒數上限，預設 900）。

## 解除安裝

1. 刪除 `%LOCALAPPDATA%\SolidWorksMCP`（存過的零件在裡面的 `app\workspace`，需要的話先複製出來）。
2. 移除登記：`claude_desktop_config.json` 裡的 `"solidworks"` 項目、
   `claude mcp remove solidworks -s user`、`codex mcp remove solidworks`。
3. SSH 模式：刪除開始功能表和 `shell:startup` 裡的 *SolidWorks MCP Bridge Host* 捷徑，
   以及 `%LOCALAPPDATA%\solidworks-mcp`。
