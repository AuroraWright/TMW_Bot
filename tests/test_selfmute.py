import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from cogs.selfmute import REMOVE_MUTE_QUERY, Selfmute


class CheckMuteDMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.member = SimpleNamespace(id=100)
        self.guild = SimpleNamespace(
            id=123,
            name="Server",
            get_member=lambda user_id: self.member,
            get_channel=lambda channel_id: None,
            fetch_member=AsyncMock(return_value=self.member),
        )
        self.bot = SimpleNamespace(
            GET=AsyncMock(return_value=[]),
            RUN=AsyncMock(),
            get_guild=lambda guild_id: self.guild,
        )
        self.cog = Selfmute(self.bot)
        self.cog.perform_user_unmute = AsyncMock()
        self.interaction = SimpleNamespace(
            guild=None,
            user=SimpleNamespace(id=100),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def mute(self, guild_id=123, *, expired=True):
        when = discord.utils.utcnow() + timedelta(days=-1 if expired else 1)
        return (guild_id, 100, 50, "10,20", when.strftime("%Y-%m-%d %H:%M:%S"))

    async def check_mute(self):
        await self.cog.check_mute.callback(self.cog, self.interaction)

    async def test_no_mutes_replies_in_dm(self):
        await self.check_mute()
        self.interaction.followup.send.assert_awaited_once_with(
            "You are not muted.", ephemeral=True
        )

    async def test_expired_dm_mute_uses_guild_member_and_individual_record(self):
        records = [self.mute(), self.mute(guild_id=456)]
        self.bot.GET.return_value = records
        await self.check_mute()
        self.assertEqual(self.cog.perform_user_unmute.await_count, 2)
        self.cog.perform_user_unmute.assert_any_await(self.member, None, records[0])
        self.cog.perform_user_unmute.assert_any_await(self.member, None, records[1])

    async def test_active_mute_reports_remaining_time(self):
        self.bot.GET.return_value = [self.mute(expired=False)]
        await self.check_mute()
        self.cog.perform_user_unmute.assert_not_awaited()
        self.assertIn("Server", self.interaction.followup.send.call_args.args[0])

    async def test_member_is_fetched_when_not_cached(self):
        record = self.mute()
        self.bot.GET.return_value = [record]
        self.guild.get_member = lambda user_id: None
        await self.check_mute()
        self.guild.fetch_member.assert_awaited_once_with(100)
        self.cog.perform_user_unmute.assert_awaited_once_with(self.member, None, record)

    async def test_departed_user_expired_record_is_removed(self):
        self.bot.GET.return_value = [self.mute()]
        self.guild.get_member = lambda user_id: None
        self.guild.fetch_member.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown Member"
        )
        await self.check_mute()
        self.bot.RUN.assert_awaited_once_with(REMOVE_MUTE_QUERY, (123, 100))
        self.cog.perform_user_unmute.assert_not_awaited()

    async def test_missing_guild_replies_without_dropping_mute(self):
        self.bot.GET.return_value = [self.mute()]
        self.bot.get_guild = lambda guild_id: None
        await self.check_mute()
        self.interaction.followup.send.assert_awaited_once()
        self.bot.RUN.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
