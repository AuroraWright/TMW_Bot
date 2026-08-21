import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import yaml

from cogs.auto_receive import AutoReceive

GUILD_ID = 1
MEMBER_ID = 100
SOURCE_ROLE_ID = 10
TARGET_ROLE_ID = 20
SECOND_TARGET_ROLE_ID = 30


def make_role(role_id, name=None, members=None):
    return SimpleNamespace(
        id=role_id,
        name=name or f"role-{role_id}",
        members=members or [],
    )


class AutoReceiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source_role = make_role(SOURCE_ROLE_ID, "Student")
        self.target_role = make_role(TARGET_ROLE_ID, "Sharing")
        self.second_target_role = make_role(SECOND_TARGET_ROLE_ID, "Custom Role")
        self.guild = Mock(id=GUILD_ID, name="Test Guild")
        self.guild.get_role.side_effect = {
            SOURCE_ROLE_ID: self.source_role,
            TARGET_ROLE_ID: self.target_role,
            SECOND_TARGET_ROLE_ID: self.second_target_role,
        }.get
        self.bot = Mock(guilds=[self.guild])
        self.cog = AutoReceive(self.bot)
        self.cog.auto_receive_config = {GUILD_ID: {SOURCE_ROLE_ID: (TARGET_ROLE_ID,)}}

    def make_member(self, roles):
        return SimpleNamespace(
            id=MEMBER_ID,
            name="quiz-taker",
            guild=self.guild,
            roles=roles,
            add_roles=AsyncMock(),
        )

    async def test_new_qualifying_role_immediately_grants_target_role(self):
        before = self.make_member([])
        after = self.make_member([self.source_role])

        await self.cog.on_member_update(before, after)

        after.add_roles.assert_awaited_once_with(
            self.target_role,
            reason="Configured automatic role assignment",
        )

    async def test_existing_target_role_is_not_added_again(self):
        before = self.make_member([self.target_role])
        after = self.make_member([self.source_role, self.target_role])

        await self.cog.on_member_update(before, after)

        after.add_roles.assert_not_awaited()

    async def test_member_update_without_role_change_does_nothing(self):
        before = self.make_member([self.source_role])
        after = self.make_member([self.source_role])

        await self.cog.on_member_update(before, after)

        after.add_roles.assert_not_awaited()

    async def test_one_source_role_can_grant_multiple_target_roles(self):
        self.cog.auto_receive_config = {
            GUILD_ID: {
                SOURCE_ROLE_ID: (TARGET_ROLE_ID, SECOND_TARGET_ROLE_ID),
            }
        }
        before = self.make_member([])
        after = self.make_member([self.source_role])

        await self.cog.on_member_update(before, after)

        self.assertEqual(
            after.add_roles.await_args_list,
            [
                call(
                    self.target_role,
                    reason="Configured automatic role assignment",
                ),
                call(
                    self.second_target_role,
                    reason="Configured automatic role assignment",
                ),
            ],
        )

    def test_scalar_and_list_targets_are_supported(self):
        normalized = self.cog._normalize_settings(
            {
                str(GUILD_ID): {
                    str(SOURCE_ROLE_ID): [
                        str(TARGET_ROLE_ID),
                        TARGET_ROLE_ID,
                        SECOND_TARGET_ROLE_ID,
                    ],
                    "11": "21",
                }
            }
        )

        self.assertEqual(
            normalized,
            {
                GUILD_ID: {
                    SOURCE_ROLE_ID: (TARGET_ROLE_ID, SECOND_TARGET_ROLE_ID),
                    11: (21,),
                }
            },
        )

    def test_every_main_guild_rank_grants_the_required_sub_server_role(self):
        auto_receive_settings = AutoReceive._normalize_settings(
            yaml.safe_load(Path("config/auto_receive.yml").read_text(encoding="utf-8"))
        )
        gatekeeper_settings = yaml.safe_load(
            Path("config/gatekeeper_settings.yml").read_text(encoding="utf-8")
        )
        sub_server_settings = yaml.safe_load(
            Path("config/sub_server_settings.yml").read_text(encoding="utf-8")
        )
        main_guild_id = sub_server_settings["main_guild_id"]
        required_role_id = sub_server_settings["required_role_id"]

        rank_role_ids = {
            rank["rank_to_get"]
            for rank in gatekeeper_settings["rank_structure"][main_guild_id]
            if rank["rank_to_get"] is not None
        }

        for rank_role_id in rank_role_ids:
            with self.subTest(rank_role_id=rank_role_id):
                self.assertIn(
                    required_role_id,
                    auto_receive_settings[main_guild_id].get(rank_role_id, ()),
                )


if __name__ == "__main__":
    unittest.main()
