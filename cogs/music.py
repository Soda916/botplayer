import os
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional
import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp

logger = logging.getLogger("botplayer.music")

# 從 .env 讀取 OWNER_ID (如未設定或無效則為 0)
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID", "").isdigit() else 0


# yt-dlp 最佳化設定：極致輕量化，只解析音訊，嚴禁下載與畫面處理，停用播放清單
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "extract_flat": False,
    "noplaylist": True,  # 嚴格不支援播放清單解析
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}

# FFmpeg 串流優化設定：啟用網路斷線自動重連，僅輸入音訊流
FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
        "-probesize 64000 -analyzeduration 0"
    ),
    "options": "-vn",  # 絕不安裝/處理影片畫面，節省 CPU 與 RAM
}


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
            vc.resume()
            await interaction.response.send_message("▶️ 已恢復播放音樂。", ephemeral=True)
        elif vc.is_playing():
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
    def __init__(self, guild_id: int):
        self.guild_id: int = guild_id
        self.queue: List[Track] = []
        self.history: List[Track] = []
        self.current_track: Optional[Track] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.panel_message: Optional[discord.Message] = None
        self.text_channel: Optional[discord.TextChannel] = None
        self.idle_timer_task: Optional[asyncio.Task] = None
        self.idle_timeout_minutes: int = 5  # 預設閒置 5 分鐘自動退出



class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: Dict[int, GuildPlayer] = {}

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(guild_id)
        return self.players[guild_id]

    async def extract_yt_track(self, query: str, requester: discord.Member) -> Optional[Track]:
        """使用 yt-dlp 非同步解析 YouTube 音訊網址 (僅解析音源，防阻塞)"""
        loop = asyncio.get_running_loop()

        def fetch():
            with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ytdl:
                info = ytdl.extract_info(query, download=False)
                if not info:
                    return None
                # 若因搜尋或傳入清單而返回 entries，強行只取第一首
                if "entries" in info:
                    entries = [e for e in info["entries"] if e]
                    if not entries:
                        return None
                    info = entries[0]
                return info

        try:
            data = await loop.run_in_executor(None, fetch)
            if not data:
                return None

            stream_url = data.get("url")
            if not stream_url:
                return None

            return Track(
                title=data.get("title", "未知曲名"),
                url=data.get("webpage_url", query),
                stream_url=stream_url,
                duration=int(data.get("duration", 0)),
                requester=requester,
                thumbnail=data.get("thumbnail"),
            )
        except Exception as e:
            logger.error(f"解析音訊失敗 [{query}]: {e}")
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

        embed = discord.Embed(
            title="🎵 正在播放歌曲",
            description=f"**[{track.title}]({track.url})**",
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
        """內部播放迴圈關鍵點：播放完畢後的叫用"""
        player = self.get_player(guild_id)
        vc = player.voice_client

        if not vc or not vc.is_connected():
            return

        # 把當前歌曲推進 history
        if player.current_track:
            player.history.append(player.current_track)
            if len(player.history) > 20:  # 限制歷史紀錄上限以節省 RAM
                player.history.pop(0)

        # 佇列有下一首
        if player.queue:
            self.cancel_idle_timer(player)
            next_track = player.queue.pop(0)
            player.current_track = next_track

            source = discord.FFmpegPCMAudio(next_track.stream_url, **FFMPEG_OPTIONS)

            def after_playing(error):
                if error:
                    logger.error(f"FFmpeg 播放例外: {error}")
                # 使用 loop 安全調用下一次播放
                coro = self.update_panel(guild_id)
                fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
                try:
                    fut.result(5)
                except Exception:
                    pass
                self.play_next_track(guild_id)

            vc.play(source, after=after_playing)
            asyncio.run_coroutine_threadsafe(self.update_panel(guild_id), self.bot.loop)
        else:
            # 無待播歌曲，進入閒置狀態並啟動自動斷線計時器 (3分鐘)
            player.current_track = None
            asyncio.run_coroutine_threadsafe(self.update_panel(guild_id), self.bot.loop)
            self.start_idle_timer(player)

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
                    await player.voice_client.disconnect()
                    player.voice_client = None
                    if player.text_channel:
                        try:
                            await player.text_channel.send(f"💤 佇列已空且閒置超過 {player.idle_timeout_minutes} 分鐘，已自動離開語音頻道以節省系統資源。")
                        except Exception:
                            pass

        player.idle_timer_task = self.bot.loop.create_task(idle_disconnect())


    def cancel_idle_timer(self, player: GuildPlayer):
        if player.idle_timer_task and not player.idle_timer_task.done():
            player.idle_timer_task.cancel()
            player.idle_timer_task = None

    async def stop_player(self, guild_id: int):
        """清空佇列並斷開連線"""
        player = self.get_player(guild_id)
        self.cancel_idle_timer(player)
        player.queue.clear()
        player.history.clear()
        player.current_track = None

        if player.voice_client:
            if player.voice_client.is_connected():
                player.voice_client.stop()
                await player.voice_client.disconnect()
            player.voice_client = None

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

        # 確保連線至語音頻道
        if not player.voice_client or not player.voice_client.is_connected():
            try:
                player.voice_client = await voice_channel.connect(reconnect=True, self_deaf=True)
            except Exception as e:
                await interaction.followup.send(f"❌ 連線至語音頻道失敗: {e}")
                return
        elif player.voice_client.channel != voice_channel:
            await player.voice_client.move_to(voice_channel)

        # 解析音訊
        track = await self.extract_yt_track(query, interaction.user)
        if not track:
            await interaction.followup.send("❌ 無法解析該網址或搜尋結果，請確認輸入有效 YouTube 連結或關鍵字。")
            return

        if player.voice_client.is_playing() or player.voice_client.is_paused():
            # 正在播放中，加入待播清單 (Queue)
            player.queue.append(track)
            await interaction.followup.send(
                f"✅ **已加入待播清單**：[{track.title}]({track.url}) (佇列位置: #{len(player.queue)})"
            )
            await self.update_panel(guild_id)
        else:
            # 目前空閒，直接開始播放
            player.queue.append(track)
            await interaction.followup.send(f"🎶 **開始播放**：[{track.title}]({track.url})")
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
        await interaction.response.send_message("⏭️ 已跳過當前歌曲。")

    @app_commands.command(name="prev", description="播放上一首歌曲")
    async def prev(self, interaction: discord.Interaction):
        await interaction.response.defer()
        success = await self.play_previous(interaction.guild_id)
        if success:
            await interaction.followup.send("⏮️ 已切換至上一首歌曲。")
        else:
            await interaction.followup.send("⚠️ 沒有上一首播放紀錄！")

    @app_commands.command(name="pause", description="暫停播放音樂")
    async def pause(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if player.voice_client and player.voice_client.is_playing():
            player.voice_client.pause()
            await self.update_panel(interaction.guild_id)
            await interaction.response.send_message("⏸️ 已暫停播放。")
        else:
            await interaction.response.send_message("⚠️ 目前沒有正在播放的音樂。", ephemeral=True)

    @app_commands.command(name="resume", description="恢復播放音樂")
    async def resume(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if player.voice_client and player.voice_client.is_paused():
            player.voice_client.resume()
            await self.update_panel(interaction.guild_id)
            await interaction.response.send_message("▶️ 已恢復播放。")
        else:
            await interaction.response.send_message("⚠️ 目前音樂沒有處於暫停狀態。", ephemeral=True)

    @app_commands.command(name="stop", description="停止播放並清空待播清單與歷史")
    async def stop(self, interaction: discord.Interaction):
        await self.stop_player(interaction.guild_id)
        await interaction.response.send_message("⏹️ 已停止播放並離開語音頻道。")

    @app_commands.command(name="queue", description="顯示當前待播清單 (Queue)")
    async def queue(self, interaction: discord.Interaction):
        embed = self.build_queue_embed(interaction.guild_id)
        await interaction.response.send_message(embed=embed)

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

        if not player.voice_client or not player.voice_client.is_connected():
            try:
                player.voice_client = await voice_channel.connect(reconnect=True, self_deaf=True)
            except Exception as e:
                await interaction.followup.send(f"❌ 連線至語音頻道失敗: {e}")
                return
        elif player.voice_client.channel != voice_channel:
            await player.voice_client.move_to(voice_channel)


        player.idle_timeout_minutes = timeout_minutes

        if timeout_minutes == 0:
            self.cancel_idle_timer(player)
            await interaction.followup.send(f"🔊 已加入語音頻道 **{voice_channel.name}**！閒置退出模式設定為：**永不自動退出** ♾️")
        else:
            if not player.current_track and not player.queue:
                self.start_idle_timer(player)
            await interaction.followup.send(f"🔊 已加入語音頻道 **{voice_channel.name}**！閒置自動退出時間設定為：**{timeout_minutes} 分鐘** ⏱️")

    @app_commands.command(name="leave", description="離開語音頻道")
    async def leave(self, interaction: discord.Interaction):
        await self.stop_player(interaction.guild_id)
        await interaction.response.send_message("👋 已離開語音頻道。")


    # 監聽 Voice State 異動 (成員離線與 Bot 被強制踢出處理)
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.id == self.bot.user.id:
            # Bot 被踢出語音頻道或斷線
            if before.channel and not after.channel:
                player = self.get_player(member.guild.id)
                player.voice_client = None
                player.current_track = None
                player.queue.clear()
                self.cancel_idle_timer(player)
                await self.update_panel(member.guild.id)
            return

        # 若語音頻道中只剩下 Bot 人口 (其他成員全部離開)
        guild = member.guild
        player = self.get_player(guild.id)
        vc = player.voice_client

        if vc and vc.is_connected() and vc.channel:
            # 過濾掉 Bot 成員
            human_members = [m for m in vc.channel.members if not m.bot]
            if len(human_members) == 0:
                logger.info(f"Guild {guild.id} 語音頻道內已無真實成員，自動開啟斷線倒數。")
                self.start_idle_timer(player)


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
