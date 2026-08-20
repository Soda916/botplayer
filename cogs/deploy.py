"""Hot deploy 指令：在私訊執行 git pull、語法檢查並深層重載安全的 cogs。"""

import asyncio
import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Iterable, List

import discord
from discord import app_commands
from discord.ext import commands


LOGGER = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent
COGS_DIR = BASE_DIR / "cogs"


class Deploy(commands.Cog):
    """提供 /hotdeploy 指令，讓 bot owner 遠端更新與重載。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.owner_id = int(os.getenv("OWNER_ID", "0"))
        self.deploy_hash = os.getenv("DEPLOY_HASH", "")
        self._lock = asyncio.Lock()

    def _is_owner(self, user_id: int) -> bool:
        return self.owner_id > 0 and user_id == self.owner_id

    def _is_hot_reload_safe(self, path: Path) -> bool:
        # 避免含有 persistent view 的 cog 熱重載，防止按鈕失效。
        if any(part.startswith("_") for part in path.parts):
            return False
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            return False
        return "add_view(" not in source

    async def _run_command(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

    async def _run_git(self, *args: str) -> tuple[int, str, str]:
        return await self._run_command("git", *args)

    async def _run_python_compileall(self) -> tuple[int, str, str]:
        return await self._run_command(sys.executable, "-m", "compileall", "-q", ".")

    async def _reset_to_commit(self, commit: str) -> tuple[int, str, str]:
        return await self._run_git("reset", "--hard", commit)

    async def _reload_extensions(self, extensions: Iterable[str]) -> List[str]:
        # 先刷新 sys.modules 中所有 cogs.* 相依模組
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith("cogs.") and mod_name not in self.bot.extensions:
                try:
                    importlib.reload(sys.modules[mod_name])
                except Exception:
                    LOGGER.warning("無法重載相依模組 %s", mod_name, exc_info=True)

        reloaded: List[str] = []
        for ext in extensions:
            if ext in self.bot.extensions:
                await self.bot.reload_extension(ext)
            else:
                await self.bot.load_extension(ext)
            reloaded.append(ext)
        return reloaded

    @app_commands.command(name="hotdeploy", description="git pull 並重新載入安全的 cogs")
    @app_commands.rename(hash_value="雜湊")
    @app_commands.describe(hash_value="雜湊")
    async def deploy(self, interaction: discord.Interaction, hash_value: str) -> None:
        if interaction.user is None or not self._is_owner(interaction.user.id):
            await interaction.response.send_message("只有 bot owner 可以使用這個指令。", ephemeral=True)
            return

        if interaction.guild is not None:
            await interaction.response.send_message("這個指令只允許在私訊中使用。", ephemeral=True)
            return

        if not self.deploy_hash:
            await interaction.response.send_message("`DEPLOY_HASH` 尚未設定，無法執行部署。", ephemeral=True)
            return

        if hash_value != self.deploy_hash:
            await interaction.response.send_message("雜湊驗證失敗。", ephemeral=True)
            return

        async with self._lock:
            await interaction.response.send_message(
                "開始更新：執行 `git pull --ff-only` 並重新載入安全的 cogs。",
                ephemeral=True,
            )

            old_code, old_rev, old_err = await self._run_git("rev-parse", "HEAD")
            if old_code != 0:
                await interaction.followup.send(f"`git rev-parse HEAD` 失敗：```text\n{old_err or old_rev}\n```", ephemeral=True)
                return

            pull_code, pull_out, pull_err = await self._run_git("pull", "--ff-only")
            if pull_code != 0:
                await interaction.followup.send(f"`git pull --ff-only` 失敗：```text\n{pull_err or pull_out}\n```", ephemeral=True)
                return

            new_code, new_rev, new_err = await self._run_git("rev-parse", "HEAD")
            if new_code != 0:
                await interaction.followup.send(f"更新後無法取得版本：```text\n{new_err or new_rev}\n```", ephemeral=True)
                return

            # 編譯語法預檢
            compile_code, compile_out, compile_err = await self._run_python_compileall()
            if compile_code != 0:
                await self._reset_to_commit(old_rev)
                compile_output = compile_err or compile_out
                await interaction.followup.send(
                    "❌ 這次版本在語法檢查時失敗，已自動回滾：\n"
                    f"```text\n{compile_output[:1500]}\n```",
                    ephemeral=True,
                )
                return

            diff_code, diff_out, diff_err = await self._run_git("diff", "--name-only", old_rev, new_rev)
            if diff_code != 0:
                await interaction.followup.send(f"無法取得更新檔案清單：```text\n{diff_err or diff_out}\n```", ephemeral=True)
                return

            changed_files = [line for line in diff_out.splitlines() if line.strip()]
            reloadable_exts: List[str] = []
            blocked_cogs: List[str] = []

            for path_str in changed_files:
                p = Path(path_str)
                if len(p.parts) > 1 and p.parts[0] == "cogs" and p.suffix == ".py":
                    if any(part.startswith("_") for part in p.parts):
                        blocked_cogs.append(path_str)
                        continue

                    full_path = BASE_DIR / p
                    if self._is_hot_reload_safe(full_path):
                        ext_name = ".".join(p.with_suffix("").parts)
                        reloadable_exts.append(ext_name)
                    else:
                        blocked_cogs.append(path_str)

            needs_restart = any(
                path == "main.py"
                or path == "requirements.txt"
                or path == ".env"
                or (path.endswith(".py") and not path.startswith("cogs/"))
                for path in changed_files
            ) or bool(blocked_cogs)

            try:
                reloaded: List[str] = []
                synced = []
                if reloadable_exts:
                    reloaded = await self._reload_extensions(reloadable_exts)
                    synced = await self.bot.tree.sync()
            except Exception:
                LOGGER.exception("Hot deploy failed during extension reload")
                await interaction.followup.send("cog 熱更新失敗，程式仍在執行，但這次部署需要手動檢查 log。", ephemeral=True)
                return

            lines = [
                f"更新完成：`{old_rev[:7]}` -> `{new_rev[:7]}`",
            ]

            if reloaded:
                lines.append(f"重新載入 {len(reloaded)} 個 cogs，重新同步 {len(synced)} 個 app commands。")
            else:
                lines.append("這次沒有執行任何 cog 熱重載。")

            if changed_files:
                preview = "\n".join(changed_files[:20])
                if len(changed_files) > 20:
                    preview += "\n..."
                lines.append(f"變更檔案：```text\n{preview}\n```")
            else:
                lines.append("沒有偵測到檔案差異。")

            if blocked_cogs:
                lines.append(
                    "以下 cog 含有 persistent view 或非安全載入邏輯，這次不做熱重載："
                    f" `{', '.join(blocked_cogs)}`"
                )

            if pull_out:
                lines.append(f"`git pull` 輸出：```text\n{pull_out[:1500]}\n```")

            if needs_restart:
                lines.append("這次變更包含核心檔案或不安全 hot reload 的 cog，建議仍手動重啟一次。")
            else:
                lines.append("這次變更只落在 cog 範圍內，可直接繼續運行，不需要重啟整個 bot process。")

            await interaction.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Deploy(bot))
