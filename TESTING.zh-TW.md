# 測試說明

[English](TESTING.md)

這個專案沒有模擬 SOLIDWORKS COM 的假物件，每個測試都是實際操作一個正在執行的
SOLIDWORKS。本文說明怎麼跑測試、測了哪些東西、在什麼環境測過，以及已知不穩定的地方。

## 測試環境

| | |
|---|---|
| SOLIDWORKS | 2022 SP04（30.4.0）教育版，**繁體中文介面** |
| SOLIDWORKS CAM | 2022（只有 CAM 工具需要） |
| Windows | Windows 11 |
| Python | 測試用 3.13；server 也用 3.12 做過端對端檢查 |

只在這個環境測過。其他 SOLIDWORKS 版本或語言可能可以用（程式刻意不依賴在地化名稱），
但沒有驗證過。如果你在其他環境跑過測試，歡迎開 issue 告訴我們結果。

## 跑測試之前

> **測試程式在開始前、以及每個測試之間，會把 SOLIDWORKS 裡所有開著的文件「不存檔」關掉。**
> 請先存檔，或用一個沒有在做其他工作的 SOLIDWORKS。

- 請在 SOLIDWORKS 所在的 **Windows 桌面**上跑（一般的終端機視窗，不是 SSH 連線）：
  `test.py` 直接操作 SOLIDWORKS，不經過 SSH bridge。
- 測試存的檔案會放在 `%TEMP%` 底下新建的資料夾（`swmcp_*`），測試不會刪除它們。
- `mcp_screenshot` 會在 SOLIDWORKS 上方短暫開一個紅色小視窗（用來檢查不會拍到其他視窗），
  執行中請不要點擊。
- `mcp_cam_flow` 需要 SOLIDWORKS CAM 附加程式；`recognize_features` 的測試需要
  FeatureWorks（SOLIDWORKS Professional 或 Premium）。

## 執行方式

在專案資料夾，使用專案的 Python 環境（見 [README](README.md) 的 *Manual install*）：

```powershell
python test.py --list                       # 列出所有測試的 id 和類別
python test.py --test mcp_export_dxf        # 跑一個測試
python test.py --category "Sketch Tools"    # 跑一個類別
python test.py --gui                        # 互動式挑選
python test.py                              # 全部
```

- `--test` **一次只能給一個** id。`--test a --test b` 只會跑 `b`，要分開一個一個跑。
- 每個測試會逐項印出 ✓ 或 ❌，最後是 `PASSED` / `FAILED`；整輪結束會印出統計
  （`Passed : n`、`Failed : n`）。全部通過時結束代碼才是 0。
- 詳細紀錄寫在專案資料夾的 `solidworks_mcp.log`。

## 測試範圍

6 個類別共 84 個測試（完整清單用 `python test.py --list`）：

| 類別 | 數量 | 檢查內容 |
|---|---|---|
| Basic | 2 | 所有 `.py` 都能編譯、bridge 模組能載入（不需要 SOLIDWORKS）；從頭建一個方塊 |
| Sketch Tools | 12 | 各種草圖圖元、尺寸、限制條件、建構線 |
| Feature Tools | 9 | 除料伸長、圓角、倒角、薄殼、旋轉、疊層拉伸、參考平面、質量屬性、特徵清單 |
| MCP Tools | 52 | 用 AI 呼叫的方式（經過 server 的工具分派）呼叫工具，並核對幾何、體積、ID、輸出檔案。其中 `mcp_stdio_transport` 會把 `server.py` 當成真正的 MCP stdio server 啟動，所有呼叫都經過 pipe 傳送；只有經過 stdio 才會出現的卡住問題，其他測試抓不到 |
| Assembly | 8 | 文件存檔／切換／關閉、插入零件、結合、干涉與抑制、各文件獨立的狀態、參數修改、外框 |
| Integration | 1 | 在同一個本體上連續做多次除料伸長 |

105 個工具每一個都至少被一個測試呼叫過：不是在 `test.py` 裡，就是在
`tests/agent_capability_tests.md`（46 題可以直接貼給 AI 的題目，做端對端測試）。
46 題在上面的環境中，透過 Claude Code（經由 SSH bridge）全部通過。

## 結果與已知問題

**通過標準是：每個測試單獨跑（`--test <id>`）都要通過。** 在上面的環境中，除了下面兩個例外，
每個測試都符合。

最近一次全套連續跑（`python test.py`，當時 73 個測試）通過 70 個。失敗的 3 個跟前一次全套
失敗的不一樣，而且單獨跑都會通過；它們都跟「用座標點選幾何」或「目前作用中的文件」有關。
原因還沒找到。之後新增的 11 個測試（`code_compiles`、`mcp_cam_flow`、`mcp_export_dxf`、
`assembly_bounding_box`、`mcp_execute_python`、`mcp_screenshot`、`mcp_stdio_transport`、
`mcp_sketch_snapping`、`mcp_sketch_snapping_tools`、`mcp_sketch_entities_live`、`mcp_extrusion_error`）單獨跑都通過；84 個還沒有一次全部連續跑過。

單獨跑也已知不穩定的工具（工具說明裡也這樣告訴 AI）：

| 工具 | 問題 | 建議改用 |
|---|---|---|
| `set_dimension_value` | 用尺寸在畫面上的位置來點選，有時找不到（`No dimension found at (x, y)`）。不同次執行時有時通過、有時失敗。 | `set_parameter`，用尺寸名稱（如 `D1@Sketch1`，可用 `list_parameters` 查），不受視角影響。 |
| `suppress_component` | 在測試套件中，下一個組合件工具有時會回報 `Active document is not an assembly`。在測試套件以外重現不出來。 | 先對組合件用 `activate_document`，再重試。這個做法還沒對到真正的失敗驗證過。 |

只在全套連續跑時出現過（單獨跑都通過）：整合測試的除料序列（其中一次除料沒有切掉材料）、
`mcp_linear_pattern`。

## 透過 AI 程式測試

`tests/agent_capability_tests.md` 有 46 題，每題都附預期結果（體積、數量、位置）。
在裝好這個 server 的 Claude Desktop 或 Claude Code 裡一題一題貼上，再比對答案。
這測的是 `test.py` 測不到的部分：AI 能不能從工具說明選對工具。
