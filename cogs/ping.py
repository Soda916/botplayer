import discord
from discord.ext import commands
from discord import app_commands


class PingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="測試 Bot 延遲與連線狀態")
    async def ping(self, interaction: discord.Interaction):
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(
            f"🏓 Pong! 延遲時間：`{latency_ms} ms`",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PingCog(bot))
