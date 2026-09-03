import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.rank_saver import RankSaver
from cogs.sub_server_access import sub_server_settings


class RankSaverAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_sub_server_roles_are_never_saved(self):
        authoritative_role = SimpleNamespace(id=10, is_assignable=lambda: True)
        authoritative_member = SimpleNamespace(
            id=100, bot=False, roles=[authoritative_role]
        )
        authoritative_guild = SimpleNamespace(
            id=sub_server_settings.main_guild_id,
            members=[authoritative_member],
        )
        sub_member = SimpleNamespace(id=100, bot=False, roles=[])
        sub_guild = SimpleNamespace(
            id=sub_server_settings.sub_guild_ids[0],
            members=[sub_member],
        )
        bot = SimpleNamespace(
            guilds=[authoritative_guild, sub_guild],
            RUN_MANY=AsyncMock(),
        )
        cog = RankSaver(bot)

        await cog.rank_saver.coro(cog)

        rows = bot.RUN_MANY.await_args.args[1]
        self.assertEqual(
            rows, [(authoritative_guild.id, authoritative_member.id, "10")]
        )

    async def test_sub_server_join_does_not_use_local_restore_data(self):
        member = SimpleNamespace(
            id=100,
            guild=SimpleNamespace(id=sub_server_settings.sub_guild_ids[0]),
        )
        bot = SimpleNamespace(GET=AsyncMock())
        cog = RankSaver(bot)

        await cog.rank_restorer(member)

        bot.GET.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
