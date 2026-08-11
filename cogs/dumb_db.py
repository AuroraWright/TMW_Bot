import asyncio
import gzip
import os
import shutil
import tempfile

import discord
import yaml
from discord.ext import commands

from lib.bot import TMWBot

PATH_TO_DB = os.getenv("PATH_TO_DB", "data/db.sqlite3")
DUMB_DB_SETTINGS_PATH = (
    os.getenv("ALT_DUMB_DB_SETTINGS_PATH") or "config/dumb_db_settings.yml"
)

with open(DUMB_DB_SETTINGS_PATH, "r", encoding="utf-8") as settings_file:
    dumb_db_settings = yaml.safe_load(settings_file) or {}


def create_temporary_gzip_file():
    with tempfile.NamedTemporaryFile(
        prefix="tmw-db-", suffix=".sqlite3.gz", delete=False
    ) as temp_file:
        temp_file_path = temp_file.name
    try:
        with open(PATH_TO_DB, "rb") as f_in, gzip.open(temp_file_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        return temp_file_path
    except Exception:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        raise


def has_database_access(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False

    guild_settings = dumb_db_settings.get(interaction.guild.id) or dumb_db_settings.get(
        str(interaction.guild.id), {}
    )
    allowed_role_ids = {
        int(role_id) for role_id in guild_settings.get("allowed_role_ids", [])
    }
    member_roles = getattr(interaction.user, "roles", [])
    return any(role.id in allowed_role_ids for role in member_roles)


class DatabasePoster(commands.Cog):
    def __init__(self, bot: TMWBot):
        self.bot = bot

    @discord.app_commands.command(
        name="post_db",
        description="Send a compressed database backup.",
    )
    @discord.app_commands.guild_only()
    async def post_db(self, interaction: discord.Interaction):
        if not has_database_access(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        temp_file_path = None
        try:
            temp_file_path = await asyncio.to_thread(create_temporary_gzip_file)
            await interaction.followup.send(
                file=discord.File(temp_file_path, filename="db.sqlite3.gz"),
                ephemeral=True,
            )
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)


async def setup(bot: TMWBot):
    await bot.add_cog(DatabasePoster(bot))
