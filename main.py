import os
import sys
import logging
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

# 載入 .env
load_dotenv()

# 設定 Logging
log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("botplayer")

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    logger.critical("未找到 DISCORD_TOKEN 環境變數，請確認 .env 檔案設定！")
    sys.exit(1)


class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True  # 音訊狀態變更監聽 (閒置離線與自動處理用)
        intents.message_content = False  # 採用 Slash Command，毋需開 Message Content Intent

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        # 動態載入 cogs/ 目錄下所有模組
        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        if os.path.exists(cogs_dir):
            for filename in sorted(os.listdir(cogs_dir)):
                if filename.endswith(".py") and not filename.startswith("_"):
                    cog_name = f"cogs.{filename[:-3]}"
                    try:
                        await self.load_extension(cog_name)
                        logger.info(f"成功載入 Cog: {cog_name}")
                    except Exception as e:
                        logger.error(f"載入 Cog {cog_name} 失敗: {e}", exc_info=True)

        # 同步 Slash Commands
        logger.info("正在同步 Slash Commands 至 Discord...")
        try:
            synced = await self.tree.sync()
            logger.info(f"Slash Commands 同步成功，共 {len(synced)} 個指令。")
        except Exception as e:
            logger.error(f"Slash Commands 同步失敗: {e}", exc_info=True)

    async def on_ready(self):
        logger.info(f"Bot 成功上線！名稱: {self.user} (ID: {self.user.id})")
        logger.info(f"目前加入伺服器數量: {len(self.guilds)}")


def main():
    bot = MusicBot()
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        logger.info("Bot 已關閉。")


if __name__ == "__main__":
    main()
