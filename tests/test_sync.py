import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord import app_commands

os.environ.setdefault("AUTHORIZED_USERS", "1")

from cogs.sync import Sync


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = discord.Client(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self.client)
        self.guild = discord.Object(id=123)

        @app_commands.command(name="info")
        async def info(interaction: discord.Interaction):
            pass

        @app_commands.command(name="warn")
        @app_commands.guild_only()
        async def warn(interaction: discord.Interaction):
            pass

        self.tree.add_command(info)
        self.tree.add_command(warn, guild=self.guild)
        self.snapshots = []

        async def sync(*, guild=None):
            self.snapshots.append(
                (
                    guild.id if guild else None,
                    [command.name for command in self.tree.get_commands(guild=guild)],
                )
            )
            return []

        self.tree.sync = AsyncMock(side_effect=sync)
        self.cog = Sync(SimpleNamespace(tree=self.tree))
        self.ctx = SimpleNamespace(guild=self.guild, send=AsyncMock())

    async def asyncTearDown(self):
        await self.client.close()

    async def test_guild_sync_does_not_copy_or_remove_global_commands(self):
        await self.cog.sync_guild.callback(self.cog, self.ctx)
        self.assertEqual(self.snapshots, [(123, ["warn"])])
        self.assertIsNotNone(self.tree.get_command("info"))
        self.assertIsNone(self.tree.get_command("info", guild=self.guild))
        await self.cog.sync_global.callback(self.cog, self.ctx)
        self.assertEqual(self.snapshots[-1], (None, ["info"]))

    async def test_global_sync_then_guild_sync_keeps_dm_handlers(self):
        await self.cog.sync_global.callback(self.cog, self.ctx)
        await self.cog.sync_guild.callback(self.cog, self.ctx)
        self.assertEqual(self.snapshots, [(None, ["info"]), (123, ["warn"])])
        payload = self.tree.get_command("info").to_dict(self.tree)
        self.assertTrue(payload["dm_permission"])

    async def test_clear_global_restores_handlers_for_next_sync(self):
        await self.cog.clear_global_commands.callback(self.cog, self.ctx)
        self.assertEqual(self.snapshots, [(None, [])])
        self.assertIsNotNone(self.tree.get_command("info"))
        self.assertIsNotNone(self.tree.get_command("warn", guild=self.guild))
        await self.cog.sync_global.callback(self.cog, self.ctx)
        self.assertEqual(self.snapshots[-1], (None, ["info"]))

    async def test_clear_guild_restores_handlers_for_next_sync(self):
        await self.cog.clear_guild_commands.callback(self.cog, self.ctx)
        self.assertEqual(self.snapshots, [(123, [])])
        self.assertIsNotNone(self.tree.get_command("info"))
        await self.cog.sync_guild.callback(self.cog, self.ctx)
        self.assertEqual(self.snapshots[-1], (123, ["warn"]))

    async def test_failed_clear_restores_handlers(self):
        self.tree.sync.side_effect = RuntimeError("Sync failed")
        for command in (self.cog.clear_global_commands, self.cog.clear_guild_commands):
            with self.assertRaises(RuntimeError):
                await command.callback(self.cog, self.ctx)
            self.assertIsNotNone(self.tree.get_command("info"))
            self.assertIsNotNone(self.tree.get_command("warn", guild=self.guild))


if __name__ == "__main__":
    unittest.main()
