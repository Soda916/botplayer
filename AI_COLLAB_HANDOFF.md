# AI 協作交接手冊 (botplayer)

> 本文件為 `botplayer` 專案之 **AI 協作單一事實來源 (Single Source of Truth)**。
> 任何 AI 在開始或接續工作前，必須閱讀本文件並更新維護紀錄。

## 1. 專案簡介與維護身份

- **專案名稱**：`botplayer`
- **專案定位**：極輕量、低資源消耗且專為 GCP e2-micro 最佳化之 Discord YouTube 音樂播放機器人。
- **維護者標籤**：`dust_AgyGemini3.6flash(mid)`
- **語言/框架**：Python 3.10+ / `discord.py` / `yt-dlp` / `FFmpeg`

## 2. 專案最高架構原則

1. **GCP e2-micro 極致省資源優化**
   - 僅解析 YouTube 音源（`bestaudio`），嚴禁下載影片檔或圖像處理。
   - 採用 direct URL 串流（Direct Streaming via FFmpeg Pipe/Reconnect），不佔用本機硬碟空間。
   - 語音頻道閒置自動斷開（Auto Disconnect），防範背景 FFmpeg 程序殘留與記憶體洩漏 (Memory Leaks)。
2. **防崩潰與非同步安全**
   - 所有的 `yt-dlp` 網路與解析請求必須透過 `asyncio.to_thread` 執行，嚴禁阻塞 Discord Event Loop 主執行緒。
   - 妥善捕獲並處理網絡逾時、無效網址、權限不足（無法加入語音頻道 / 無法發言）等例外。
3. **功能契約**
   - **音訊解析**：僅提取音訊，不解析/下載影片畫面。
   - **播放清單**：明確不支援 Playlist 解析（`noplaylist: True`），點擊清單連結僅擷取單曲。
   - **待播清單 (Queue)**：支援單曲佇列，播放中點歌自動排入佇列。
   - **操作面板 (Control Panel)**：播放時提供包含 ⏯️ 播放/暫停、⏭️ 下一首、⏮️ 上一首、⏹️ 停止/離開、📜 查看佇列 之動態 Discord UI View。

## 3. 專案結構盤點

```
botplayer/
├── AI_COLLAB_HANDOFF.md     # AI 協作與交接說明檔
├── main.py                  # Bot 啟動點、Intents 設定與 Cog 動態載入
├── requirements.txt         # 專案相依套件
├── .env.example             # 環境變數範本
└── cogs/
    ├── music.py             # 音樂播放、待播清單、yt-dlp 串流與操作面板 View
    └── ping.py              # 基礎狀態測試指令 (/ping)
```

## 4. 維護紀錄（只追加）

### [2026-08-20] Entry #001
- Maintainer: `dust_AgyGemini3.6flash(mid)`
- Session Goal: 從零初始化極輕量 Discord 音樂機器人 `botplayer`。
- Scope: `AI_COLLAB_HANDOFF.md`, `main.py`, `requirements.txt`, `.env.example`, `cogs/music.py`, `cogs/ping.py`
- Delta:
  - 建立專案結構與輕量化 `botplayer` 音樂機器人。
  - 實作 `yt-dlp` direct audio URL 串流（不下載檔案、僅提取音源 `bestaudio`、`noplaylist: True`）。
  - 實作每伺服器獨立的待播佇列（Queue）與歷史播放記錄（History）。
  - 實作即時互動播放面板 `MusicControlView`（包含 ⏯️ 播放/暫停、⏭️ 下一首、⏮️ 上一首、⏹️ 停止/離開、📜 待播清單）。
  - 實作 e2-micro 資源防護：語音頻道閒置自動離線、`asyncio.to_thread` 防止 Event Loop 阻塞。
### [2026-08-20] Entry #002
- Maintainer: `dust_AgyGemini3.6flash(mid)`
- Session Goal: 檢查並補齊 Discord 語音連線必備相依套件 (PyNaCl, davey) 與 libopus 跨平台自動載入機制。
- Scope: `main.py`, `requirements.txt`, `AI_COLLAB_HANDOFF.md`
- Delta:
  - 於 `main.py` 補齊 `libopus` 自動檢測與載入機制 (`ctypes.util.find_library("opus")`)，確保不同作業系統環境 (macOS / Linux / GCP e2-micro) 皆可正常發聲。
  - 確認 `requirements.txt` 已包含 `discord.py[voice]`，其自動帶入 `PyNaCl` (xsalsa20/xchacha20 語音封包加密) 與 `davey` (Discord 新版 DAVE 端對端語音加密協定)。
  - 通過編譯與單元測試。
- Next Relay:
  - 於伺服器部署並測試語音頻道連線。

