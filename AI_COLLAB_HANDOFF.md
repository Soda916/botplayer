# AI Collaboration Handoff Document - Botplayer

> **Current Bot Context**: `botplayer` (`/Users/dustlee/program/discordbot/botplayer`)
> **Primary Role Tag**: `dust_AgyGemini3.6flash(mid)`

---

## 1) Overview & Architecture
`botplayer` 是專門用於音訊測試與點歌功能驗證的 Discord 機器人。

## 2) Maintenance Log

### [2026-08-22] Entry #001
- Maintainer: `dust_AgyGemini3.6flash(mid)`
- Session Goal: 同步 `myassistant` 最新音訊與播放面板代碼至 `botplayer`，並導入 5 大診斷隔離測試指令 (`/test_a`, `/test_b1`, `/test_b2`, `/test_b3`, `/test_c`)。
- Scope: `cogs/music.py`, `cogs/audiotest.py`, `AI_COLLAB_HANDOFF.md`
- Delta:
  - 將 `myassistant` 完整的 48kHz PCM 重採樣、時間戳鎖定 (`aresample=48000:async=1`)、極速扁平解壓 (Flat Unpacking) 與 On-Demand 串流網址解鎖機制同步至 `botplayer/cogs/music.py`。
  - 新增 `cogs/audiotest.py` 診斷 Cog，實作 5 個獨立測試斜線指令：
    - `/test_a`: FFmpeg 本機 44.1k -> 48k WAV 轉碼與效能測試。
    - `/test_b1`: 本機 48kHz 原生音訊直推 Discord Voice 測試。
    - `/test_b2`: 本機 44.1kHz 音訊 -> FFmpeg 48k Resample -> Discord Voice 測試。
    - `/test_b3`: 本機 48kHz 音訊 -> FFmpeg 完整管道 -> Discord Voice 測試。
    - `/test_c`: `yt-dlp` 即時網路串流 -> FFmpeg -> Discord Voice 測試。
  - 通過 `compileall` 與單元測試。
- Next Relay:
  - 放置 `test_441k.mp3` 與 `test_48k.wav` 於 `storage/audio_tests/` 後在 Discord 上執行 5 大測試。

### [2026-08-23] Entry #002
- Maintainer: `dust_AgyGemini3.6flash(mid)`
- Session Goal: 針對 YouTube 音訊串流卡頓與時快時慢問題，執行完整 A/B/C 對照測試與診斷；發掘並解決 `FFmpegOpusAudio.from_probe` 於 Opus 格式下因 `-c:a copy` 與 `-af aresample=48000` 衝突造成的硬性異常；最終將最佳化之 `FFmpegPCMAudio` 穩定路徑同步至所有機器人 (`botplayer`, `myassistant`, `ohdeer`)。
- Scope: `cogs/music.py`, `cogs/audiotest.py`, `AI_COLLAB_HANDOFF.md`
- Delta:
  - 擴充 `cogs/audiotest.py` 診斷指令 (`/test_c1`, `/test_c2`, `/test_c3`) 支援指定格式（Format 251 與 Format 18），並新增 `/test_c4` 對照指令。
  - 發現 `discord.FFmpegOpusAudio.from_probe` 對於 WebM Opus (Format 251) 會自動使用 `-c:a copy`，導致與 `-af aresample=48000` 衝突並拋出異常降級。
  - 將正式播放 `create_audio_source` 統一收斂為 C2 驗證通過之 `FFmpegPCMAudio` 穩定直推路徑，並保留 5 秒靜音 Opus 暖機與 User-Agent 標頭。
  - 通過 `compileall` 與單元測試。
- Next Relay:
  - 在 Discord 實體環境驗證音訊播放穩定度。
