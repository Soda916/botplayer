import os
import time
import asyncio
import logging
import subprocess
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

# 網路 HTTP 串流前置選項 (僅適用於 http/https 網址)
HTTP_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
# 乾淨 FFmpegPCM 參數 (避免與 discord.py 預設的 -f s16le -ar 48000 -ac 2 重複)
CLEAN_FFMPEG_OPTIONS = "-vn -af aresample=48000:async=1"


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

    # ------------------ Slash Commands for A/B1/B2/B3/C ------------------

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
            "ffmpeg", "-y", "-i", filepath,
            "-ar", "48000", "-ac", "2", "-af", "aresample=48000:async=1",
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

    @app_commands.command(name="test_b1", description="🧪 [Test B1] 測試本機原生 48kHz 音訊直推 Discord (無 44.1k 重採樣/無 yt-dlp)")
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

        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await asyncio.sleep(0.1)

        try:
            # 本機檔案絕對不能傳入 -reconnect 1 !
            source = discord.FFmpegPCMAudio(filepath, options=CLEAN_FFMPEG_OPTIONS)

            def after_play(err):
                if err:
                    logger.error(f"[Test B1] 播放例外: {err}")

            vc.play(source, after=after_play)

            embed = discord.Embed(
                title="▶️ [Test B1] 本機 48kHz 直推播放中",
                description=f"檔案: `{os.path.basename(filepath)}`\n\n已移除本機檔不相容之 `-reconnect` 參數與重複 `-ar/-ac` 標籤。",
                color=discord.Color.blue()
            )
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            await interaction.response.send_message(f"❌ 啟動 Test B1 失敗: {e}", ephemeral=True)

    @app_commands.command(name="test_b2", description="🧪 [Test B2] 測試本機 44.1kHz -> FFmpeg Resample 48kHz -> Discord (無 yt-dlp)")
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

        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await asyncio.sleep(0.1)

        try:
            # 本機檔案絕對不能傳入 -reconnect 1 !
            source = discord.FFmpegPCMAudio(filepath, options=CLEAN_FFMPEG_OPTIONS)

            def after_play(err):
                if err:
                    logger.error(f"[Test B2] 播放例外: {err}")

            vc.play(source, after=after_play)

            embed = discord.Embed(
                title="▶️ [Test B2] 本機 44.1kHz -> FFmpeg Resample -> Discord 播放中",
                description=f"檔案: `{os.path.basename(filepath)}`\n\n專門驗證本機 44.1k 音訊轉碼 48k 餵給 Discord Voice 之穩定度。",
                color=discord.Color.purple()
            )
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            await interaction.response.send_message(f"❌ 啟動 Test B2 失敗: {e}", ephemeral=True)

    @app_commands.command(name="test_b3", description="🧪 [Test B3] 測試本機 48kHz -> FFmpeg 完整管道 -> Discord (無 44.1k 重採樣/無 yt-dlp)")
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

        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await asyncio.sleep(0.1)

        try:
            # 本機檔案絕對不能傳入 -reconnect 1 !
            source = discord.FFmpegPCMAudio(filepath, options=CLEAN_FFMPEG_OPTIONS)

            def after_play(err):
                if err:
                    logger.error(f"[Test B3] 播放例外: {err}")

            vc.play(source, after=after_play)

            embed = discord.Embed(
                title="▶️ [Test B3] 本機 48kHz -> FFmpeg 完整管道 播放中",
                description=f"檔案: `{os.path.basename(filepath)}`\n\n驗證 48kHz 原生音訊經由完整 FFmpeg 管道餵給 Discord 之穩定度。",
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            await interaction.response.send_message(f"❌ 啟動 Test B3 失敗: {e}", ephemeral=True)

    @app_commands.command(name="test_c", description="🧪 [Test C] 測試 yt-dlp 網路串流 -> FFmpeg -> Discord 完整管線")
    @app_commands.describe(query="測試的 YouTube 影片關鍵字或網址")
    async def test_c(self, interaction: discord.Interaction, query: str = "NRQRC_0ZQ00"):
        vc = await self.ensure_voice(interaction)
        if not vc:
            return

        await interaction.response.defer()

        loop = asyncio.get_running_loop()

        def extract():
            opts = {
                "format": "bestaudio/best",
                "extract_flat": False,
                "noplaylist": True,
                "quiet": True,
                # 優先使用 tv, web_creator 避開 PO Token 與 JS Challenge
                "extractor_args": {"youtube": {"player_client": ["tv", "web_creator", "ios", "mweb", "android"]}}
            }
            with yt_dlp.YoutubeDL(opts) as ytdl:
                info = ytdl.extract_info(query, download=False)
                if not info:
                    return None
                if "entries" in info:
                    entries = [e for e in info["entries"] if e]
                    if not entries: return None
                    info = entries[0]
                return info

        try:
            data = await loop.run_in_executor(None, extract)
            if not data or not data.get("url"):
                await interaction.followup.send("❌ Test C 解析 YouTube 音源網址失敗！")
                return

            stream_url = data.get("url")
            title = data.get("title", "未知曲名")

            if vc.is_playing() or vc.is_paused():
                vc.stop()
                await asyncio.sleep(0.1)

            # 網路 HTTP 串流才傳入 HTTP_BEFORE_OPTIONS (-reconnect 1)
            source = discord.FFmpegPCMAudio(stream_url, before_options=HTTP_BEFORE_OPTIONS, options=CLEAN_FFMPEG_OPTIONS)

            def after_play(err):
                if err:
                    logger.error(f"[Test C] 播放例外: {err}")

            vc.play(source, after=after_play)

            embed = discord.Embed(
                title="▶️ [Test C] yt-dlp 即時網路串流 播放中",
                description=f"歌曲: **[{title}]({data.get('webpage_url', query)})**\n\n已帶入優化之 `tv`/`web_creator` Client 序列與網路 reconnect 參數。",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ 啟動 Test C 失敗: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AudioTestCog(bot))
