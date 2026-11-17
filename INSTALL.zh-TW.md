# 安裝說明

[English](INSTALL.md)

這個專案沒有安裝程式。把這一頁交給你的 AI（Claude Code、Codex 等），請它幫你裝，例如：

> 請閱讀 https://github.com/ultima6-tw/solidworks-mcp/blob/main/INSTALL.zh-TW.md ，
> 幫我在這台電腦上裝好這個 SolidWorks MCP server。

要怎麼在你的電腦上安裝，由 AI 自己判斷。這一頁提供它需要知道的事實、必須留給你決定的選擇，
以及怎麼確認裝好了。你也可以自己照著做。

## 給負責安裝的 AI 的規則

1. **問使用者，不要替他決定。** 有兩個選擇會改變這個 server 能在他電腦上做的事：
   [本機或 SSH](#本機或-ssh) 和 [execute_python](#execute_python)。把下面的風險原文給他看；
   除非他明確選擇，否則一律用安全的預設值（本機、execute_python 關閉）。
2. **動手前先說明計畫**：檔案要放哪裡、用哪個 Python、要改哪些設定檔。改設定檔之前先備份，
   並保留檔案裡的其他設定。
3. **不要關閉或修改使用者的 SOLIDWORKS 文件。** 下面的檢查會自己建一個零件。**安裝時不要跑
   `test.py`**：它是開發這個專案用的，一開始就會**不存檔**關掉**所有**開著的文件。
4. **確認結果**：用[下面的檢查](#確認安裝結果)，回報哪些通過、哪些沒有。

## 需求

- **Windows**，並已安裝、啟用 **SOLIDWORKS**。這個 fork 只在 SOLIDWORKS 2022 SP04 繁體中文介面
  上測試過（見 [TESTING.zh-TW.md](TESTING.zh-TW.md)）；原版專案是在 SOLIDWORKS 2025 上測試的。
- **Python 3.10 以上**，64 位元。測試過 3.12 和 3.13。
- 支援本機 stdio MCP server 的 AI 程式：Claude Desktop、Claude Code、Codex，或其他 MCP client。

## 安裝程式碼

從 https://github.com/ultima6-tw/solidworks-mcp 取得程式碼（git clone，或下載 `main` 的 ZIP），
把相依套件裝進虛擬環境，例如：

```powershell
git clone https://github.com/ultima6-tw/solidworks-mcp.git
cd solidworks-mcp
python -m venv .venv
.venv\Scripts\pip install -e .
```

用其他方式（uv、放在別的資料夾）都可以。重點是：

- **`mcp` 必須低於 2**（`mcp>=1.26,<2`，跟 `pyproject.toml`、`requirements.txt` 一樣）。
  裝到 mcp 2.x 的話，server 一啟動就會壞掉（`'Server' object has no attribute 'list_tools'`）。
- server 的啟動方式是 `<那個環境的 python> <完整路徑>\server.py`。它的工作檔案（存檔的零件、
  截圖、DXF）放在 `server.py` 旁邊的 `workspace\`；log 是同一個資料夾的 `solidworks_mcp.log`。

## 登記到 AI 程式

路徑都用完整路徑。登記完要重新啟動 AI 程式。

**Claude Code**——選項要放在 server 名稱**前面**：

```powershell
claude mcp add -s user --transport stdio solidworks -- C:\path\to\.venv\Scripts\python.exe C:\path\to\server.py
```

環境變數用 `-e NAME=value`，一樣放在名稱前面。`claude mcp get solidworks` 可以查看登記內容。

**Codex／ChatGPT 桌面版：**

```powershell
codex mcp add solidworks -- C:\path\to\.venv\Scripts\python.exe C:\path\to\server.py
```

環境變數用 `--env NAME=value`，放在 `--` 前面。

**Claude Desktop**——在 `claude_desktop_config.json` 的 `mcpServers` 底下加一項
（設定 → 開發者 → 編輯設定）。檔案位置是 `%APPDATA%\Claude\claude_desktop_config.json`；
Microsoft Store 版則是
`%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json`。
檔案裡的其他內容要保留。

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

Claude Desktop 要完全重新啟動（檔案 → 結束，再重新打開）。

## 本機或 SSH

| 模式 | 什麼時候用 | |
|---|---|---|
| **本機** | AI 程式跟 SOLIDWORKS 在同一個 Windows 桌面上執行。 | **預設。** 不用再設定別的。 |
| **SSH** | AI 程式（Claude Code、Codex CLI）在 SSH 連進這台電腦的 session 裡執行。 | 先把下面的風險給使用者看。 |

怎麼判斷：透過 SSH（或以服務方式）啟動的程式，跑在 Windows 的 **Session 0**。從那裡 COM 連不到
桌面上的 SOLIDWORKS，所以本機模式在 SSH session 裡不能用。在 PowerShell 裡，
`(Get-Process -Id $PID).SessionId` 在 SSH session 會印出 `0`。

### SSH 模式的風險——要給使用者看

SSH 模式讓透過 SSH 執行的 AI 程式，可以使用這台電腦桌面上的 SOLIDWORKS：

1. **任何能用 SSH 登入這個 Windows 帳號的人**，都能讀到 bridge 的 token，並呼叫所有
   SOLIDWORKS 工具。
2. **會影響坐在這台電腦前的人**：工具操作的是他正在用的 SOLIDWORKS 視窗。`close_document`
   會**不存檔**直接關閉，測試程式也會先關掉所有開著的文件。
3. **如果同時開啟 execute_python**，遠端的 AI 就能在這台電腦上執行任何程式——包括因為它讀到的
   檔案或網頁裡有 prompt injection。
4. **`screenshot` 會把 SOLIDWORKS 視窗**（包括其他開著的設計圖）傳給遠端的 AI 服務。
5. **遠端看不到對話框**：呼叫可能會卡住，直到有人在電腦前按掉對話框；而電腦前的人也可能干擾
   AI 正在做的事。

建議：不要在別人正在使用的電腦上用；開始前先存檔；只給信任的人 SSH 權限。

### SSH 模式怎麼運作

- 桌面上執行一個 **bridge host**，由它做所有 SOLIDWORKS 操作，一次處理一個呼叫。它只監聽
  `127.0.0.1`（port 47110，或 `SOLIDWORKS_MCP_BRIDGE_PORT`）。
- 要在**桌面上**啟動，不能從 SSH 啟動：執行 `bridge\start_host.cmd`（有設定
  `SOLIDWORKS_MCP_PYTHON` 就用它，否則用程式碼旁邊的 `.venv`，再不然用 `python`），視窗要保持
  開著。它拒絕在 Session 0 啟動。要登入時自動啟動，就在 `shell:startup` 放一個它的捷徑。
  如果已經有 host 在跑，新開的會直接結束，由正在跑的那個保留原本的設定。
- host 每次啟動都會產生一個新的隨機 token，寫到 `%LOCALAPPDATA%\solidworks-mcp\bridge.json`，
  只有這個 Windows 帳號讀得到——這也是為什麼風險 1 適用於任何能用你的帳號登入的人。
- SSH 那端的 AI 程式登記時加上 `SOLIDWORKS_MCP_BRIDGE=1`，之後每個工具呼叫都會轉給 host。
  `SOLIDWORKS_MCP_BRIDGE_TIMEOUT` 設定每個呼叫最多等幾秒（預設 900）。
- 確認方式：`claude mcp list`（或 Codex 的對應指令）顯示 `solidworks` 已連線。如果工具呼叫說
  連不到 bridge host，就是桌面上的 host 沒有在跑。

## execute_python

`solidworks_execute_python` 會在 server 裡、用這個 Windows 使用者的權限，執行 AI 寫的任何
Python 程式碼。它能做到專用工具做不到的事，但不限於 SOLIDWORKS：它能讀寫檔案、連網路、
啟動程式。**除非使用者讀完這一節後主動要求，否則不要開啟。**

- **預設關閉。** 關閉時 AI 看不到這個工具，直接呼叫也會被拒絕。
- 要開啟，就在執行 SOLIDWORKS 指令的那個程序設定 `SOLIDWORKS_MCP_ENABLE_PYTHON=1`：本機模式是
  登記在 AI 程式裡的 server；SSH 模式是 bridge host（在 `start_host.cmd` 之前設定），而且 SSH
  那端的登記也要設，工具才會出現在清單上。host 沒設的話，呼叫會在 host 那邊被拒絕。
- 開啟時，bridge host 會在桌面上顯示警告，而且每段程式碼都會記錄到 `solidworks_mcp.log`。

**這個開關防得了什麼、防不了什麼。** 它能防止 AI——或 AI 讀到的內容裡的 prompt injection——
在這台電腦上執行任意程式碼。它**防不了**已經能登入這個 Windows 帳號的人（例如透過 SSH）：
那個人本來就能執行程式。誰能登入是由 Windows 帳號和 SSH 設定控制的，不是這個專案。

## 確認安裝結果

先打開 SOLIDWORKS（不然第一個呼叫會去啟動它，比較慢）。然後在 AI 程式裡確認：

1. **工具有列出來**：大約 104 個名稱以 `solidworks_` 開頭的工具；只有開啟時才會有
   `solidworks_execute_python`。
2. **建模正常**：建一個 50 mm 立方體（Front 平面草圖、中心在原點、伸長 50 mm），查質量屬性。
   體積必須是 125,000 mm³。
3. **截圖正常**：`solidworks_screenshot` 會存一張 SOLIDWORKS 視窗的 PNG，畫面上看得到立方體。
4. **匯出檔案正常**：對 z = 50 那一面執行 `solidworks_export_dxf`，回傳 4 個 LINE，
   範圍 (0, 0)–(50, 50) mm。
5. 用 `solidworks_close_document` 關掉立方體（不會存檔）。

做完這些檢查就夠了。這個專案本身已經測試過（見 [TESTING.zh-TW.md](TESTING.zh-TW.md)），
安裝時不需要再跑它的測試程式。

**如果某個工具呼叫一直沒有回應**，看 `solidworks_mcp.log`：如果最後一行是那個工具被呼叫，
先看桌面上有沒有 SOLIDWORKS 的對話框。SSH 模式還要確認 bridge host 的視窗開著。

## 解除安裝

1. 移除登記：`claude mcp remove solidworks -s user`、`codex mcp remove solidworks`，或刪掉
   `claude_desktop_config.json` 裡的 `"solidworks"` 項目。
2. 刪除程式碼所在的資料夾（存檔的零件在它的 `workspace` 資料夾裡——要的話先複製出來）。
3. SSH 模式：刪除 `shell:startup` 裡的 bridge host 捷徑，以及 `%LOCALAPPDATA%\solidworks-mcp`。
