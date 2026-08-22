import os
import time
import random
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional
import discord
from discord.ext import commands, tasks
from discord import app_commands
import yt_dlp

logger = logging.getLogger("botplayer.music")

# 從 .env 讀取 OWNER_ID (如未設定或無效則為 0)
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID", "").isdigit() else 0

# 機器人訊息自動延時刪除秒數 (防訊息洗版，維持播放面板在底部)
AUTO_DELETE_DELAY = 10.0


def format_seconds(secs: int) -> str:
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def build_progress_bar(elapsed: int, duration: int, total_length: int = 12) -> str:
    """生成真實時間播放進度條 (格式: 01:23 ▬▬▬🔘▬▬▬▬▬▬ 03:45)"""
    elapsed_str = format_seconds(elapsed)
    if duration <= 0:
        return f"`{elapsed_str}` 🔘" + "▬" * (total_length - 1) + " `直播/未知`"

    duration_str = format_seconds(duration)
    capped_elapsed = min(elapsed, duration)
    ratio = max(0.0, min(capped_elapsed / duration, 1.0))

    filled = int(ratio * (total_length - 1))
    unfilled = (total_length - 1) - filled
    bar = "▬" * filled + "🔘" + "▬" * unfilled
    return f"`{elapsed_str}` {bar} `{duration_str}`"




# 相對專案根目錄解析絕對路徑，防範從不同 CWD 啟動時找不到 cookies.txt
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_COOKIE_PATH = os.path.join(BASE_DIR, "cookies.txt")
COOKIE_PATH = os.getenv("YOUTUBE_COOKIE_PATH", DEFAULT_COOKIE_PATH)
COOKIES_TEXT = os.getenv("YOUTUBE_COOKIES_TEXT", "")

if not os.path.exists(COOKIE_PATH) and COOKIES_TEXT.strip():
    try:
        with open(COOKIE_PATH, "w", encoding="utf-8") as f:
            f.write(COOKIES_TEXT.strip() + "\n")
    except Exception as e:
        logger.warning(f"自動寫入 .env COOKIES_TEXT 失敗: {e}")

# yt-dlp 最佳化設定：極致輕量化，只解析音訊，嚴禁下載與畫面處理，停用播放清單
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "extract_flat": False,
    "noplaylist": True,  # 嚴格不支援播放清單解析
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
    "extractor_args": {
        "youtube": {
            "player_client": ["tv", "web_creator", "ios", "mweb", "android"]
        }
    },
}

if os.path.exists(COOKIE_PATH) and os.path.getsize(COOKIE_PATH) > 0:
    YTDL_OPTIONS["cookiefile"] = COOKIE_PATH



# FFmpeg 串流優化設定：強制帶入 -map 0:a:0 -vn -af aresample=48000，拋棄影音檔(Format 18 MP4)之視訊軌解碼，避開 CPU 雙核跑滿與開頭卡頓
FFMPEG_OPTIONS_HTTP = "-threads 2 -probesize 1M -analyzeduration 2000000 -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS_LOCAL = "-threads 2 -probesize 4M -analyzeduration 4000000"
FFMPEG_OPTIONS_CLEAN = "-map 0:a:0 -vn -af aresample=48000"

FFMPEG_OPTIONS = {
    "before_options": FFMPEG_OPTIONS_HTTP,
    "options": FFMPEG_OPTIONS_CLEAN,
}

def get_ffmpeg_before_options(stream_url: str, offset_seconds: int = 0, user_agent: str = None) -> str:
    is_http = stream_url.startswith("http://") or stream_url.startswith("https://")
    base = FFMPEG_OPTIONS_HTTP if is_http else FFMPEG_OPTIONS_LOCAL
    if is_http and user_agent:
        base = f"-headers \"User-Agent: {user_agent}\\r\\n\" {base}"
    if offset_seconds > 0:
        return f"-ss {offset_seconds} {base}"
    return base





@dataclass
class Track:
    title: str
    url: str
    stream_url: str
    duration: int
    requester: discord.Member
    thumbnail: Optional[str] = None

    def format_duration(self) -> str:
        if not self.duration:
            return "直播 / 未知"
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"


class MusicControlView(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int):
        super().__init__(timeout=None)  # 面板持續有效
        self.cog = cog
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 檢查點歌者/觸發者是否在語音頻道內
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "❌ 你必須在語音頻道內才能使用播放面板控制按鈕！",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="music_play_pause")
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog.get_player(self.guild_id)
        vc = player.voice_client

        if not vc or not vc.is_connected():
            await interaction.response.send_message("❌ 機器人目前未連線至語音頻道。", ephemeral=True)
            return

        if vc.is_paused():
            player.on_resume()
            vc.resume()
            await interaction.response.send_message("▶️ 已恢復播放音樂。", ephemeral=True)
        elif vc.is_playing():
            player.on_pause()
            vc.pause()
            await interaction.response.send_message("⏸️ 已暫停播放音樂。", ephemeral=True)

        else:
            await interaction.response.send_message("⚠️ 目前沒有在播放音樂。", ephemeral=True)

        await self.cog.update_panel(self.guild_id)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="music_prev")
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog.get_player(self.guild_id)
        if not player.history:
            await interaction.response.send_message("⚠️ 目前沒有上一首播放紀錄！", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        success = await self.cog.play_previous(self.guild_id)
        if success:
            await interaction.followup.send("⏮️ 已切換至上一首歌曲。", ephemeral=True)
        else:
            await interaction.followup.send("❌ 切換至上一首失敗。", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music_next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog.get_player(self.guild_id)
        vc = player.voice_client

        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("⚠️ 目前沒有在播放音樂，無法跳過！", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        vc.stop()  # 觸發 after 回呼自動播放下一首
        await interaction.followup.send("⏭️ 已跳過當前歌曲。", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.stop_player(self.guild_id)
        await interaction.followup.send("⏹️ 已停止播放並清空待播清單。", ephemeral=True)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="music_queue")
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.cog.build_queue_embed(self.guild_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class GuildPlayer:
    def __init__(self, guild_id: int, bot: Optional[commands.Bot] = None):
        self.guild_id: int = guild_id
        self.bot: Optional[commands.Bot] = bot
        self._voice_client: Optional[discord.VoiceClient] = None
        self.queue: List[Track] = []
        self.history: List[Track] = []
        self.current_track: Optional[Track] = None
        self.panel_message: Optional[discord.Message] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.idle_timer_task: Optional[asyncio.Task] = None
        self.idle_timeout_minutes: int = 5  # 預設閒置 5 分鐘自動退出
        self.play_lock: asyncio.Lock = asyncio.Lock()  # 防並行點播競態衝堂鎖
        self.connect_lock: asyncio.Lock = asyncio.Lock()  # Single-flight 語音連線防併發鎖

        # 狀態機意圖追蹤 (Desired State)
        self.desired_connected: bool = False
        self.desired_channel_id: Optional[int] = None

        # 進度條實時時間追蹤
        self.track_start_time: float = 0.0
        self.paused_time_total: float = 0.0
        self.pause_start_time: Optional[float] = None

    @property
    def voice_client(self) -> Optional[discord.VoiceClient]:
        """以 Discord Guild 即時 voice_client 為單一真實來源，防長連線與熱重載狀態脫節"""
        if self.bot:
            guild = self.bot.get_guild(self.guild_id)
            if guild and guild.voice_client:
                return guild.voice_client
        return self._voice_client

    @voice_client.setter
    def voice_client(self, value: Optional[discord.VoiceClient]):
        self._voice_client = value

    def get_elapsed_seconds(self) -> int:
        if not self.current_track or not self.track_start_time:
            return 0
        if self.pause_start_time is not None:
            elapsed = self.pause_start_time - self.track_start_time - self.paused_time_total
        else:
            elapsed = time.time() - self.track_start_time - self.paused_time_total
        elapsed_int = max(0, int(elapsed))
        if self.current_track.duration > 0:
            elapsed_int = min(elapsed_int, self.current_track.duration)
        return elapsed_int

    def on_track_start(self):
        self.track_start_time = time.time()
        self.paused_time_total = 0.0
        self.pause_start_time = None

    def on_pause(self):
        if self.pause_start_time is None:
            self.pause_start_time = time.time()

    def on_resume(self):
        if self.pause_start_time is not None:
            self.paused_time_total += (time.time() - self.pause_start_time)
            self.pause_start_time = None



class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: Dict[int, GuildPlayer] = {}
        self.progress_loop.start()

    def cog_unload(self):
        self.progress_loop.cancel()
        for player in self.players.values():
            self.cancel_idle_timer(player)

    @tasks.loop(seconds=5)
    async def progress_loop(self):
        """音樂播放進度條輪詢與語音連線看門狗 (Watchdog)"""
        for guild_id, player in list(self.players.items()):
            try:
                vc = player.voice_client
                # 1. 播放中即時進度條更新
                if vc and vc.is_connected() and vc.is_playing():
                    await self.update_panel(guild_id)
                elif player.desired_connected and player.current_track:
                    # 2. 連線異常/假死看門狗自癒 (Supervisor Watchdog)
                    if not vc or not vc.is_connected():
                        guild = self.bot.get_guild(guild_id)
                        if guild and player.desired_channel_id:
                            ch = guild.get_channel(player.desired_channel_id)
                            if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                                logger.info(f"Watchdog 偵測到 Guild {guild_id} 連線異常中斷，自動觸發連線自癒...")
                                loop = asyncio.get_running_loop()
                                loop.create_task(self.ensure_voice_connected(ch))
            except Exception:
                pass

    def export_playback_state(self) -> dict:
        """導出所有活躍伺服器的播放狀態與佇列資訊供熱重載接續"""
        state = {}
        for guild_id, player in self.players.items():
            vc = player.voice_client
            channel_id = vc.channel.id if (vc and vc.channel) else None
            if not channel_id:
                continue

            current_dict = None
            if player.current_track:
                current_dict = {
                    "title": player.current_track.title,
                    "url": player.current_track.url,
                    "stream_url": player.current_track.stream_url,
                    "duration": player.current_track.duration,
                    "thumbnail": player.current_track.thumbnail,
                    "requester_id": player.current_track.requester.id if player.current_track.requester else None,
                    "elapsed_seconds": player.get_elapsed_seconds(),
                }

            queue_list = [
                {
                    "title": t.title,
                    "url": t.url,
                    "stream_url": t.stream_url,
                    "duration": t.duration,
                    "thumbnail": t.thumbnail,
                    "requester_id": t.requester.id if t.requester else None,
                }
                for t in player.queue
            ]

            history_list = [
                {
                    "title": t.title,
                    "url": t.url,
                    "stream_url": t.stream_url,
                    "duration": t.duration,
                    "thumbnail": t.thumbnail,
                    "requester_id": t.requester.id if t.requester else None,
                }
                for t in player.history
            ]

            state[guild_id] = {
                "voice_channel_id": channel_id,
                "text_channel_id": player.text_channel.id if player.text_channel else None,
                "desired_connected": player.desired_connected,
                "desired_channel_id": player.desired_channel_id or channel_id,
                "is_playing": (vc.is_playing() if vc else False),
                "is_paused": (vc.is_paused() if vc else False),
                "current_track": current_dict,
                "queue": queue_list,
                "history": history_list,
                "idle_timeout_minutes": player.idle_timeout_minutes,
            }
        return state

    async def restore_playback_state(self, state: dict) -> None:
        """根據 state 快照恢復語音連線與播放佇列"""
        for guild_id, gdata in state.items():
            guild = self.bot.get_guild(int(guild_id))
            if not guild:
                continue

            voice_channel_id = gdata.get("voice_channel_id")
            voice_channel = guild.get_channel(voice_channel_id) if voice_channel_id else None
            if not voice_channel or not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
                continue

            player = self.get_player(guild.id)
            player.idle_timeout_minutes = gdata.get("idle_timeout_minutes", 5)
            player.desired_connected = gdata.get("desired_connected", True)
            player.desired_channel_id = gdata.get("desired_channel_id", voice_channel.id)

            text_channel_id = gdata.get("text_channel_id")
            if text_channel_id:
                txt_ch = guild.get_channel(text_channel_id)
                if isinstance(txt_ch, discord.TextChannel):
                    player.text_channel = txt_ch

            try:
                vc = await self.ensure_voice_connected(voice_channel)
                if not vc:
                    continue
            except Exception as e:
                logger.error(f"恢復語音連線失敗 [Guild {guild.id}]: {e}")
                continue

            def make_track(tdict: dict) -> Track:
                req_id = tdict.get("requester_id")
                req_member = guild.get_member(req_id) if req_id else None
                if not req_member:
                    req_member = guild.me
                return Track(
                    title=tdict.get("title", "未知曲名"),
                    url=tdict.get("url", ""),
                    stream_url=tdict.get("stream_url", ""),
                    duration=tdict.get("duration", 0),
                    requester=req_member,
                    thumbnail=tdict.get("thumbnail"),
                )

            player.history = [make_track(t) for t in gdata.get("history", [])]
            player.queue = [make_track(t) for t in gdata.get("queue", [])]

            curr_data = gdata.get("current_track")
            was_playing = gdata.get("is_playing", False)
            was_paused = gdata.get("is_paused", False)

            if curr_data and (was_playing or was_paused):
                curr_track = make_track(curr_data)
                elapsed = curr_data.get("elapsed_seconds", 0)
                self.play_track_with_offset(guild.id, curr_track, offset_seconds=elapsed, paused=was_paused)
                if player.text_channel:
                    try:
                        await self.send_new_panel(guild.id, player.text_channel)
                        await player.text_channel.send(
                            f"🔄 **熱更新完成**：已自動接續播放 [{curr_track.title}]({curr_track.url})（從 `{curr_track.format_duration()}` 斷點接續）。",
                            delete_after=AUTO_DELETE_DELAY,
                        )
                    except Exception:
                        pass
            else:
                self.start_idle_timer(player)

    async def create_audio_source(self, stream_url: str, offset_seconds: int = 0) -> discord.AudioSource:
        """創設音訊源：優先使用 FFmpegOpusAudio (C 原生 Opus 轉碼)，徹底消除 CPU 時脈漂移導致之時快時慢問題"""
        ffmpeg_before = get_ffmpeg_before_options(stream_url, offset_seconds)
        try:
            return await discord.FFmpegOpusAudio.from_probe(
                stream_url,
                before_options=ffmpeg_before,
                options=FFMPEG_OPTIONS_CLEAN,
            )
        except Exception as e:
            logger.warning(f"FFmpegOpusAudio 原生創設失敗，降級使用 FFmpegPCMAudio: {e}")
            return discord.FFmpegPCMAudio(
                stream_url,
                before_options=ffmpeg_before,
                options=FFMPEG_OPTIONS_CLEAN,
            )

    def play_track_with_offset(self, guild_id: int, track: Track, offset_seconds: int = 0, paused: bool = False):
        """從指定秒數斷點接續播放歌曲"""
        player = self.get_player(guild_id)
        vc = player.voice_client
        if not vc or not vc.is_connected():
            return

        self.cancel_idle_timer(player)
        player.current_track = track

        async def start_play():
            for _ in range(20):
                if not (vc.is_playing() or vc.is_paused()):
                    break
                vc.stop()
                await asyncio.sleep(0.05)

            try:
                source = await self.create_audio_source(track.stream_url, offset_seconds)

                def after_playing(error):
                    if error:
                        logger.error(f"FFmpeg 播放例外: {error}")
                    self.play_next_track(guild_id)

                player.on_track_start()
                if offset_seconds > 0:
                    player.track_start_time = time.time() - offset_seconds

                vc.play(source, after=after_playing)
                if paused:
                    vc.pause()
                    player.on_pause()
                await self.update_panel(guild_id)
            except Exception as e:
                logger.error(f"斷點播放音訊失敗 [{track.title}]: {e}")
                self.play_next_track(guild_id)

        loop = asyncio.get_running_loop()
        loop.create_task(start_play())


    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(guild_id, self.bot)
        return self.players[guild_id]

    async def ensure_voice_connected(self, voice_channel: discord.VoiceChannel, max_retries: int = 3) -> Optional[discord.VoiceClient]:
        """
        語音連線 Supervisor：確保語音連線符合 desired_state。
        具備 Single-Flight 防併發鎖、Non-recoverable 權限熔斷、與 Exponential Backoff + Jitter 指數退避機制。
        """
        guild = voice_channel.guild
        player = self.get_player(guild.id)

        # 1. Non-recoverable 權限與頻道存在性先驗檢查 (Fail-Fast 熔斷防死循環)
        if not guild.me:
            logger.error(f"Guild {guild.id} 找不到 Bot 成員，無法建立語音連線。")
            player.desired_connected = False
            return None

        perms = voice_channel.permissions_for(guild.me)
        if not perms.connect:
            logger.warning(f"缺少語音頻道 [{voice_channel.name}] 的 Connect 連線權限，放棄重連。")
            player.desired_connected = False
            return None

        # 2. Single-Flight 併發鎖：防止多個事件/指令同時觸發多重連線
        async with player.connect_lock:
            player.desired_connected = True
            player.desired_channel_id = voice_channel.id

            vc = guild.voice_client

            # 若連線健康活躍且已在目標頻道
            if vc and isinstance(vc, discord.VoiceClient) and vc.is_connected():
                if vc.channel != voice_channel:
                    try:
                        await vc.move_to(voice_channel)
                    except Exception as e:
                        logger.warning(f"移動至語音頻道失敗 [{voice_channel.name}]: {e}")

                if isinstance(voice_channel, discord.StageChannel):
                    try:
                        if guild.me.voice and guild.me.voice.suppressed:
                            await guild.me.edit(suppress=False)
                    except Exception as e:
                        logger.warning(f"Stage 頻道申請開麥發言失敗: {e}")
                return vc

            # 3. 徹底清理失效/殭屍舊連線
            if vc:
                try:
                    if vc.is_playing() or vc.is_paused():
                        vc.stop()
                    await vc.disconnect(force=True)
                except Exception as e:
                    logger.warning(f"強制中斷失效語音連線失敗: {e}")
                await asyncio.sleep(0.2)

            # 4. 指數退避與抖動重試 (Exponential Backoff + Jitter)
            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(f"正在建立語音連線 [Guild {guild.id} -> {voice_channel.name}] (嘗試 #{attempt})...")
                    vc = await voice_channel.connect(reconnect=True, self_deaf=True, timeout=15.0)

                    if isinstance(voice_channel, discord.StageChannel) and guild.me:
                        try:
                            if guild.me.voice and guild.me.voice.suppressed:
                                await guild.me.edit(suppress=False)
                        except Exception:
                            pass

                    logger.info(f"語音連線成功建立 [Guild {guild.id}]。")
                    return vc
                except Exception as e:
                    logger.warning(f"語音連線嘗試 #{attempt} 失敗: {e}")
                    if guild.voice_client:
                        try:
                            await guild.voice_client.disconnect(force=True)
                        except Exception:
                            pass

                    if attempt < max_retries:
                        # 指數退避: 1s, 2s, 4s + 0.1~0.5s Jitter 抖動防雪崩
                        backoff = (2 ** (attempt - 1)) + random.uniform(0.1, 0.5)
                        logger.info(f"等待 {backoff:.2f} 秒後進行下一次連線重試...")
                        await asyncio.sleep(backoff)

            logger.error(f"達到最大重試次數 ({max_retries})，建立語音連線失敗 [Guild {guild.id}]。")
            return None

    async def extract_yt_tracks(self, query: str, requester: discord.Member) -> List[Track]:
        """使用 yt-dlp 非同步極速解壓網址 (支援單曲與播放清單 Rapid Flat Unpacking，防卡死)"""
        loop = asyncio.get_running_loop()

        def fetch():
            opts = YTDL_OPTIONS.copy()
            opts["extract_flat"] = True
            opts["noplaylist"] = False
            with yt_dlp.YoutubeDL(opts) as ytdl:
                info = ytdl.extract_info(query, download=False)
                if not info:
                    return []
                if "entries" in info:
                    entries = [e for e in info["entries"] if e]
                    return entries
                return [info]

        try:
            data_list = await loop.run_in_executor(None, fetch)
            if not data_list:
                return []

            tracks = []
            for data in data_list:
                url = data.get("webpage_url") or data.get("url")
                if not url and data.get("id"):
                    url = f"https://www.youtube.com/watch?v={data.get('id')}"
                if not url:
                    continue

                title = data.get("title", "未知曲名")
                stream_url = data.get("url") if not data.get("is_flat", True) else ""

                tracks.append(
                    Track(
                        title=title,
                        url=url,
                        stream_url=stream_url or "",
                        duration=int(data.get("duration", 0)),
                        requester=requester,
                        thumbnail=data.get("thumbnail"),
                    )
                )
            return tracks
        except Exception as e:
            logger.error(f"解析音訊清單失敗 [{query}]: {e}")
            return []

    async def resolve_stream_url(self, track: Track) -> Optional[Track]:
        """即時 (On-Demand) 解析單曲的 FFmpeg 音訊串流網址"""
        if track.stream_url:
            return track

        loop = asyncio.get_running_loop()

        def fetch():
            opts = YTDL_OPTIONS.copy()
            opts["noplaylist"] = True
            opts["extract_flat"] = False
            with yt_dlp.YoutubeDL(opts) as ytdl:
                info = ytdl.extract_info(track.url, download=False)
                if not info:
                    return None
                if "entries" in info:
                    entries = [e for e in info["entries"] if e]
                    if not entries:
                        return None
                    info = entries[0]
                return info

        try:
            data = await loop.run_in_executor(None, fetch)
            if not data or not data.get("url"):
                return None

            track.stream_url = data.get("url")
            if not track.title or track.title == "未知曲名":
                track.title = data.get("title", track.title)
            if not track.duration:
                track.duration = int(data.get("duration", 0))
            if not track.thumbnail:
                track.thumbnail = data.get("thumbnail")
            return track
        except Exception as e:
            logger.error(f"即時解析歌曲串流網址失敗 [{track.url}]: {e}")
            return None


    def build_panel_embed(self, guild_id: int) -> discord.Embed:
        player = self.get_player(guild_id)
        track = player.current_track

        if not track:
            embed = discord.Embed(
                title="🎵 播放操作面板",
                description="目前沒有在播放音樂。\n使用 `/play` 指令開始點歌！",
                color=discord.Color.dark_gray(),
            )
            return embed

        status = "▶️ 播放中"
        if player.voice_client and player.voice_client.is_paused():
            status = "⏸️ 已暫停"

        elapsed = player.get_elapsed_seconds()
        progress_bar = build_progress_bar(elapsed, track.duration)

        embed = discord.Embed(
            title="🎵 正在播放歌曲",
            description=f"**[{track.title}]({track.url})**\n\n{progress_bar}",
            color=discord.Color.blue(),
        )
        embed.add_field(name="狀態", value=status, inline=True)
        embed.add_field(name="長度", value=track.format_duration(), inline=True)
        embed.add_field(name="點歌者", value=track.requester.mention, inline=True)

        queue_count = len(player.queue)
        embed.add_field(
            name="待播佇列",
            value=f"共 `{queue_count}` 首歌曲等待中",
            inline=False,
        )

        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

        embed.set_footer(text="GCP e2-micro 輕量播放模式 | 僅解析 YouTube 音源")
        return embed


    def build_queue_embed(self, guild_id: int) -> discord.Embed:
        player = self.get_player(guild_id)
        embed = discord.Embed(title="📜 待播清單 (Queue)", color=discord.Color.gold())

        if player.current_track:
            embed.add_field(
                name="🔊 目前播放",
                value=f"**[{player.current_track.title}]({player.current_track.url})** | 點歌者: {player.current_track.requester.mention}",
                inline=False,
            )

        if not player.queue:
            embed.add_field(name="待播隊列", value="目前佇列空空如也~", inline=False)
        else:
            queue_list = []
            for idx, tr in enumerate(player.queue[:10], start=1):
                queue_list.append(f"`{idx}.` [{tr.title}]({tr.url}) ({tr.format_duration()}) - {tr.requester.mention}")
            
            queue_text = "\n".join(queue_list)
            if len(player.queue) > 10:
                queue_text += f"\n*...以及另外 {len(player.queue) - 10} 首歌曲*"

            embed.add_field(name=f"📋 下一首待播 (共 {len(player.queue)} 首)", value=queue_text, inline=False)

        return embed

    async def update_panel(self, guild_id: int):
        """更新發送在頻道中的動態播放面板"""
        player = self.get_player(guild_id)
        if not player.panel_message or not player.text_channel:
            return

        embed = self.build_panel_embed(guild_id)
        view = MusicControlView(self, guild_id)

        try:
            await player.panel_message.edit(embed=embed, view=view)
        except discord.NotFound:
            player.panel_message = None
        except Exception as e:
            logger.warning(f"更新播放面板失敗: {e}")

    async def send_new_panel(self, guild_id: int, text_channel: discord.TextChannel):
        """發送全新的控制面板並將舊面板刪除/更新"""
        player = self.get_player(guild_id)
        player.text_channel = text_channel

        embed = self.build_panel_embed(guild_id)
        view = MusicControlView(self, guild_id)

        try:
            if player.panel_message:
                try:
                    await player.panel_message.delete()
                except Exception:
                    pass
            player.panel_message = await text_channel.send(embed=embed, view=view)
        except Exception as e:
            logger.error(f"發送播放面板失敗: {e}")

    def play_next_track(self, guild_id: int):
        """內部播放迴圈排程觸發點 (支援安全事件迴圈叫用與異常捕獲)"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.play_next_track_async(guild_id))
        except RuntimeError:
            asyncio.run_coroutine_threadsafe(self.play_next_track_async(guild_id), self.bot.loop)

    async def play_next_track_async(self, guild_id: int):
        """即時解鎖音源並播放下一首歌曲 (支援開關防卡死自動跳過、連線自癒與並行鎖防衝堂)"""
        player = self.get_player(guild_id)

        async with player.play_lock:
            vc = player.voice_client

            # 若連線失效但有待播歌曲且有記錄的頻道，嘗試自癒連線
            if not vc or not vc.is_connected():
                guild = self.bot.get_guild(guild_id)
                if guild and player.queue:
                    target_channel = (vc.channel if vc and vc.channel else None)
                    if not target_channel:
                        req = player.queue[0].requester
                        if req and hasattr(req, "voice") and req.voice and req.voice.channel:
                            target_channel = req.voice.channel
                    if target_channel:
                        logger.info(f"偵測到 Guild {guild_id} 語音連線失效，執行播放前連線自癒...")
                        vc = await self.ensure_voice_connected(target_channel)

            if not vc or not vc.is_connected():
                logger.warning(f"Guild {guild_id} 無法取得有效語音連線，停止播放。")
                return

            # 把當前歌曲推進 history
            if player.current_track:
                player.history.append(player.current_track)
                if len(player.history) > 20:  # 限制歷史紀錄上限以節省 RAM
                    player.history.pop(0)

            while player.queue:
                next_track = player.queue.pop(0)
                resolved_track = await self.resolve_stream_url(next_track)
                if not resolved_track:
                    logger.warning(f"跳過無法解析音訊的歌曲: {next_track.title} ({next_track.url})")
                    continue

                self.cancel_idle_timer(player)
                player.current_track = resolved_track

                # 如果前一首還在播放或停止中，等待舊音訊流完全釋放，防 ClientException: Already playing audio 衝堂
                for _ in range(20):
                    if not (vc.is_playing() or vc.is_paused()):
                        break
                    vc.stop()
                    await asyncio.sleep(0.05)

                try:
                    # 預熱發送 5 秒 Discord 靜音 Opus 幀 (250 幀, 每幀 20ms)，為 e2-micro 與 Standard Network 徹底打通 SSRC 通道，防止歌曲開頭 5~7 秒被卡掉
                    if hasattr(vc, "send_audio_packet"):
                        try:
                            for _ in range(250):
                                vc.send_audio_packet(b"\xF8\xFF\xFE", encode=False)
                                await asyncio.sleep(0.02)
                        except Exception:
                            pass

                    source = await self.create_audio_source(resolved_track.stream_url)

                    def after_playing(error):
                        if error:
                            logger.error(f"FFmpeg 播放例外: {error}")
                        self.play_next_track(guild_id)

                    player.on_track_start()
                    vc.play(source, after=after_playing)
                    await self.update_panel(guild_id)

                    # 觸發下一首歌曲背景非同步預載解析 (Preloading)，消除曲目切換延遲
                    if player.queue:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.preload_next_track(guild_id))

                    return
                except Exception as e:
                    logger.error(f"播放歌曲失敗 [{resolved_track.title}]: {e}")
                    continue

            # 無待播歌曲：進入播畢收尾狀態 (顯示 5 秒播畢面板後清空並切換至待機模式)
            last_track = player.current_track
            player.current_track = None

            if last_track and player.panel_message:
                try:
                    embed = discord.Embed(
                        title="✅ 歌曲播放完畢",
                        description=f"**[{last_track.title}]({last_track.url})**\n\n`{last_track.format_duration()}` ▬▬▬▬▬▬▬▬▬▬🔘 `{last_track.format_duration()}`",
                        color=discord.Color.green(),
                    )
                    embed.add_field(name="狀態", value="🏁 佇列已播畢", inline=True)
                    embed.add_field(name="長度", value=last_track.format_duration(), inline=True)
                    embed.add_field(name="點歌者", value=last_track.requester.mention, inline=True)
                    embed.add_field(name="待播佇列", value="共 `0` 首歌曲等待中", inline=False)
                    if last_track.thumbnail:
                        embed.set_thumbnail(url=last_track.thumbnail)
                    embed.set_footer(text="GCP e2-micro 輕量播放模式 | 5 秒後切換至待機面板")
                    view = MusicControlView(self, guild_id)
                    await player.panel_message.edit(embed=embed, view=view)
                except Exception:
                    pass

                await asyncio.sleep(5.0)

            # 5 秒後徹底清空並切換至空閒待機面板
            await self.update_panel(guild_id)
            self.start_idle_timer(player)

    async def preload_next_track(self, guild_id: int):
        """背景非同步預先解析佇列下一首歌的音訊串流網址 (預載加速)，消除換歌卡頓"""
        player = self.get_player(guild_id)
        if player.queue:
            next_track = player.queue[0]
            if not next_track.stream_url:
                try:
                    await self.resolve_stream_url(next_track)
                except Exception as e:
                    logger.warning(f"預載下一首歌曲失敗 [{next_track.title}]: {e}")

    async def play_previous(self, guild_id: int) -> bool:
        """播放上一首歌曲"""
        player = self.get_player(guild_id)
        vc = player.voice_client

        if not vc or not player.history:
            return False

        prev_track = player.history.pop()

        # 將當前歌曲塞回 queue 的第一位
        if player.current_track:
            player.queue.insert(0, player.current_track)

        player.queue.insert(0, prev_track)

        if vc.is_playing() or vc.is_paused():
            player.current_track = None  # 防止 after_playing 重複寫入 history
            vc.stop()
        else:
            self.play_next_track(guild_id)

        return True

    def start_idle_timer(self, player: GuildPlayer):
        """啟動閒置自動斷線計時器 (e2-micro 防閒置資源浪費)"""
        self.cancel_idle_timer(player)

        if player.idle_timeout_minutes <= 0:
            logger.info(f"Guild {player.guild_id} 設定為永不自動退出 (idle_timeout_minutes=0)，跳過開啟閒置計時器。")
            return

        async def idle_disconnect():
            timeout_secs = player.idle_timeout_minutes * 60
            await asyncio.sleep(timeout_secs)
            if player.voice_client and player.voice_client.is_connected():
                if not player.current_track and not player.queue:
                    logger.info(f"Guild {player.guild_id} 閒置超過 {player.idle_timeout_minutes} 分鐘，自動斷開語音連線以省資源。")
                    player.desired_connected = False
                    player.desired_channel_id = None
                    await player.voice_client.disconnect(force=True)
                    if player.text_channel:
                        try:
                            await player.text_channel.send(
                                f"💤 佇列已空且閒置超過 {player.idle_timeout_minutes} 分鐘，已自動離開語音頻道以節省系統資源。",
                                delete_after=AUTO_DELETE_DELAY,
                            )
                        except Exception:
                            pass

        player.idle_timer_task = self.bot.loop.create_task(idle_disconnect())

    def cancel_idle_timer(self, player: GuildPlayer):
        if player.idle_timer_task and not player.idle_timer_task.done():
            player.idle_timer_task.cancel()
            player.idle_timer_task = None

    async def stop_player(self, guild_id: int):
        """停止播放並清空待播清單（保持語音連線，啟動閒置倒數）"""
        player = self.get_player(guild_id)
        self.cancel_idle_timer(player)
        player.queue.clear()
        player.history.clear()
        player.current_track = None

        if player.voice_client and (player.voice_client.is_playing() or player.voice_client.is_paused()):
            player.voice_client.stop()

        self.start_idle_timer(player)
        await self.update_panel(guild_id)


    # ------------------ Slash Commands ------------------

    @app_commands.command(name="play", description="播放 YouTube 音樂 (僅解析音效，不包含畫面/播放清單)")
    @app_commands.describe(query="輸入 YouTube 影片關鍵字或網址")
    async def play(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ 你必須先加入一個語音頻道！", ephemeral=True)
            return

        voice_channel = interaction.user.voice.channel
        guild_id = interaction.guild_id
        player = self.get_player(guild_id)

        # 權限檢查
        permissions = voice_channel.permissions_for(interaction.guild.me)
        if not permissions.connect or not permissions.speak:
            await interaction.response.send_message("❌ 機器人缺少加入或在該語音頻道發言的權限！", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        # 確保連線至語音頻道 (自動自癒與重連)
        vc = await self.ensure_voice_connected(voice_channel)
        if not vc:
            msg = await interaction.followup.send("❌ 連線至語音頻道失敗，請確認機器人權限或稍後重試。")
            await msg.delete(delay=AUTO_DELETE_DELAY)
            return

        # 解析音訊 (支援單曲與播放清單極速解壓)
        tracks = await self.extract_yt_tracks(query, interaction.user)
        if not tracks:
            msg = await interaction.followup.send("❌ 無法解析該網址或搜尋結果，請確認輸入有效 YouTube 連結或關鍵字。")
            await msg.delete(delay=AUTO_DELETE_DELAY)
            return

        if (player.voice_client and (player.voice_client.is_playing() or player.voice_client.is_paused())) or player.current_track:
            # 正在播放或有曲目鎖定中，加入待播清單 (Queue)
            player.queue.extend(tracks)
            if len(tracks) == 1:
                msg = await interaction.followup.send(
                    f"✅ **已加入待播清單**：[{tracks[0].title}]({tracks[0].url}) (佇列位置: #{len(player.queue)})"
                )
            else:
                msg = await interaction.followup.send(
                    f"✅ **已加入播放清單**：成功新增 `{len(tracks)}` 首歌曲至待播佇列！"
                )
            await msg.delete(delay=AUTO_DELETE_DELAY)
            await self.update_panel(guild_id)
        else:
            # 目前空閒，直接開始播放
            player.queue.extend(tracks)
            if len(tracks) == 1:
                msg = await interaction.followup.send(f"🎶 **開始播放**：[{tracks[0].title}]({tracks[0].url})")
            else:
                msg = await interaction.followup.send(
                    f"🎶 **開始播放播放清單**：首曲 [{tracks[0].title}]({tracks[0].url})（共 `{len(tracks)}` 首）"
                )
            await msg.delete(delay=AUTO_DELETE_DELAY)
            await self.send_new_panel(guild_id, interaction.channel)
            self.play_next_track(guild_id)


    @app_commands.command(name="skip", description="跳過當前正在播放的歌曲")
    async def skip(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        vc = player.voice_client

        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("⚠️ 目前沒有在播放音樂。", ephemeral=True)
            return

        vc.stop()
        await interaction.response.send_message("⏭️ 已跳過當前歌曲。", delete_after=AUTO_DELETE_DELAY)

    @app_commands.command(name="prev", description="播放上一首歌曲")
    async def prev(self, interaction: discord.Interaction):
        await interaction.response.defer()
        success = await self.play_previous(interaction.guild_id)
        if success:
            msg = await interaction.followup.send("⏮️ 已切換至上一首歌曲。")
        else:
            msg = await interaction.followup.send("⚠️ 沒有上一首播放紀錄！")
        await msg.delete(delay=AUTO_DELETE_DELAY)

    @app_commands.command(name="pause", description="暫停播放音樂")
    async def pause(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if player.voice_client and player.voice_client.is_playing():
            player.on_pause()
            player.voice_client.pause()
            await self.update_panel(interaction.guild_id)
            await interaction.response.send_message("⏸️ 已暫停播放。", delete_after=AUTO_DELETE_DELAY)
        else:
            await interaction.response.send_message("⚠️ 目前沒有正在播放的音樂。", ephemeral=True)

    @app_commands.command(name="resume", description="恢復播放音樂")
    async def resume(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if player.voice_client and player.voice_client.is_paused():
            player.on_resume()
            player.voice_client.resume()
            await self.update_panel(interaction.guild_id)
            await interaction.response.send_message("▶️ 已恢復播放。", delete_after=AUTO_DELETE_DELAY)
        else:
            await interaction.response.send_message("⚠️ 目前音樂沒有處於暫停狀態。", ephemeral=True)


    @app_commands.command(name="stop", description="停止播放並清空待播清單")
    async def stop(self, interaction: discord.Interaction):
        await self.stop_player(interaction.guild_id)
        await interaction.response.send_message("⏹️ 已停止播放音樂並清空待播佇列。", delete_after=AUTO_DELETE_DELAY)

    @app_commands.command(name="queue", description="顯示當前待播清單 (Queue)")
    async def queue(self, interaction: discord.Interaction):
        embed = self.build_queue_embed(interaction.guild_id)
        await interaction.response.send_message(embed=embed, delete_after=AUTO_DELETE_DELAY)

    @app_commands.command(name="join", description="加入語音頻道並設定閒置自動退出時間")
    @app_commands.describe(timeout_minutes="閒置自動退出時間 (5-30 分鐘；輸入 0 代表永不退出 [僅 Owner 可設])")
    async def join(self, interaction: discord.Interaction, timeout_minutes: int = 5):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ 你必須先加入一個語音頻道！", ephemeral=True)
            return

        # 檢查是否為 Bot Owner (支援 .env 之 OWNER_ID 與 Discord App Owner)
        is_owner = (interaction.user.id == OWNER_ID) or (await self.bot.is_owner(interaction.user))

        if timeout_minutes == 0:
            if not is_owner:
                await interaction.response.send_message(
                    "❌ 「永不自動退出 (0 分鐘)」選項僅限 Bot 擁有者 (OWNER_ID) 設定！一般使用者可設定 5 至 30 分鐘。",
                    ephemeral=True,
                )
                return
        elif timeout_minutes < 5 or timeout_minutes > 30:
            await interaction.response.send_message(
                "❌ 閒置自動退出時間請設定在 5 至 30 分鐘之間（輸入 0 代表永不退出，僅限 Owner）。",
                ephemeral=True,
            )
            return

        voice_channel = interaction.user.voice.channel
        guild_id = interaction.guild_id
        player = self.get_player(guild_id)

        permissions = voice_channel.permissions_for(interaction.guild.me)
        if not permissions.connect or not permissions.speak:
            await interaction.response.send_message("❌ 機器人缺少加入或在該語音頻道發言的權限！", ephemeral=True)
            return

        await interaction.response.defer()

        # 確保連線至語音頻道 (自動自癒與重連)
        vc = await self.ensure_voice_connected(voice_channel)
        if not vc:
            msg = await interaction.followup.send("❌ 連線至語音頻道失敗，請稍後再試。")
            await msg.delete(delay=AUTO_DELETE_DELAY)
            return

        player.idle_timeout_minutes = timeout_minutes

        if timeout_minutes == 0:
            self.cancel_idle_timer(player)
            msg = await interaction.followup.send(f"🔊 已加入語音頻道 **{voice_channel.name}**！閒置退出模式設定為：**永不自動退出** ♾️")
        else:
            if not player.current_track and not player.queue:
                self.start_idle_timer(player)
            msg = await interaction.followup.send(f"🔊 已加入語音頻道 **{voice_channel.name}**！閒置自動退出時間設定為：**{timeout_minutes} 分鐘** ⏱️")
        await msg.delete(delay=AUTO_DELETE_DELAY)

    @app_commands.command(name="leave", description="離開語音頻道")
    async def leave(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        player.desired_connected = False
        player.desired_channel_id = None
        self.cancel_idle_timer(player)
        player.queue.clear()
        player.history.clear()
        player.current_track = None

        if player.voice_client:
            if player.voice_client.is_connected():
                player.voice_client.stop()
                await player.voice_client.disconnect(force=True)

        await self.update_panel(interaction.guild_id)
        await interaction.response.send_message("👋 已離開語音頻道。", delete_after=AUTO_DELETE_DELAY)


    # 監聽 Voice State 異動 (成員離線、頻道移動與 Bot 被踢出處理)
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        player = self.get_player(guild.id)

        # 1. 機器人自身狀態異動處理
        if member.id == self.bot.user.id:
            # Bot 被踢出語音頻道或斷線
            if before.channel and not after.channel:
                logger.info(f"Guild {guild.id} 機器人已離開語音頻道。")
                player.desired_connected = False
                player.desired_channel_id = None
                player.current_track = None
                player.queue.clear()
                self.cancel_idle_timer(player)
                await self.update_panel(member.guild.id)
                return

            # Bot 被管理員移動至其他語音頻道
            if before.channel and after.channel and before.channel != after.channel:
                logger.info(f"Guild {guild.id} 機器人被移動至頻道 {after.channel.name}")
                player.desired_channel_id = after.channel.id
                human_members = [m for m in after.channel.members if not m.bot]
                if len(human_members) == 0:
                    self.start_idle_timer(player)
                else:
                    if player.current_track or player.queue:
                        self.cancel_idle_timer(player)
                return

        # 2. 其他成員人口異動處理
        vc = player.voice_client
        if vc and vc.is_connected() and vc.channel:
            human_members = [m for m in vc.channel.members if not m.bot]
            if len(human_members) == 0:
                logger.info(f"Guild {guild.id} 語音頻道內已無真實成員，自動開啟斷線倒數。")
                self.start_idle_timer(player)
            else:
                # 若成員重新進入語音頻道，且有歌曲正在播放或佇列中，取消閒置倒數
                if player.current_track or player.queue:
                    self.cancel_idle_timer(player)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """當 Bot 被移出伺服器時，清理記憶體狀態以防 Memory Leak"""
        player = self.players.pop(guild.id, None)
        if player:
            self.cancel_idle_timer(player)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
