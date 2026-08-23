import os
import time
import asyncio
import logging
import subprocess
import resource
from typing import Optional
import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp

logger = logging.getLogger("botplayer.audiotest")

# 解析專案根目錄絕對路徑
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_AUDIO_DIR = os.path.join(BASE_DIR, "storage", "audio_tests")
os.makedirs(TEST_AUDIO_DIR, exist_ok=True)

# 網路與本機檔解碼參數：強制僅解碼音訊軌 (-map 0:a:0)，預設優先請求 Format 251 (純 48kHz WebM Opus 音訊)
HTTP_BEFORE_OPTIONS = "-threads 2 -probesize 1M -analyzeduration 2000000 -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
LOCAL_BEFORE_OPTIONS = "-threads 2 -probesize 4M -analyzeduration 4000000"
CLEAN_FFMPEG_OPTIONS = "-map 0:a:0 -vn -af aresample=48000"


def resolve_test_filepath(filename: str) -> Optional[str]:
    """解析測試音訊檔絕對路徑"""
    candidate1 = os.path.join(TEST_AUDIO_DIR, filename)
    if os.path.exists(candidate1):
        return candidate1
    candidate2 = os.path.join(BASE_DIR, filename)
    if os.path.exists(candidate2):
        return candidate2
    if os.path.exists(filename):
        return os.path.abspath(filename)
    return None


class AudioTestCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def ensure_voice(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ 你必須先加入一個語音頻道！", ephemeral=True)
            return None

        voice_channel = interaction.user.voice.channel
        guild = interaction.guild
        vc = guild.voice_client

        if not vc or not vc.is_connected():
            try:
                vc = await voice_channel.connect(reconnect=True, self_deaf=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ 連線至語音頻道失敗: {e}", ephemeral=True)
                return None
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)

        return vc

    async def cleanup_voice_player(self, vc: discord.VoiceClient):
        """徹底清理語音播放器，確保舊 FFmpeg 行程完全釋放死透，避免殘留進程污染 CPU 或發送"""
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            for _ in range(20):
                if not (vc.is_playing() or vc.is_paused()):
                    break
                await asyncio.sleep(0.1)

    # ------------------ Slash Commands for A/B1/B2/B3/C1/C2/C3 ------------------

    @app_commands.command(name="test_a", description="🧪 [Test A] 測試 FFmpeg 本機 44.1kHz -> 48kHz 轉碼效能 (無 Discord/無 yt-dlp)")
    @app_commands.describe(filename="測試檔案名稱 (預設: test_441k.mp3)")
    async def test_a(self, interaction: discord.Interaction, filename: str = "test_441k.mp3"):
        await interaction.response.defer()

        filepath = resolve_test_filepath(filename)
        if not filepath:
            await interaction.followup.send(
                f"❌ 找不到測試檔案 `{filename}`！\n請將測試音訊檔放入 `{TEST_AUDIO_DIR}/` 資料夾中。"
            )
            return

        out_path = os.path.join(TEST_AUDIO_DIR, "output_test_a_48k.wav")
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass

        cmd = [
            "ffmpeg", "-threads", "2", "-y", "-i", filepath,
            "-ar", "48000", "-ac", "2",
            out_path
        ]

        t0 = time.time()
        loop = asyncio.get_running_loop()

        def run_ffmpeg():
            res = subprocess.run(cmd, capture_output=True, text=True)
            return res

        try:
            res = await loop.run_in_executor(None, run_ffmpeg)
            t1 = time.time()
            elapsed_ms = (t1 - t0) * 1000

            if res.returncode != 0 or not os.path.exists(out_path):
                embed = discord.Embed(
                    title="❌ [Test A] FFmpeg 轉碼失敗",
                    description=f"FFmpeg 退出碼: `{res.returncode}`\n\n```\n{res.stderr[-500:]}\n```",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed)
                return

            out_size_bytes = os.path.getsize(out_path)
            out_size_mb = out_size_bytes / (1024 * 1024)

            embed = discord.Embed(
                title="✅ [Test A] FFmpeg 本機轉碼測試成功",
                description="驗證 44.1 kHz 轉碼至 48 kHz WAV 之耗時與輸出狀況",
                color=discord.Color.green()
            )
            embed.add_field(name="輸入檔案", value=f"`{os.path.basename(filepath)}`", inline=True)
            embed.add_field(name="轉碼耗時", value=f"`{elapsed_ms:.2f} ms`", inline=True)
            embed.add_field(name="輸出大小", value=f"`{out_size_mb:.2f} MB`", inline=True)
            embed.add_field(name="測試結論", value="FFmpeg 44.1k -> 48k 本機轉碼運作正常！", inline=False)
            await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ 執行 Test A 失敗: {e}")

    @app_commands.command(name="test_b1", description="🧪 [Test B1] 測試本機原生 48kHz 音訊直推 Discord")
    @app_commands.describe(filename="測試檔案名稱 (預設: test_48k.wav)")
    async def test_b1(self, interaction: discord.Interaction, filename: str = "test_48k.wav"):
        vc = await self.ensure_voice(interaction)
        if not vc:
            return

        filepath = resolve_test_filepath(filename)
        if not filepath:
            await interaction.response.send_message(
                f"❌ 找不到測試檔案 `{filename}`！\n請將測試音訊檔放入 `{TEST_AUDIO_DIR}/` 資料夾中。",
                ephemeral=True
            )
            return

        await self.cleanup_voice_player(vc)

        try:
            source = discord.FFmpegPCMAudio(filepath, before_options=LOCAL_BEFORE_OPTIONS, options=CLEAN_FFMPEG_OPTIONS)

            def after_play(err):
                if err:
                    logger.error(f"[Test B1] 播放例外: {err}")

            vc.play(source, after=after_play)

            embed = discord.Embed(
                title="▶️ [Test B1] 本機 48kHz 直推播放中",
                description=f"檔案: `{os.path.basename(filepath)}`",
                color=discord.Color.blue()
            )
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            await interaction.response.send_message(f"❌ 啟動 Test B1 失敗: {e}", ephemeral=True)

    @app_commands.command(name="test_b2", description="🧪 [Test B2] 測試本機 44.1kHz -> FFmpeg Resample 48kHz -> Discord (評分 99/100 參考)")
    @app_commands.describe(filename="測試檔案名稱 (預設: test_441k.mp3)")
    async def test_b2(self, interaction: discord.Interaction, filename: str = "test_441k.mp3"):
        vc = await self.ensure_voice(interaction)
        if not vc:
            return

        filepath = resolve_test_filepath(filename)
        if not filepath:
            await interaction.response.send_message(
                f"❌ 找不到測試檔案 `{filename}`！\n請將測試音訊檔放入 `{TEST_AUDIO_DIR}/` 資料夾中。",
                ephemeral=True
            )
            return

        await self.cleanup_voice_player(vc)

        try:
            source = discord.FFmpegPCMAudio(filepath, before_options=LOCAL_BEFORE_OPTIONS, options=CLEAN_FFMPEG_OPTIONS)

            def after_play(err):
                if err:
                    logger.error(f"[Test B2] 播放例外: {err}")

            vc.play(source, after=after_play)

            embed = discord.Embed(
                title="▶️ [Test B2] 本機 44.1kHz -> FFmpeg Resample 播放中 (99分最佳標竿)",
                description=f"檔案: `{os.path.basename(filepath)}`",
                color=discord.Color.purple()
            )
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            await interaction.response.send_message(f"❌ 啟動 Test B2 失敗: {e}", ephemeral=True)

    @app_commands.command(name="test_b3", description="🧪 [Test B3] 測試本機 48kHz -> FFmpeg 完整管道 -> Discord")
    @app_commands.describe(filename="測試檔案名稱 (預設: test_48k.wav)")
    async def test_b3(self, interaction: discord.Interaction, filename: str = "test_48k.wav"):
        vc = await self.ensure_voice(interaction)
        if not vc:
            return

        filepath = resolve_test_filepath(filename)
        if not filepath:
            await interaction.response.send_message(
                f"❌ 找不到測試檔案 `{filename}`！\n請將測試音訊檔放入 `{TEST_AUDIO_DIR}/` 資料夾中。",
                ephemeral=True
            )
            return

        await self.cleanup_voice_player(vc)

        try:
            source = discord.FFmpegPCMAudio(filepath, before_options=LOCAL_BEFORE_OPTIONS, options=CLEAN_FFMPEG_OPTIONS)

            def after_play(err):
                if err:
                    logger.error(f"[Test B3] 播放例外: {err}")

            vc.play(source, after=after_play)

            embed = discord.Embed(
                title="▶️ [Test B3] 本機 48kHz -> FFmpeg 完整管道 播放中",
                description=f"檔案: `{os.path.basename(filepath)}`",
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            await interaction.response.send_message(f"❌ 啟動 Test B3 失敗: {e}", ephemeral=True)

    @app_commands.command(name="test_c1", description="🧪 [Test C1] 指定格式下載至本機 ➔ FFmpegPCMAudio 輸出至 Discord (隔離網路串流)")
    @app_commands.describe(url="測試的 YouTube 影片網址", format_id="指定格式 (預設 251: 純 WebM Opus；可選 18: MP4 AAC 360p)")
    @app_commands.choices(format_id=[
        app_commands.Choice(name="Format 251 (純 WebM Opus 48k 音訊)", value="251"),
        app_commands.Choice(name="Format 18 (MP4 AAC+H.264 影音混合檔)", value="18"),
    ])
    async def test_c1(self, interaction: discord.Interaction, url: str, format_id: str = "251"):
        vc = await self.ensure_voice(interaction)
        if not vc:
            return

        await interaction.response.defer()
        loop = asyncio.get_running_loop()

        out_path = os.path.join(TEST_AUDIO_DIR, f"c1_download_fmt{format_id}.tmp")
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass

        logger.info(f"========== [Test C1] 開始全流程測試 (目標網址: {url}, Format: {format_id}) ==========")
        logger.info(f"[Test C1 Step 1/2] 啟動 yt-dlp 下載至本機檔 `{out_path}`...")

        t_dl_start = time.perf_counter()

        def do_download():
            opts = {
                "format": format_id,
                "outtmpl": out_path,
                "quiet": True,
                "overwrites": True
            }
            with yt_dlp.YoutubeDL(opts) as ytdl:
                info = ytdl.extract_info(url, download=True)
                return info

        try:
            info = await loop.run_in_executor(None, do_download)
            t_dl_end = time.perf_counter()
            dl_ms = (t_dl_end - t_dl_start) * 1000

            if not os.path.exists(out_path):
                await interaction.followup.send(f"❌ [Test C1] 下載失敗：未產生實體檔案 (Format {format_id})！")
                return

            file_size_bytes = os.path.getsize(out_path)
            file_size_mb = file_size_bytes / (1024 * 1024)
            title = info.get("title", "未知曲名")
            actual_fmt = info.get("format_id", "N/A")
            ext = info.get("ext", "N/A")
            acodec = info.get("acodec", "N/A")
            vcodec = info.get("vcodec", "none")
            asr = info.get("asr", "N/A")

            logger.info(f"[Test C1 Step 1/2 完成] 下載耗時: {dl_ms:.2f}ms, 大小: {file_size_mb:.2f}MB, Format: {actual_fmt} ({ext}, acodec: {acodec}, vcodec: {vcodec})")
            logger.info(f"[Test C1 Step 2/2] 初始化 FFmpeg 讀取本機檔 `{out_path}` 並推送 Discord...")

            await self.cleanup_voice_player(vc)

            t_source_0 = time.perf_counter()
            source = discord.FFmpegPCMAudio(out_path, before_options=LOCAL_BEFORE_OPTIONS, options=CLEAN_FFMPEG_OPTIONS)
            t_source_ms = (time.perf_counter() - t_source_0) * 1000

            def after_play(err):
                if err:
                    logger.error(f"[Test C1] FFmpeg 播放例外: {err}")
                else:
                    logger.info("[Test C1] FFmpeg 播放成功結束。")

            vc.play(source, after=after_play)

            embed = discord.Embed(
                title=f"▶️ [Test C1] 本機下載模式 (Format {actual_fmt})",
                description=f"歌曲: **[{title}]({info.get('webpage_url', url)})**\n\n已隔離網路串流，比照 B2 模式播放。",
                color=discord.Color.blue()
            )
            embed.add_field(name="1. 下載耗時 / 大小", value=f"`{dl_ms:.2f} ms` / `{file_size_mb:.2f} MB`", inline=True)
            embed.add_field(name="2. 建立耗時", value=f"`{t_source_ms:.2f} ms`", inline=True)
            embed.add_field(name="3. 格式資訊", value=f"`id: {actual_fmt}` | `{ext}` | 音訊: `{acodec} ({asr}Hz)` | 視訊: `{vcodec}`", inline=False)
            embed.add_field(name="4. FFmpeg Before", value=f"`{LOCAL_BEFORE_OPTIONS}`", inline=False)
            embed.add_field(name="5. FFmpeg Options", value=f"`{CLEAN_FFMPEG_OPTIONS}`", inline=False)

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"[Test C1] 測試例外失敗: {e}", exc_info=True)
            await interaction.followup.send(f"❌ [Test C1] 執行失敗: {e}")

    @app_commands.command(name="test_c2", description="🧪 [Test C2] 指定格式線上即時網路串流 ➔ FFmpegPCMAudio 直推")
    @app_commands.describe(url="測試的 YouTube 影片網址", format_id="指定格式 (預設 251: 純 WebM Opus；可選 18: MP4 AAC 360p)")
    @app_commands.choices(format_id=[
        app_commands.Choice(name="Format 251 (純 WebM Opus 48k 音訊)", value="251"),
        app_commands.Choice(name="Format 18 (MP4 AAC+H.264 影音混合檔)", value="18"),
    ])
    async def test_c2(self, interaction: discord.Interaction, url: str, format_id: str = "251"):
        vc = await self.ensure_voice(interaction)
        if not vc:
            return

        await interaction.response.defer()
        loop = asyncio.get_running_loop()

        logger.info(f"========== [Test C2] 開始全流程即時串流測試 (目標網址: {url}, Format: {format_id}) ==========")
        logger.info(f"[Test C2 Step 1/2] 解析 YouTube 指定格式 {format_id} HTTP 網址...")

        t_res_start = time.perf_counter()

        def do_resolve():
            opts = {
                "format": format_id,
                "extract_flat": False,
                "noplaylist": True,
                "quiet": True,
            }
            with yt_dlp.YoutubeDL(opts) as ytdl:
                info = ytdl.extract_info(url, download=False)
                return info

        try:
            info = await loop.run_in_executor(None, do_resolve)
            t_res_end = time.perf_counter()
            res_ms = (t_res_end - t_res_start) * 1000

            if not info or not info.get("url"):
                await interaction.followup.send(f"❌ [Test C2] 即時串流網址解析失敗 (Format {format_id})！")
                return

            stream_url = info.get("url")
            title = info.get("title", "未知曲名")
            actual_fmt = info.get("format_id", "N/A")
            ext = info.get("ext", "N/A")
            acodec = info.get("acodec", "N/A")
            vcodec = info.get("vcodec", "none")
            asr = info.get("asr", "N/A")
            user_agent = info.get("http_headers", {}).get("User-Agent", "")

            http_before = HTTP_BEFORE_OPTIONS
            if user_agent:
                http_before = f"-headers \"User-Agent: {user_agent}\r\n\" {HTTP_BEFORE_OPTIONS}"

            logger.info(f"[Test C2 Step 1/2 完成] 解析耗時: {res_ms:.2f}ms, Format: {actual_fmt} ({ext}, acodec: {acodec}, vcodec: {vcodec})")
            logger.info(f"[Test C2 Step 2/2] 初始化 FFmpeg 讀取 Format {actual_fmt} 串流 `{stream_url[:60]}...` 並推送 Discord...")

            await self.cleanup_voice_player(vc)

            t_source_0 = time.perf_counter()
            source = discord.FFmpegPCMAudio(stream_url, before_options=http_before, options=CLEAN_FFMPEG_OPTIONS)
            t_source_ms = (time.perf_counter() - t_source_0) * 1000

            def after_play(err):
                if err:
                    logger.error(f"[Test C2] FFmpeg 串流播放例外: {err}")
                else:
                    logger.info("[Test C2] FFmpeg 串流播放成功結束。")

            vc.play(source, after=after_play)

            embed = discord.Embed(
                title=f"▶️ [Test C2] 線上即時串流 (Format {actual_fmt})",
                description=f"歌曲: **[{title}]({info.get('webpage_url', url)})**\n\n以 FFmpegPCMAudio 直推 (標準 CRLF User-Agent 標頭)。",
                color=discord.Color.green()
            )
            embed.add_field(name="1. 解析耗時", value=f"`{res_ms:.2f} ms`", inline=True)
            embed.add_field(name="2. 建立耗時", value=f"`{t_source_ms:.2f} ms`", inline=True)
            embed.add_field(name="3. 格式資訊", value=f"`id: {actual_fmt}` | `{ext}` | 音訊: `{acodec} ({asr}Hz)` | 視訊: `{vcodec}`", inline=False)
            embed.add_field(name="4. User-Agent", value=f"`{user_agent[:40]}...`", inline=False)
            embed.add_field(name="5. FFmpeg Before", value=f"`{http_before}`", inline=False)
            embed.add_field(name="6. FFmpeg Options", value=f"`{CLEAN_FFMPEG_OPTIONS}`", inline=False)

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"[Test C2] 測試例外失敗: {e}", exc_info=True)
            await interaction.followup.send(f"❌ [Test C2] 執行失敗: {e}")

    @app_commands.command(name="test_c3", description="🧪 [Test C3] 一鍵連續執行指定格式之 C1(下載) ➔ 5秒冷卻 ➔ C2(串流)")
    @app_commands.describe(url="測試的 YouTube 影片網址", format_id="指定格式 (預設 251: 純 WebM Opus；可選 18: MP4 AAC 360p)")
    @app_commands.choices(format_id=[
        app_commands.Choice(name="Format 251 (純 WebM Opus 48k 音訊)", value="251"),
        app_commands.Choice(name="Format 18 (MP4 AAC+H.264 影音混合檔)", value="18"),
    ])
    async def test_c3(self, interaction: discord.Interaction, url: str, format_id: str = "251"):
        vc = await self.ensure_voice(interaction)
        if not vc:
            return

        await interaction.response.defer()
        loop = asyncio.get_running_loop()

        channel = interaction.channel

        logger.info(f"==================== [Test C3 一鍵雙測] 開始 (目標網址: {url}, Format: {format_id}) ====================")
        logger.info("[Test C3 階段 1/2] 啟動 C1 測試 (下載至本機檔 ➔ FFmpeg 直推)...")

        out_path = os.path.join(TEST_AUDIO_DIR, f"c3_download_fmt{format_id}.tmp")
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass

        t_dl_start = time.perf_counter()

        def do_download():
            opts = {
                "format": format_id,
                "outtmpl": out_path,
                "quiet": True,
                "overwrites": True
            }
            with yt_dlp.YoutubeDL(opts) as ytdl:
                info = ytdl.extract_info(url, download=True)
                return info

        try:
            info = await loop.run_in_executor(None, do_download)
            t_dl_end = time.perf_counter()
            dl_ms = (t_dl_end - t_dl_start) * 1000

            file_size_bytes = os.path.getsize(out_path) if os.path.exists(out_path) else 0
            file_size_mb = file_size_bytes / (1024 * 1024)
            title = info.get("title", "未知曲名")
            actual_fmt = info.get("format_id", "N/A")
            ext = info.get("ext", "N/A")
            acodec = info.get("acodec", "N/A")
            vcodec = info.get("vcodec", "none")

            await self.cleanup_voice_player(vc)

            c1_finished_event = asyncio.Event()

            source1 = discord.FFmpegPCMAudio(out_path, before_options=LOCAL_BEFORE_OPTIONS, options=CLEAN_FFMPEG_OPTIONS)

            def after_c1(err):
                if err:
                    logger.error(f"[Test C3-C1] 播放例外: {err}")
                else:
                    logger.info("[Test C3-C1] 階段 1 本機播放完成。")
                loop.call_soon_threadsafe(c1_finished_event.set)

            vc.play(source1, after=after_c1)

            embed1 = discord.Embed(
                title=f"▶️ [Test C3 階段 1/2] C1 本機下載檔 (Format {actual_fmt})",
                description=f"歌曲: **[{title}]({info.get('webpage_url', url)})**\n\n一鍵兩測階段 1。播畢後將自動發送 5 秒冷卻通知並開啟 C2 串流測試。",
                color=discord.Color.blue()
            )
            embed1.add_field(name="下載耗時", value=f"`{dl_ms:.2f} ms`", inline=True)
            embed1.add_field(name="檔案大小", value=f"`{file_size_mb:.2f} MB`", inline=True)
            embed1.add_field(name="格式 ID", value=f"`{actual_fmt} ({ext})`", inline=True)

            await interaction.followup.send(embed=embed1)

            # 等待 C1 播放完成
            logger.info("[Test C3] 等待 C1 本機播放完成...")
            await c1_finished_event.wait()

            # C1 完成，發送頻道通知並等待 5 秒
            logger.info("[Test C3] C1 播放完成！發送頻道通知並冷卻 5 秒...")
            if channel:
                await channel.send(f"📢 **[Test C3 自動化測試通知]** C1 測試 (Format {actual_fmt}) 已完成！將在 **5 秒** 後自動發動 C2 串流測試...")

            for i in range(5, 0, -1):
                await asyncio.sleep(1.0)

            # 階段 2: 啟動 C2 即時串流測試
            logger.info("[Test C3 階段 2/2] 啟動 C2 測試 (即時網路串流 ➔ FFmpeg 直推)...")
            await self.cleanup_voice_player(vc)

            t_res_start = time.perf_counter()

            def do_resolve():
                opts = {
                    "format": format_id,
                    "extract_flat": False,
                    "noplaylist": True,
                    "quiet": True,
                }
                with yt_dlp.YoutubeDL(opts) as ytdl:
                    res_info = ytdl.extract_info(url, download=False)
                    return res_info

            c2_info = await loop.run_in_executor(None, do_resolve)
            t_res_end = time.perf_counter()
            res_ms = (t_res_end - t_res_start) * 1000

            stream_url = c2_info.get("url")
            c2_format_id = c2_info.get("format_id", "N/A")
            c2_ext = c2_info.get("ext", "N/A")
            c2_acodec = c2_info.get("acodec", "N/A")
            c2_vcodec = c2_info.get("vcodec", "none")
            user_agent = c2_info.get("http_headers", {}).get("User-Agent", "")

            http_before = HTTP_BEFORE_OPTIONS
            if user_agent:
                http_before = f"-headers \"User-Agent: {user_agent}\r\n\" {HTTP_BEFORE_OPTIONS}"

            source2 = discord.FFmpegPCMAudio(stream_url, before_options=http_before, options=CLEAN_FFMPEG_OPTIONS)

            def after_c2(err):
                if err:
                    logger.error(f"[Test C3-C2] 串流播放例外: {err}")
                else:
                    logger.info("[Test C3-C2] 階段 2 串流播放完成。")

            vc.play(source2, after=after_c2)

            embed2 = discord.Embed(
                title=f"▶️ [Test C3 階段 2/2] C2 即時網路串流 (Format {c2_format_id})",
                description=f"歌曲: **[{title}]({info.get('webpage_url', url)})**\n\n一鍵兩測階段 2。請對比 C1 (階段1) 與 C2 (階段2) 之聽感差異！",
                color=discord.Color.green()
            )
            embed2.add_field(name="解析耗時", value=f"`{res_ms:.2f} ms`", inline=True)
            embed2.add_field(name="格式 ID", value=f"`{c2_format_id} ({c2_ext})`", inline=True)
            embed2.add_field(name="User-Agent", value=f"`{user_agent[:40]}...`", inline=False)

            if channel:
                await channel.send(embed=embed2)

            logger.info("==================== [Test C3 一鍵雙測] 全流程成功發動 ====================")

        except Exception as e:
            logger.error(f"[Test C3] 一鍵測試例外失敗: {e}", exc_info=True)
            if channel:
                await channel.send(f"❌ [Test C3] 一鍵測試失敗: {e}")

    @app_commands.command(name="test_c4", description="🧪 [Test C4] 測試 FFmpegOpusAudio.from_probe 線上串流 (比照正式播放工廠)")
    @app_commands.describe(url="測試的 YouTube 影片網址", format_id="指定格式 (預設 251: 純 WebM Opus；可選 18: MP4 AAC 360p)")
    @app_commands.choices(format_id=[
        app_commands.Choice(name="Format 251 (純 WebM Opus 48k 音訊)", value="251"),
        app_commands.Choice(name="Format 18 (MP4 AAC+H.264 影音混合檔)", value="18"),
    ])
    async def test_c4(self, interaction: discord.Interaction, url: str, format_id: str = "251"):
        vc = await self.ensure_voice(interaction)
        if not vc:
            return

        await interaction.response.defer()
        loop = asyncio.get_running_loop()

        logger.info(f"========== [Test C4] 開始 FFmpegOpusAudio.from_probe 測試 (URL: {url}, Format: {format_id}) ==========")

        def do_resolve():
            opts = {
                "format": format_id,
                "extract_flat": False,
                "noplaylist": True,
                "quiet": True,
            }
            with yt_dlp.YoutubeDL(opts) as ytdl:
                return ytdl.extract_info(url, download=False)

        try:
            t_res_0 = time.perf_counter()
            info = await loop.run_in_executor(None, do_resolve)
            res_ms = (time.perf_counter() - t_res_0) * 1000

            if not info or not info.get("url"):
                await interaction.followup.send(f"❌ [Test C4] 解析 Format `{format_id}` 失敗！")
                return

            stream_url = info.get("url")
            title = info.get("title", "未知曲名")
            actual_fmt = info.get("format_id", "N/A")
            ext = info.get("ext", "N/A")
            acodec = info.get("acodec", "N/A")
            vcodec = info.get("vcodec", "none")
            asr = info.get("asr", "N/A")
            user_agent = info.get("http_headers", {}).get("User-Agent", "")

            http_before = HTTP_BEFORE_OPTIONS
            if user_agent:
                http_before = f"-headers \"User-Agent: {user_agent}\r\n\" {HTTP_BEFORE_OPTIONS}"

            await self.cleanup_voice_player(vc)

            t_probe_0 = time.perf_counter()
            source = await discord.FFmpegOpusAudio.from_probe(
                stream_url,
                before_options=http_before,
                options=CLEAN_FFMPEG_OPTIONS
            )
            probe_ms = (time.perf_counter() - t_probe_0) * 1000

            def after_play(err):
                if err:
                    logger.error(f"[Test C4] 播放例外: {err}")
                else:
                    logger.info("[Test C4] 播放成功結束。")

            vc.play(source, after=after_play)

            embed = discord.Embed(
                title=f"▶️ [Test C4] 線上串流 (FFmpegOpusAudio.from_probe - Format {actual_fmt})",
                description=f"歌曲: **[{title}]({info.get('webpage_url', url)})**\n\n完全比照 `music.py` 正式路徑之 AudioSource 工廠。",
                color=discord.Color.teal()
            )
            embed.add_field(name="1. 網址解析耗時", value=f"`{res_ms:.2f} ms`", inline=True)
            embed.add_field(name="2. Probe 探測耗時", value=f"`{probe_ms:.2f} ms`", inline=True)
            embed.add_field(name="3. 格式資訊", value=f"`id: {actual_fmt}` | `{ext}` | 音訊: `{acodec} ({asr}Hz)` | 視訊: `{vcodec}`", inline=False)
            embed.add_field(name="4. 工廠類型", value="`discord.FFmpegOpusAudio.from_probe` (由 FFmpeg 輸出 Opus)", inline=False)
            embed.add_field(name="5. User-Agent", value=f"`{user_agent[:40]}...`", inline=False)

            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"[Test C4] 測試例外失敗: {e}", exc_info=True)
            await interaction.followup.send(f"❌ [Test C4] 執行失敗: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AudioTestCog(bot))
