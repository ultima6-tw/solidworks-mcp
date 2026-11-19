# 更新紀錄

這個 fork 改了什麼，最新的在最上面。標示 **建議更新** 的項目，修的是「工具回報成功，
但其實沒有照指示做」的問題。

更新 git 下載的程式碼：在資料夾裡執行 `git pull`，再重新啟動 MCP server（重開 AI 程式或它的
MCP 連線）；SSH 模式還要重開桌面上的 bridge host。GitHub 上有你這份還沒有的更新時，server
會告訴你——見 [INSTALL.zh-TW.md › 更新](INSTALL.zh-TW.md#更新)。
English: [CHANGELOG.md](CHANGELOG.md).

## 2026-11-19

- `recognize_features`：SOLIDWORKS 回報 FeatureWorks 已載入、但其實沒有啟動時（在 SOLIDWORKS 2022 實測過），
  錯誤訊息現在會說明這個情況，並請使用者在「工具 → 附加程式」勾選 FeatureWorks。以前一律說需要 Professional
  或 Premium 授權，但問題不在那裡。沒安裝 FeatureWorks 和授權錯誤現在也會分開說明。
- SOLIDWORKS 視窗比較小時，在其他圖形旁邊畫小圖形不會再失敗。吸附範圍是固定的螢幕像素數，視窗越小
  （或縮得越遠），換算成毫米就越遠；在 SOLIDWORKS 2022 實測，視窗 1024×640 或 1536×960 時，在兩個小圓
  下方畫 7×14 mm 的長方形，SOLIDWORKS 什麼都沒畫，工具回報錯誤。現在工具會關掉吸附再畫一次，跟原本
  「吸附改了尺寸就關掉吸附重畫」的做法一樣。

## 2026-11-18

- **建議更新——會改變實體的工具，回報實體前後的體積**（`volumeChange`）。特徵可能沒有錯誤碼，
  卻什麼都沒做：在 SOLIDWORKS 2022 實測到，整個畫在實體裡面的伸長回報 ✓，但沒有加上任何材料。
  現在體積沒變，或變化方向錯了（伸長反而少了材料），回報會從 ✓ 改成 ⚠。固定深度的伸長和除料，
  工具還會自己算出預期變化量（SOLIDWORKS 讀出的輪廓面積 × 深度），比預期大就標出來；比預期小
  只附註，因為除料有一部分切在實體外、伸長跟原本的材料重疊都是正常的。線性陣列、環狀陣列和鏡射的
  預期值是原特徵的變化 × 複本數（原特徵要是這次用這些工具做出來的）。
- `solidworks_batch`：有呼叫執行了但回報 ⚠ 時，batch 的總結也會變成 ⚠ 並指出是哪一個（那個呼叫
  標 `"warning": true`）。以前只要每個呼叫都有執行，總結就是 ✓。
- `mirror`（鏡射平面）、`draft`（中立面）、`ref_plane`（參考平面）接受 `ref_plane` 回傳的 ID
  （`ref:Plane1`）；以前只接受平面名稱。
- **建議更新——`linear_pattern` 設 `{"axis": "X"}` 可能往 −X 排。** SOLIDWORKS 沿著給它的那條邊
  的方向排列，工具以前用找到的第一條平行邊；在一般方塊上實測，X 和 Z 都排反了。現在 `{"axis": ...}`
  一律往 +軸，`reverseDir1`／`reverseDir2` 往 −軸，兩個陣列方向都是。
- **建議更新——特徵工具不再讓 SOLIDWORKS 一直累積 handle。** SOLIDWORKS 交給 server 的每個 COM 物件，
  都會在 SOLIDWORKS 程序裡留下永遠不釋放的 handle，而 server 每次呼叫後的檢查會把整棵特徵樹走好幾遍。
  長時間測試下 SOLIDWORKS 累積到將近 300 萬個 handle，零件越大每次呼叫也越慢。
  現在檢查和各工具都只讀最後幾個特徵：60 個特徵的零件上，除料一次約 300 個 handle（原本約 6000，時間從約 6.5 秒降到約
  1.3 秒），開草圖約 450 個（原本約 1600）。
- **啟動時檢查更新。** server 啟動後在背景問 GitHub 一次，你這份是不是舊版；是的話，這次
  使用的第一個工具回傳會附上提醒。只送出一個公開的 GitHub API 查詢，不會送出任何跟你、你的
  電腦或文件有關的資料。用 `SOLIDWORKS_MCP_UPDATE_CHECK=0` 關掉。詳見
  [INSTALL.zh-TW.md › 更新](INSTALL.zh-TW.md#更新)。
- **建議更新——所有特徵工具都會讀特徵的錯誤狀態**（`2c10219`）。SOLIDWORKS 可能回傳一個
  「帶錯誤」或「帶警告」、實際上什麼都沒做的特徵，工具以前照樣回報 ✓。在 SOLIDWORKS 2022
  實測到：線性陣列或鏡射的複本落在實體外（警告 51，什麼都沒加）。現在帶錯誤的特徵會被刪掉、
  呼叫失敗並附上 SOLIDWORKS 官方說明；帶警告的特徵保留，回報從 ✓ 改成 ⚠。SOLIDWORKS 回傳
  「什麼都沒有」還是這種特徵，在長時間使用時會改變，所以不能交給各工具自己判斷。

## 2026-11-17

- **建議更新——草圖工具會核對畫出來的結果**（`df070a2`、`9e849c8`）。視角縮小時，SOLIDWORKS
  會把新畫的草圖吸附到附近的幾何上，卻照樣回報成功：長方形旁邊半徑 1.7 mm 的圓變成 3.5 mm、
  7 mm 寬的長方形變成 3.4 mm，弧、槽孔、點、中心線最多偏移 2.5 mm。長方形、圓、直線、弧、
  多邊形、橢圓、槽孔、spline、點、中心線現在都會讀回核對，不符就關掉吸附重畫成正確尺寸；
  `sketch_profile` 會核對，畫錯就刪掉並報錯。
- **建議更新——除料和伸長的錯誤特徵**（`8c0abc8`、`46439d5`）。方向朝外的完全貫穿除料以前
  可能回報 ✓ 卻什麼都沒切到，現在會改試另一個方向；朝向沒有實體一側的伸長會回報錯誤，不再是 ✓。
- `get_sketch_entities` 另外回傳草圖的實際幾何（`geometry`），直接從 SOLIDWORKS 讀——包含被尺寸、
  限制條件拉動之後的位置，以及不是用這些工具畫的幾何（`9478b85`）。
- server 在 Windows 上經過 stdio 執行時，`export_dxf` 不會再卡住（`511f4c2`）。
- 新工具：`export_dxf`（平面 → 1:1 mm 的 DXF）、`get_assembly_bounding_box`、`screenshot`
  （只拍 SOLIDWORKS 視窗）、`execute_python`（預設關閉，要自己開），以及 SOLIDWORKS CAM 銑削工具
  （素材 → 刀具路徑 → 模擬 → G-code）。
- [INSTALL.zh-TW.md](INSTALL.zh-TW.md)（寫給負責安裝的 AI 看的安裝指引）與
  [TESTING.zh-TW.md](TESTING.zh-TW.md)，英文與繁體中文。

## 2026-11-16——從 HarrierPigeon/Solidworks-MCP-Server 分支

- 支援非英文版 SOLIDWORKS；`test.py` 不依賴介面語言。
- MCP SDK 限定 1.x（`mcp>=1.26.0,<2`）：2.x 移除了 server 用到的 API。
- SSH 模式：桌面上的 bridge host，讓 server 可以從 SSH 等非桌面工作階段使用。
- 在 SOLIDWORKS 2022 上逐一測試所有工具時找到並修好的問題：伸長 `reverse`、等距倒角、內接多邊形、
  小尺寸草圖、拔模、基準軸與參考點、環狀陣列參數順序、外觀螺紋、邊界填料／除料、肋、包覆、
  疊層除料、異型孔精靈、`move_to_folder`、面上取樣點、空草圖的追蹤。
