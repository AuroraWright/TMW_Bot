import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cogs.sub_server_access import (
    AccessStatus,
    SubServerAccess,
    SubServerSettings,
)

MAIN_GUILD_ID = 1
SUB_GUILD_ID = 2
USER_ID = 100


def make_role(role_id, position):
    return SimpleNamespace(id=role_id, position=position)


class SubServerAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.main_guild = Mock(id=MAIN_GUILD_ID)
        self.sub_guild = Mock(id=SUB_GUILD_ID, members=[])
        self.sub_guild.get_member.return_value = None
        self.sub_guild.kick = AsyncMock()
        self.sub_guild.ban = AsyncMock()
        self.sub_guild.unban = AsyncMock()

        self.bot = Mock()
        self.bot.user = SimpleNamespace(id=999)
        self.bot.RUN = AsyncMock()
        self.bot.GET = AsyncMock(return_value=[])
        self.bot.GET_ONE = AsyncMock(return_value=(1,))
        self.bot.get_guild.side_effect = {
            MAIN_GUILD_ID: self.main_guild,
            SUB_GUILD_ID: self.sub_guild,
        }.get
        self.cog = SubServerAccess(
            self.bot,
            SubServerSettings(
                main_guild_id=MAIN_GUILD_ID,
                sub_guild_ids=(SUB_GUILD_ID,),
            ),
        )

    def make_main_member(self, roles):
        return SimpleNamespace(id=USER_ID, guild=self.main_guild, roles=roles)

    def make_sub_member(self):
        return SimpleNamespace(id=USER_ID, guild=self.sub_guild)

    async def test_cog_load_creates_mirrored_ban_tracking_table(self):
        await self.cog.cog_load()

        create_query = self.bot.RUN.await_args.args[0]
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS sub_server_mirrored_bans", create_query
        )

    async def test_main_guild_member_is_eligible_without_a_required_role(self):
        member = self.make_main_member([make_role(1, 0)])
        self.main_guild.get_member.return_value = member

        self.assertEqual(
            await self.cog._get_access_status(USER_ID),
            AccessStatus.ELIGIBLE,
        )

    async def test_sub_server_can_require_a_main_guild_role(self):
        required_role = make_role(10, 1)
        self.cog.settings = SubServerSettings(
            main_guild_id=MAIN_GUILD_ID,
            sub_guild_ids=(SUB_GUILD_ID,),
            required_role_ids_by_sub_guild={SUB_GUILD_ID: (required_role.id,)},
        )
        self.main_guild.get_member.return_value = self.make_main_member([])

        self.assertEqual(
            await self.cog._get_access_status(USER_ID, SUB_GUILD_ID),
            AccessStatus.MISSING_REQUIRED_ROLE,
        )

        self.main_guild.get_member.return_value = self.make_main_member([required_role])
        self.assertEqual(
            await self.cog._get_access_status(USER_ID, SUB_GUILD_ID),
            AccessStatus.ELIGIBLE,
        )

    async def test_missing_required_role_is_kicked(self):
        self.cog.settings = SubServerSettings(
            main_guild_id=MAIN_GUILD_ID,
            sub_guild_ids=(SUB_GUILD_ID,),
            required_role_ids_by_sub_guild={SUB_GUILD_ID: (10,)},
        )
        self.cog._get_access_status = AsyncMock(
            return_value=AccessStatus.MISSING_REQUIRED_ROLE
        )

        await self.cog.on_member_join(self.make_sub_member())

        self.sub_guild.kick.assert_awaited_once()

    async def test_exemption_role_bypasses_missing_required_role_kick(self):
        self.cog.settings = SubServerSettings(
            main_guild_id=MAIN_GUILD_ID,
            sub_guild_ids=(SUB_GUILD_ID,),
            required_role_ids_by_sub_guild={SUB_GUILD_ID: (10,)},
            exempt_role_ids_by_sub_guild={SUB_GUILD_ID: (20,)},
        )
        self.cog._get_access_status = AsyncMock(
            return_value=AccessStatus.MISSING_REQUIRED_ROLE
        )
        member = SimpleNamespace(
            id=USER_ID,
            guild=self.sub_guild,
            roles=[make_role(20, 1)],
        )

        await self.cog._enforce_sub_member(member)

        self.sub_guild.kick.assert_not_awaited()

    async def test_exemption_role_bypasses_not_in_main_kick(self):
        self.cog.settings = SubServerSettings(
            main_guild_id=MAIN_GUILD_ID,
            sub_guild_ids=(SUB_GUILD_ID,),
            exempt_role_ids_by_sub_guild={SUB_GUILD_ID: (20,)},
        )
        self.cog._get_access_status = AsyncMock(
            return_value=AccessStatus.NOT_IN_MAIN_GUILD
        )
        member = SimpleNamespace(
            id=USER_ID,
            guild=self.sub_guild,
            roles=[make_role(20, 1)],
        )

        await self.cog._enforce_sub_member(member)

        self.sub_guild.kick.assert_not_awaited()

    async def test_exemption_user_bypasses_missing_required_role_kick(self):
        self.cog.settings = SubServerSettings(
            main_guild_id=MAIN_GUILD_ID,
            sub_guild_ids=(SUB_GUILD_ID,),
            required_role_ids_by_sub_guild={SUB_GUILD_ID: (10,)},
            exempt_user_ids_by_sub_guild={SUB_GUILD_ID: (USER_ID,)},
        )
        self.cog._get_access_status = AsyncMock(
            return_value=AccessStatus.MISSING_REQUIRED_ROLE
        )

        await self.cog._enforce_sub_member(self.make_sub_member())

        self.sub_guild.kick.assert_not_awaited()

    async def test_sub_server_join_without_main_membership_is_kicked(self):
        self.cog._get_access_status = AsyncMock(
            return_value=AccessStatus.NOT_IN_MAIN_GUILD
        )

        await self.cog.on_member_join(self.make_sub_member())

        kicked_user = self.sub_guild.kick.await_args.args[0]
        self.assertEqual(kicked_user.id, USER_ID)
        self.sub_guild.ban.assert_not_awaited()

    async def test_banned_main_server_user_is_banned_from_sub_server(self):
        self.cog._get_access_status = AsyncMock(
            return_value=AccessStatus.BANNED_FROM_MAIN_GUILD
        )

        await self.cog.on_member_join(self.make_sub_member())

        banned_user = self.sub_guild.ban.await_args.args[0]
        self.assertEqual(banned_user.id, USER_ID)
        self.sub_guild.kick.assert_not_awaited()

    async def test_leaving_main_server_kicks_user_from_every_sub_server(self):
        member = self.make_main_member([make_role(1, 0)])

        await self.cog.on_member_remove(member)

        kicked_user = self.sub_guild.kick.await_args.args[0]
        self.assertEqual(kicked_user.id, USER_ID)

    async def test_leaving_main_server_preserves_exempt_member(self):
        self.cog.settings = SubServerSettings(
            main_guild_id=MAIN_GUILD_ID,
            sub_guild_ids=(SUB_GUILD_ID,),
            exempt_role_ids_by_sub_guild={SUB_GUILD_ID: (20,)},
        )
        self.sub_guild.get_member.return_value = SimpleNamespace(
            id=USER_ID,
            guild=self.sub_guild,
            roles=[make_role(20, 1)],
        )

        await self.cog.on_member_remove(self.make_main_member([]))

        self.sub_guild.kick.assert_not_awaited()

    async def test_leaving_main_server_preserves_exempt_user_without_member_cache(self):
        self.cog.settings = SubServerSettings(
            main_guild_id=MAIN_GUILD_ID,
            sub_guild_ids=(SUB_GUILD_ID,),
            exempt_user_ids_by_sub_guild={SUB_GUILD_ID: (USER_ID,)},
        )

        await self.cog.on_member_remove(self.make_main_member([]))

        self.sub_guild.kick.assert_not_awaited()

    async def test_main_server_ban_is_propagated(self):
        user = SimpleNamespace(id=USER_ID)

        await self.cog.on_member_ban(self.main_guild, user)

        banned_user = self.sub_guild.ban.await_args.args[0]
        self.assertEqual(banned_user.id, USER_ID)

    async def test_main_server_unban_removes_mirrored_sub_server_ban(self):
        user = SimpleNamespace(id=USER_ID)

        await self.cog.on_member_unban(self.main_guild, user)

        unbanned_user = self.sub_guild.unban.await_args.args[0]
        self.assertEqual(unbanned_user.id, USER_ID)
        delete_query = self.bot.RUN.await_args.args[0]
        self.assertIn("DELETE FROM sub_server_mirrored_bans", delete_query)

    async def test_main_server_unban_preserves_untracked_local_sub_server_ban(self):
        self.bot.GET_ONE.return_value = None
        user = SimpleNamespace(id=USER_ID)

        await self.cog.on_member_unban(self.main_guild, user)

        self.sub_guild.unban.assert_not_awaited()

    async def test_existing_main_server_bans_are_synchronized(self):
        async def main_bans(limit):
            self.assertIsNone(limit)
            yield SimpleNamespace(user=SimpleNamespace(id=USER_ID))

        async def sub_server_bans(limit):
            self.assertIsNone(limit)
            if False:
                yield

        self.main_guild.bans = main_bans
        self.sub_guild.bans = sub_server_bans

        await self.cog.synchronize_main_guild_bans()

        banned_user = self.sub_guild.ban.await_args.args[0]
        self.assertEqual(banned_user.id, USER_ID)

    async def test_existing_main_ban_already_in_sub_server_is_tracked(self):
        async def bans(limit):
            self.assertIsNone(limit)
            yield SimpleNamespace(user=SimpleNamespace(id=USER_ID))

        self.main_guild.bans = bans
        self.sub_guild.bans = bans

        await self.cog.synchronize_main_guild_bans()

        self.sub_guild.ban.assert_not_awaited()
        record_query = self.bot.RUN.await_args.args[0]
        self.assertIn("INSERT INTO sub_server_mirrored_bans", record_query)

    async def test_offline_main_unban_is_reconciled_on_startup(self):
        async def main_bans(limit):
            self.assertIsNone(limit)
            if False:
                yield

        async def sub_server_bans(limit):
            self.assertIsNone(limit)
            yield SimpleNamespace(user=SimpleNamespace(id=USER_ID))

        self.main_guild.bans = main_bans
        self.sub_guild.bans = sub_server_bans
        self.bot.GET.return_value = [(SUB_GUILD_ID, USER_ID)]

        await self.cog.synchronize_main_guild_bans()

        unbanned_user = self.sub_guild.unban.await_args.args[0]
        self.assertEqual(unbanned_user.id, USER_ID)
        delete_query = self.bot.RUN.await_args.args[0]
        self.assertIn("DELETE FROM sub_server_mirrored_bans", delete_query)

    async def test_startup_preserves_untracked_local_sub_server_ban(self):
        async def main_bans(limit):
            self.assertIsNone(limit)
            if False:
                yield

        async def sub_server_bans(limit):
            self.assertIsNone(limit)
            yield SimpleNamespace(user=SimpleNamespace(id=USER_ID))

        self.main_guild.bans = main_bans
        self.sub_guild.bans = sub_server_bans

        await self.cog.synchronize_main_guild_bans()

        self.sub_guild.unban.assert_not_awaited()

    async def test_cannot_verify_does_not_mass_kick(self):
        self.cog._get_access_status = AsyncMock(return_value=AccessStatus.CANNOT_VERIFY)

        await self.cog.on_member_join(self.make_sub_member())

        self.sub_guild.kick.assert_not_awaited()
        self.sub_guild.ban.assert_not_awaited()


class SubServerSettingsTests(unittest.TestCase):
    def test_per_sub_server_requirements_and_exemptions_are_configurable(self):
        settings = SubServerSettings.from_mapping(
            {
                "main_guild_id": 1,
                "sub_guild_ids": [2, 3],
                "required_role_ids": {"3": [10]},
                "exempt_role_ids": {3: 20},
                "exempt_user_ids": {3: [30, 31]},
                "mirrored_sub_guild_ids": [2],
            }
        )

        self.assertEqual(settings.required_role_ids_for(3), (10,))
        self.assertEqual(settings.exempt_role_ids_for(3), (20,))
        self.assertEqual(settings.exempt_user_ids_for(3), (30, 31))
        self.assertEqual(settings.mirror_guild_ids, (2,))

    def test_duplicate_sub_server_ids_are_removed(self):
        settings = SubServerSettings.from_mapping(
            {
                "main_guild_id": "1",
                "sub_guild_ids": ["2", 2, 3],
            }
        )

        self.assertEqual(settings.sub_guild_ids, (2, 3))

    def test_main_server_cannot_be_a_sub_server(self):
        with self.assertRaises(ValueError):
            SubServerSettings.from_mapping(
                {
                    "main_guild_id": 1,
                    "sub_guild_ids": [1],
                }
            )


if __name__ == "__main__":
    unittest.main()
