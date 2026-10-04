import asyncio
import sqlite3
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.moderation import Moderation, moderation_settings

GUILD_ID = next(iter(moderation_settings))
STAFF_ROLE_IDS = (
    moderation_settings[GUILD_ID]["moderator_role_id"],
    moderation_settings[GUILD_ID]["admin_role_id"],
)


class ModerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)

        async def run(query, params=()):
            self.db.execute(query, params)
            self.db.commit()

        async def get_one(query, params=()):
            return self.db.execute(query, params).fetchone()

        self.bot = SimpleNamespace(
            RUN=AsyncMock(side_effect=run), GET_ONE=AsyncMock(side_effect=get_one)
        )
        self.cog = Moderation(self.bot)
        await self.cog.cog_load()
        self.member = SimpleNamespace(
            id=100,
            mention="<@100>",
            top_role=1,
            timeout=AsyncMock(),
            send=AsyncMock(),
        )
        self.guild = SimpleNamespace(
            id=GUILD_ID,
            name="TheMoeWay",
            owner_id=999,
            ban=AsyncMock(),
            fetch_member=AsyncMock(return_value=self.member),
        )
        self.interaction = SimpleNamespace(
            guild_id=GUILD_ID,
            guild=self.guild,
            user=SimpleNamespace(
                id=200, top_role=2, roles=[SimpleNamespace(id=STAFF_ROLE_IDS[0])]
            ),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def warn(self, increment=1, message="Please follow the rules."):
        await self.cog.warn.callback(
            self.cog, self.interaction, self.member, message, increment
        )

    async def unwarn(self):
        await self.cog.unwarn.callback(self.cog, self.interaction, self.member)

    async def test_progressive_penalties_and_dm(self):
        await self.warn()
        self.member.timeout.assert_awaited_with(
            timedelta(minutes=10), reason=unittest.mock.ANY
        )
        self.member.send.assert_awaited_with(
            "You have been warned in TheMoeWay:\nPlease follow the rules.",
            allowed_mentions=unittest.mock.ANY,
        )
        await self.warn()
        self.member.timeout.assert_awaited_with(
            timedelta(weeks=1), reason=unittest.mock.ANY
        )
        await self.warn()
        self.guild.ban.assert_awaited_once_with(
            self.member, reason=unittest.mock.ANY, delete_message_seconds=0
        )
        self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 3)

    async def test_increment_can_skip_to_ban_and_dm_precedes_ban(self):
        calls = []

        async def dm(*args, **kwargs):
            calls.append("dm")

        async def ban(*args, **kwargs):
            calls.append("ban")

        self.member.send.side_effect = dm
        self.guild.ban.side_effect = ban
        await self.warn(4)
        self.assertEqual(calls, ["dm", "ban"])
        self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 4)
        self.member.timeout.assert_not_awaited()

    async def test_unwarn_clears_timeout_and_does_not_go_negative(self):
        await self.warn(2)
        await self.unwarn()
        self.member.timeout.assert_awaited_with(None, reason=unittest.mock.ANY)
        self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 1)
        await self.unwarn()
        await self.unwarn()
        self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 0)

    async def test_unwarn_can_target_absent_or_banned_user_without_unbanning(self):
        await self.warn(3)
        self.guild.fetch_member.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown Member"
        )
        await self.unwarn()
        self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 2)
        self.member.timeout.assert_not_awaited()

    async def test_unban_resets_any_warning_count_and_ignores_other_guilds(self):
        await self.warn(2)
        await self.cog.on_member_unban(SimpleNamespace(id=1), self.member)
        self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 2)
        await self.cog.on_member_unban(self.guild, self.member)
        self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 0)

    async def test_staff_role_required_in_configured_guild(self):
        for role_id in STAFF_ROLE_IDS:
            self.interaction.user.roles = [SimpleNamespace(id=role_id)]
            self.assertTrue(await self.cog.interaction_check(self.interaction))
        self.interaction.user.roles = []
        with self.assertRaises(discord.app_commands.MissingAnyRole):
            await self.cog.interaction_check(self.interaction)
        self.interaction.guild_id = 1
        self.assertFalse(await self.cog.interaction_check(self.interaction))
        self.interaction.guild_id = None
        self.assertFalse(await self.cog.interaction_check(self.interaction))

    async def test_discord_failure_leaves_warning_count_unchanged(self):
        failure = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"
        )
        self.member.timeout.side_effect = failure
        await self.warn()
        self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 0)
        self.member.send.assert_not_awaited()
        self.member.timeout.side_effect = None
        await self.warn()
        self.guild.ban.side_effect = failure
        await self.warn(2)
        self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 1)
        self.member.timeout.side_effect = failure
        await self.unwarn()
        self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 1)

    async def test_blocked_dm_does_not_prevent_penalty(self):
        self.member.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages"
        )
        await self.warn()
        self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 1)
        self.assertIn(
            "DM could not be delivered",
            self.interaction.followup.send.call_args.args[0],
        )

    async def test_parallel_warnings_do_not_lose_increments(self):
        await asyncio.gather(self.warn(), self.warn())
        self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 2)

    async def test_counter_survives_cog_reload(self):
        await self.warn()
        reloaded = Moderation(self.bot)
        await reloaded.cog_load()
        self.assertEqual(await reloaded.warning_count(GUILD_ID, self.member.id), 1)

    async def test_hierarchy_and_blank_message_rejections(self):
        await self.warn(message="  ")
        for member_id, top_role in [(200, 1), (999, 1), (100, 2), (100, 3)]:
            self.member.id = member_id
            self.member.top_role = top_role
            await self.warn()
        self.member.timeout.assert_not_awaited()
        self.guild.ban.assert_not_awaited()
        self.member.send.assert_not_awaited()

    async def test_commands_are_guild_scoped_and_increment_is_positive(self):
        for command in (self.cog.warn, self.cog.unwarn):
            self.assertEqual(command._guild_ids, list(moderation_settings))
            self.assertTrue(command.guild_only)
        increment = next(
            parameter
            for parameter in self.cog.warn.parameters
            if parameter.name == "increment"
        )
        self.assertEqual(increment.default, 1)
        self.assertEqual(increment.min_value, 1)

    async def test_staff_roles_are_specific_to_each_guild(self):
        second_guild_id = 123
        settings = {"moderator_role_id": 456, "admin_role_id": 789}
        with patch.dict(moderation_settings, {second_guild_id: settings}):
            self.interaction.guild_id = second_guild_id
            with self.assertRaises(discord.app_commands.MissingAnyRole):
                await self.cog.interaction_check(self.interaction)
            for role_id in settings.values():
                self.interaction.user.roles = [SimpleNamespace(id=role_id)]
                self.assertTrue(await self.cog.interaction_check(self.interaction))
                self.interaction.guild_id = GUILD_ID
                with self.assertRaises(discord.app_commands.MissingAnyRole):
                    await self.cog.interaction_check(self.interaction)
                self.interaction.guild_id = second_guild_id

    async def test_warnings_unwarnings_and_unbans_are_isolated_by_guild(self):
        second_guild_id = 123
        settings = {"moderator_role_id": 456, "admin_role_id": 789}
        with patch.dict(moderation_settings, {second_guild_id: settings}):
            await self.warn(2)
            self.guild.id = second_guild_id
            self.guild.name = "Second server"
            self.interaction.guild_id = second_guild_id
            await self.warn()
            self.member.timeout.assert_awaited_with(
                timedelta(minutes=10), reason=unittest.mock.ANY
            )
            self.member.send.assert_awaited_with(
                "You have been warned in Second server:\nPlease follow the rules.",
                allowed_mentions=unittest.mock.ANY,
            )
            self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 2)
            self.assertEqual(
                await self.cog.warning_count(second_guild_id, self.member.id), 1
            )
            await self.unwarn()
            self.assertEqual(
                await self.cog.warning_count(second_guild_id, self.member.id), 0
            )
            self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 2)
            await self.warn(3)
            await self.cog.on_member_unban(self.guild, self.member)
            self.assertEqual(
                await self.cog.warning_count(second_guild_id, self.member.id), 0
            )
            self.assertEqual(await self.cog.warning_count(GUILD_ID, self.member.id), 2)


if __name__ == "__main__":
    unittest.main()
