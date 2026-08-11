import asyncio
import gzip
import os
import shutil
import tempfile

import discord
import yaml
from discord.ext import commands

from lib.bot import TMWBot

DUMB_DB_SETTINGS_PATH = (
    os.getenv("ALT_DUMB_DB_SETTINGS_PATH") or "config/dumb_db_settings.yml"
)

with open(DUMB_DB_SETTINGS_PATH, "r", encoding="utf-8") as settings_file:
    dumb_db_settings = yaml.safe_load(settings_file) or {}


def gzip_database(source_path: str, destination_path: str):
    with open(source_path, "rb") as f_in, gzip.open(destination_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


async def create_temporary_gzip_file(bot: TMWBot):
    with tempfile.NamedTemporaryFile(
        prefix="tmw-db-", suffix=".sqlcipher.sqlite3", delete=False
    ) as database_file:
        database_path = database_file.name
    with tempfile.NamedTemporaryFile(
        prefix="tmw-db-", suffix=".sqlcipher.sqlite3.gz", delete=False
    ) as archive_file:
        archive_path = archive_file.name

    try:
        await bot.create_database_backup(database_path)
        await asyncio.to_thread(gzip_database, database_path, archive_path)
        return archive_path
    except Exception:
        if os.path.exists(archive_path):
            os.remove(archive_path)
        raise
    finally:
        if os.path.exists(database_path):
            os.remove(database_path)


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
            temp_file_path = await create_temporary_gzip_file(self.bot)
            await interaction.followup.send(
                file=discord.File(
                    temp_file_path,
                    filename="db.sqlcipher.sqlite3.gz",
                ),
                ephemeral=True,
            )
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)


async def setup(bot: TMWBot):
    await bot.add_cog(DatabasePoster(bot))
