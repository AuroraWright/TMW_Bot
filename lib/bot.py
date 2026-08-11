import asyncio
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands

from lib.database import EncryptedDatabase

_log = logging.getLogger(__name__)


class TMWBot(commands.Bot):
    def __init__(
        self,
        command_prefix,
        database_encryption_key: bytes,
        cog_folder="cogs",
        path_to_db="data/db.sqlite3",
    ):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix=command_prefix, intents=intents)
        self.cog_folder = cog_folder
        self.path_to_db = path_to_db
        self.ready_file = os.getenv("READY_FILE")
        if self.ready_file:
            Path(self.ready_file).unlink(missing_ok=True)
        self._db_lock = asyncio.Lock()
        self._database = EncryptedDatabase(
            self.path_to_db,
            database_encryption_key,
        )

    async def on_ready(self):
        if self.ready_file:
            Path(self.ready_file).write_text("ready\n", encoding="ascii")
        _log.info("Bot is ready.")

    async def setup_hook(self):
        self.tree.on_error = self.on_application_command_error

    async def load_cogs(self, cogs_to_load):
        cogs = [
            cog
            for cog in os.listdir(self.cog_folder)
            if cog.endswith(".py") and (cogs_to_load == "*" or cog[:-3] in cogs_to_load)
        ]

        for cog in cogs:
            cog = f"{self.cog_folder}.{cog[:-3]}"
            await self.load_extension(cog)
            _log.info(f"Loaded {cog}")

    async def RUN(self, query: str, params: tuple = ()):
        async with self._db_lock:
            await asyncio.to_thread(self._database.run, query, params)

    async def RUN_AND_GET_ID(self, query: str, params: tuple = ()):
        """Execute a query and return the last inserted row ID."""
        async with self._db_lock:
            return await asyncio.to_thread(
                self._database.run_and_get_id,
                query,
                params,
            )

    async def RUN_MANY(self, query: str, rows: list[tuple]):
        async with self._db_lock:
            await asyncio.to_thread(self._database.run_many, query, rows)

    async def GET(self, query: str, params: tuple = ()):
        return await asyncio.to_thread(self._database.get, query, params)

    async def GET_ONE(self, query: str, params: tuple = ()):
        return await asyncio.to_thread(self._database.get_one, query, params)

    async def create_database_backup(self, destination: str):
        async with self._db_lock:
            await asyncio.to_thread(self._database.backup, destination)

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.MissingPermissions):
            _log.warning(
                f"User {ctx.author} tried to use a command without permission: {ctx.command}"
            )
            return

    async def on_application_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ):
        if isinstance(error, discord.app_commands.MissingAnyRole):
            await interaction.response.send_message(
                "You do not have the permission to use this command.", ephemeral=True
            )
            return
        elif isinstance(error, discord.app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"This command is currently on cooldown. You can use this command again after {int(error.retry_after)} seconds.",
                ephemeral=True,
            )
            return

        command = interaction.command
        if command is not None:
            if command._has_any_error_handlers():
                return

            _log.error("Exception in command %r", command.name, exc_info=error)
        else:
            _log.error("Exception in command tree", exc_info=error)

        error_embed = discord.Embed(
            title="Error",
            description=f"```{str(error)[:4000]}```",
            color=discord.Color.red(),
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(
                "An error occurred while processing your command:", embed=error_embed
            )
        else:
            await interaction.followup.send(
                "An error occurred while processing your command:", embed=error_embed
            )
