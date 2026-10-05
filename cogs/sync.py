import os

import discord
from discord.ext import commands

from lib.bot import TMWBot

AUTHORIZED_USER_IDS = [int(id) for id in os.getenv("AUTHORIZED_USERS").split(",")]


def is_authorized():
    async def predicate(ctx: commands.Context):
        return ctx.author.id in AUTHORIZED_USER_IDS

    return commands.check(predicate)


class Sync(commands.Cog):
    def __init__(self, bot: TMWBot):
        self.bot = bot

    async def cog_load(self):
        pass

    @commands.command()
    @is_authorized()
    @commands.guild_only()
    async def sync_guild(self, ctx: discord.ext.commands.Context):
        """Sync server-specific commands without copying or clearing global commands."""
        await self.bot.tree.sync(guild=discord.Object(id=ctx.guild.id))
        await ctx.send(f"Synced commands to guild with id {ctx.guild.id}.")

    @commands.command()
    @is_authorized()
    async def sync_global(self, ctx: discord.ext.commands.Context):
        """Sync global commands for use in servers and supported DM contexts."""
        await self.bot.tree.sync()
        await ctx.send("Synced commands to global.")

    @commands.command()
    @is_authorized()
    async def clear_global_commands(self, ctx):
        """Clear all global commands."""
        await self._clear_remote_commands()
        await ctx.send("Cleared global commands.")

    @commands.command()
    @is_authorized()
    @commands.guild_only()
    async def clear_guild_commands(self, ctx):
        """Clear all guild commands."""
        await self._clear_remote_commands(guild=discord.Object(id=ctx.guild.id))
        await ctx.send(f"Cleared guild commands for guild with id {ctx.guild.id}.")

    async def _clear_remote_commands(self, *, guild=None):
        # Removing registrations must not discard handlers needed by the next sync.
        tree = self.bot.tree
        registered_commands = tree.get_commands(guild=guild)
        tree.clear_commands(guild=guild)
        try:
            await tree.sync(guild=guild)
        finally:
            for command in registered_commands:
                tree.add_command(command, guild=guild)


async def setup(bot):
    await bot.add_cog(Sync(bot))
