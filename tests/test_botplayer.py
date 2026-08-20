import unittest
import sys
import os

# 將專案根目錄納入 Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cogs.music import Track, GuildPlayer, YTDL_OPTIONS, FFMPEG_OPTIONS


class TestBotPlayer(unittest.TestCase):
    def test_ytdl_and_ffmpeg_options(self):
        """驗證 e2-micro 極致省資源設定條款"""
        self.assertTrue(YTDL_OPTIONS.get("noplaylist"), "必須設定 noplaylist=True 以封印播放清單")
        self.assertEqual(YTDL_OPTIONS.get("format"), "bestaudio/best", "必須僅請求音訊格式")
        self.assertIn("-vn", FFMPEG_OPTIONS.get("options", ""), "FFmpeg 必須包含 -vn 參數絕不安裝畫面")

    def test_track_formatting(self):
        """驗證 Track 格式化長度邏輯"""
        fake_user = None
        t1 = Track(title="Test Song", url="http://yt.com", stream_url="http://stream.com", duration=125, requester=fake_user)
        self.assertEqual(t1.format_duration(), "02:05")

        t2 = Track(title="Long Song", url="http://yt.com", stream_url="http://stream.com", duration=3665, requester=fake_user)
        self.assertEqual(t2.format_duration(), "01:01:05")

        t3 = Track(title="Live Stream", url="http://yt.com", stream_url="http://stream.com", duration=0, requester=fake_user)
        self.assertEqual(t3.format_duration(), "直播 / 未知")

    def test_guild_player_initialization(self):
        """驗證 GuildPlayer 初始化佇列與歷史"""
        player = GuildPlayer(guild_id=123456789)
        self.assertEqual(player.guild_id, 123456789)
        self.assertEqual(len(player.queue), 0)
        self.assertEqual(len(player.history), 0)
        self.assertIsNone(player.current_track)
        self.assertEqual(player.idle_timeout_minutes, 5, "預設閒置退出時間應為 5 分鐘")

    def test_idle_timeout_settings(self):
        """驗證閒置退出時間設定"""
        player = GuildPlayer(guild_id=123456789)
        player.idle_timeout_minutes = 15
        self.assertEqual(player.idle_timeout_minutes, 15)

        # 設為 0 代表永不退出
        player.idle_timeout_minutes = 0
        self.assertEqual(player.idle_timeout_minutes, 0)



if __name__ == "__main__":
    unittest.main()
