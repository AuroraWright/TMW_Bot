import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import discord

from cogs import gatekeeper
from cogs.gatekeeper import (
    COPY_RENAMED_PASSED_QUIZZES,
    CREATE_PASSED_QUIZZES_TABLE,
    DELETE_RENAMED_PASSED_QUIZZES,
    GET_PASSED_QUIZZES,
    RENAME_QUIZ_ATTEMPTS,
    UPSERT_QUIZ_MENU_MESSAGE,
    DynamicQuizMenu,
    LevelUp,
    assign_quiz_autocomplete,
    build_ranktable_description,
    find_rank_by_name,
    gatekeeper_settings,
)
from lib.bot import TMWBot


def make_role(role_id, member_ids):
    return SimpleNamespace(
        id=role_id,
        mention=f"<@&{role_id}>",
        members=[SimpleNamespace(id=member_id) for member_id in member_ids],
    )


class GatekeeperNamingTests(unittest.IsolatedAsyncioTestCase):
    def test_production_rank_rename_and_bonus_boundary_are_configured(self):
        matching_guilds = [
            rank_structure
            for rank_structure in gatekeeper_settings["rank_structure"].values()
            if find_rank_by_name(rank_structure, "Mythic Idol") is not None
        ]
        self.assertEqual(len(matching_guilds), 1)

        ranked_roles = [
            rank for rank in matching_guilds[0] if rank.get("rank_to_get") is not None
        ]
        mythic_rank = find_rank_by_name(ranked_roles, "Mythic Idol")
        self.assertIsNotNone(mythic_rank)
        self.assertIn("TMW Owner", mythic_rank["legacy_names"])
        self.assertTrue(mythic_rank["emoji"].startswith("<:owner:"))

        bonus_rank_index = next(
            index
            for index, rank in enumerate(ranked_roles)
            if rank.get("ranktable_heading") == "Bonus Ranks"
        )
        self.assertEqual(ranked_roles[bonus_rank_index - 1]["name"], "Eternal Idol")
        self.assertEqual(ranked_roles[bonus_rank_index]["name"], "Immortal Idol")
        self.assertEqual(ranked_roles[bonus_rank_index + 1]["name"], "Mythic Idol")

    def test_legacy_menu_name_resolves_to_current_rank(self):
        current_rank = {
            "name": "Current Rank",
            "legacy_names": ["Previous Rank"],
        }

        self.assertIs(
            find_rank_by_name([current_rank], "previous rank"),
            current_rank,
        )

    def test_quiz_menu_displays_only_the_current_rank_name(self):
        settings = {
            "rank_structure": {
                1: [
                    {
                        "name": "Current Rank",
                        "legacy_names": ["Previous Rank"],
                        "emoji": None,
                        "command": "quiz command",
                    }
                ]
            }
        }

        with patch.object(gatekeeper, "gatekeeper_settings", settings):
            menu = DynamicQuizMenu(Mock(), 1)

        self.assertEqual(menu.item.options[0].label, "Current Rank")
        self.assertEqual(menu.item.options[0].value, "Current Rank")

    async def test_old_menu_interaction_refreshes_the_existing_message(self):
        settings = {
            "rank_structure": {
                1: [
                    {
                        "name": "Current Rank",
                        "legacy_names": ["Previous Rank"],
                        "emoji": None,
                        "command": "quiz command",
                        "no_timeout": False,
                    }
                ]
            }
        }
        levelup = Mock()
        levelup.rank_has_cooldown = AsyncMock(return_value=True)
        levelup.is_on_cooldown_create = AsyncMock(
            return_value=(True, "Still on cooldown")
        )
        levelup._record_quiz_menu_message = AsyncMock()
        response = SimpleNamespace(
            edit_message=AsyncMock(),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            data={"custom_id": "quizmenu-guild:1"},
            guild=SimpleNamespace(id=1),
            message=SimpleNamespace(id=500),
            user=SimpleNamespace(id=100),
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch.object(gatekeeper, "gatekeeper_settings", settings):
            menu = DynamicQuizMenu(levelup, 1)
            menu.item._values = ["Previous Rank"]
            await menu.callback(interaction)

        response.edit_message.assert_awaited_once()
        response.send_message.assert_not_awaited()
        levelup.rank_has_cooldown.assert_awaited_once_with(1, "Current Rank")
        levelup._record_quiz_menu_message.assert_awaited_once_with(
            interaction.message,
            1,
        )
        interaction.followup.send.assert_awaited_once_with(
            "Still on cooldown",
            ephemeral=True,
        )

    async def test_legacy_quiz_records_are_migrated_to_current_name(self):
        bot = Mock()
        bot.RUN = AsyncMock()
        cog = LevelUp(bot)
        settings = {
            "rank_structure": {
                1: [
                    {
                        "name": "Current Rank",
                        "legacy_names": ["Previous Rank"],
                    }
                ]
            }
        }

        with patch.object(gatekeeper, "gatekeeper_settings", settings):
            await cog.migrate_legacy_quiz_names()

        self.assertEqual(
            bot.RUN.await_args_list,
            [
                call(
                    RENAME_QUIZ_ATTEMPTS,
                    ("Current Rank", 1, "Previous Rank"),
                ),
                call(
                    COPY_RENAMED_PASSED_QUIZZES,
                    ("Current Rank", 1, "Previous Rank"),
                ),
                call(
                    DELETE_RENAMED_PASSED_QUIZZES,
                    (1, "Previous Rank"),
                ),
            ],
        )

    async def test_startup_finds_and_edits_existing_quiz_menu_message(self):
        settings = {
            "rank_structure": {
                1: [
                    {
                        "name": "Current Rank",
                        "emoji": None,
                        "command": "quiz command",
                    }
                ]
            },
            "rank_settings": {1: {"quiz_channel": 50}},
        }
        menu_component = SimpleNamespace(custom_id="quizmenu-guild:1")
        message = SimpleNamespace(
            id=500,
            author=SimpleNamespace(id=999),
            channel=SimpleNamespace(id=50),
            components=[SimpleNamespace(children=[menu_component])],
            edit=AsyncMock(),
        )

        async def channel_history(*, limit):
            self.assertIsNone(limit)
            yield message

        channel = SimpleNamespace(id=50, history=channel_history)
        bot = Mock(user=SimpleNamespace(id=999))
        bot.GET_ONE = AsyncMock(return_value=None)
        bot.RUN = AsyncMock()
        bot.get_channel.return_value = channel
        cog = LevelUp(bot)

        with patch.object(gatekeeper, "gatekeeper_settings", settings):
            await cog.refresh_quiz_menu_messages()

        message.edit.assert_awaited_once()
        refreshed_view = message.edit.await_args.kwargs["view"]
        self.assertEqual(
            refreshed_view.children[0].item.options[0].label, "Current Rank"
        )
        bot.RUN.assert_awaited_once_with(
            UPSERT_QUIZ_MENU_MESSAGE,
            (1, 50, 500),
        )

    async def test_later_startups_fetch_the_recorded_menu_directly(self):
        settings = {
            "rank_structure": {
                1: [
                    {
                        "name": "Current Rank",
                        "emoji": None,
                        "command": "quiz command",
                    }
                ]
            },
            "rank_settings": {1: {"quiz_channel": 50}},
        }
        message = SimpleNamespace(
            id=500,
            channel=SimpleNamespace(id=50),
            components=[
                SimpleNamespace(
                    children=[SimpleNamespace(custom_id="quizmenu-guild:1")]
                )
            ],
            edit=AsyncMock(),
        )
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        bot = Mock(user=SimpleNamespace(id=999))
        bot.GET_ONE = AsyncMock(return_value=(50, 500))
        bot.RUN = AsyncMock()
        bot.get_channel.return_value = channel
        cog = LevelUp(bot)

        with patch.object(gatekeeper, "gatekeeper_settings", settings):
            await cog.refresh_quiz_menu_messages()

        channel.fetch_message.assert_awaited_once_with(500)
        message.edit.assert_awaited_once()
        bot.RUN.assert_awaited_once_with(
            UPSERT_QUIZ_MENU_MESSAGE,
            (1, 50, 500),
        )


class ManualQuizGrantTests(unittest.IsolatedAsyncioTestCase):
    guild_id = 1

    def setUp(self):
        self.roles = {
            10: SimpleNamespace(id=10, name="Student", position=1),
            20: SimpleNamespace(id=20, name="Prima Idol", position=2),
        }
        self.rank_structure = [
            {
                "name": "Student",
                "combination_rank": False,
                "rank_to_get": 10,
            },
            {
                "name": "GN1",
                "combination_rank": False,
                "rank_to_get": None,
            },
            {
                "name": "GN2",
                "combination_rank": False,
                "rank_to_get": None,
            },
            {
                "name": "Prima Idol Vocab",
                "combination_rank": False,
                "rank_to_get": None,
            },
            {
                "name": "Divine Idol Vocab",
                "combination_rank": False,
                "rank_to_get": None,
            },
            {
                "name": "Eternal Idol Vocab",
                "combination_rank": False,
                "rank_to_get": None,
            },
            {
                "name": "Prima Idol",
                "combination_rank": True,
                "rank_to_get": 20,
            },
        ]
        self.settings = {
            "rank_structure": {self.guild_id: self.rank_structure}
        }
        self.bot = Mock()
        self.bot.GET = AsyncMock(return_value=[])
        self.cog = LevelUp(self.bot)
        self.cog.reward_user = AsyncMock(return_value=True)
        self.cog.send_in_announcement_channel = AsyncMock()
        self.user = SimpleNamespace(
            id=100,
            mention="<@100>",
            guild=SimpleNamespace(
                id=self.guild_id,
                get_role=self.roles.get,
            ),
        )

    @staticmethod
    def make_interaction(guild_id=1):
        return SimpleNamespace(
            guild_id=guild_id,
            user=SimpleNamespace(id=900, name="admin"),
            response=SimpleNamespace(
                defer=AsyncMock(),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def invoke(self, quiz_name, *, announce=True, guild_id=1):
        interaction = self.make_interaction(guild_id)
        with patch.object(gatekeeper, "gatekeeper_settings", self.settings):
            await LevelUp.assign_quiz.callback(
                self.cog,
                interaction,
                self.user,
                quiz_name,
                announce,
            )
        return interaction

    async def test_production_autocomplete_can_find_every_defined_entry(self):
        for guild_id, rank_structure in gatekeeper_settings["rank_structure"].items():
            interaction = self.make_interaction(guild_id=guild_id)
            for rank in rank_structure:
                with self.subTest(guild_id=guild_id, rank_name=rank["name"]):
                    choices = await assign_quiz_autocomplete(
                        interaction,
                        rank["name"],
                    )
                    self.assertIn(
                        rank["name"],
                        {choice.value for choice in choices},
                    )

    async def test_autocomplete_returns_all_defined_matching_entries(self):
        interaction = self.make_interaction()

        with patch.object(gatekeeper, "gatekeeper_settings", self.settings):
            all_choices = await assign_quiz_autocomplete(interaction, "")
            vocab_choices = await assign_quiz_autocomplete(interaction, "VoCaB")

        self.assertEqual(
            [choice.value for choice in all_choices],
            [rank["name"] for rank in self.rank_structure],
        )
        self.assertEqual(
            [choice.value for choice in vocab_choices],
            [
                "Prima Idol Vocab",
                "Divine Idol Vocab",
                "Eternal Idol Vocab",
            ],
        )

    async def test_autocomplete_is_empty_for_an_unconfigured_guild(self):
        interaction = self.make_interaction(guild_id=999)

        with patch.object(gatekeeper, "gatekeeper_settings", self.settings):
            choices = await assign_quiz_autocomplete(interaction, "")

        self.assertEqual(choices, [])

    async def test_runtime_permission_check_rejects_non_admins(self):
        command = LevelUp.assign_quiz
        non_admin_interaction = SimpleNamespace(
            permissions=discord.Permissions.none()
        )

        with self.assertRaises(discord.app_commands.MissingPermissions):
            await command._check_can_run(non_admin_interaction)

        admin_interaction = SimpleNamespace(
            permissions=discord.Permissions(administrator=True)
        )
        self.assertTrue(await command._check_can_run(admin_interaction))
        self.assertTrue(command.default_permissions.administrator)
        announce_parameter = next(
            parameter
            for parameter in command.parameters
            if parameter.name == "announce"
        )
        self.assertEqual(
            announce_parameter.type,
            discord.AppCommandOptionType.boolean,
        )
        self.assertTrue(announce_parameter.required)

    async def test_arbitrary_input_is_rejected(self):
        for quiz_name in (
            "unknown quiz",
            "GN1'); DROP TABLE passed_quizzes; --",
        ):
            with self.subTest(quiz_name=quiz_name):
                interaction = await self.invoke(quiz_name)
                interaction.response.send_message.assert_awaited_once_with(
                    "Invalid gatekeeper rank or quiz name.",
                    ephemeral=True,
                )
                interaction.response.defer.assert_not_awaited()

        self.bot.GET.assert_not_awaited()
        self.cog.reward_user.assert_not_awaited()

    async def test_unconfigured_guild_is_rejected_privately(self):
        interaction = await self.invoke("GN1", guild_id=999)

        interaction.response.send_message.assert_awaited_once_with(
            "Quiz grants are not configured for this server.", ephemeral=True
        )
        interaction.response.defer.assert_not_awaited()
        self.cog.reward_user.assert_not_awaited()

    async def test_partial_pass_uses_canonical_name_and_announces(self):
        interaction = await self.invoke("gn1", announce=True)
        quiz_data = find_rank_by_name(self.rank_structure, "GN1")

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.bot.GET.assert_awaited_once_with(
            GET_PASSED_QUIZZES,
            (self.guild_id, self.user.id),
        )
        self.cog.reward_user.assert_awaited_once_with(
            self.user,
            quiz_data,
            announce_combination_rank=True,
        )
        self.cog.send_in_announcement_channel.assert_awaited_once_with(
            self.user,
            "<@100> has passed the GN1 quiz!",
        )
        interaction.followup.send.assert_awaited_once_with(
            "Successfully assigned `GN1` to <@100>.",
            ephemeral=True,
        )

    async def test_discord_role_rank_can_be_assigned_silently(self):
        interaction = await self.invoke("Student", announce=False)
        rank_data = find_rank_by_name(self.rank_structure, "Student")

        self.cog.reward_user.assert_awaited_once_with(
            self.user,
            rank_data,
            announce_combination_rank=False,
        )
        self.cog.send_in_announcement_channel.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            "Successfully assigned `Student` to <@100>.",
            ephemeral=True,
        )

    async def test_combination_rank_can_be_assigned_and_announced(self):
        interaction = await self.invoke("Prima Idol", announce=True)
        rank_data = find_rank_by_name(self.rank_structure, "Prima Idol")

        self.cog.reward_user.assert_awaited_once_with(
            self.user,
            rank_data,
            announce_combination_rank=True,
        )
        self.cog.send_in_announcement_channel.assert_awaited_once_with(
            self.user,
            "<@100> is now a Prima Idol!",
        )
        interaction.followup.send.assert_awaited_once_with(
            "Successfully assigned `Prima Idol` to <@100>.",
            ephemeral=True,
        )

    async def test_existing_pass_is_idempotent(self):
        self.bot.GET.return_value = [("GN2",)]

        interaction = await self.invoke("GN2", announce=True)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.cog.reward_user.assert_not_awaited()
        self.cog.send_in_announcement_channel.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            "<@100> already has `GN2` recorded in gatekeeper.",
            ephemeral=True,
        )

    async def test_all_rank_types_and_announcement_control_end_to_end(self):
        settings = {
            "rank_structure": {
                self.guild_id: [
                    {
                        "name": "Student",
                        "combination_rank": False,
                        "rank_to_get": 10,
                    },
                    {
                        "name": "GN2",
                        "combination_rank": False,
                        "rank_to_get": None,
                    },
                    {
                        "name": "Prima Idol Vocab",
                        "combination_rank": False,
                        "rank_to_get": None,
                    },
                    {
                        "name": "Prima Idol",
                        "combination_rank": True,
                        "rank_to_get": 20,
                        "quizzes_required": ["GN2", "Prima Idol Vocab"],
                    },
                ]
            },
            "rank_settings": {self.guild_id: {"announce_channel": 30}},
        }
        student_role = SimpleNamespace(id=10, name="Student", position=1)
        prima_role = SimpleNamespace(id=20, name="Prima Idol", position=2)
        roles = {role.id: role for role in (student_role, prima_role)}
        announcement_channel = SimpleNamespace(send=AsyncMock())
        guild = SimpleNamespace(
            id=self.guild_id,
            get_role=roles.get,
            get_channel=lambda channel_id: (
                announcement_channel if channel_id == 30 else None
            ),
        )

        def make_member(member_id):
            member = SimpleNamespace(
                id=member_id,
                mention=f"<@{member_id}>",
                guild=guild,
                roles=[],
            )

            async def remove_roles(*roles_to_remove):
                member.roles = [
                    role
                    for role in member.roles
                    if role not in roles_to_remove
                ]

            async def add_role(role):
                if role not in member.roles:
                    member.roles.append(role)

            member.remove_roles = AsyncMock(side_effect=remove_roles)
            member.add_roles = AsyncMock(side_effect=add_role)
            return member

        silent_member = make_member(100)
        announced_member = make_member(101)

        with tempfile.TemporaryDirectory() as directory:
            bot = TMWBot(
                command_prefix="%",
                database_encryption_key=bytes(range(32)),
                path_to_db=str(Path(directory) / "db.sqlite3"),
            )
            try:
                await bot.RUN(CREATE_PASSED_QUIZZES_TABLE)
                cog = LevelUp(bot)

                with patch.object(gatekeeper, "gatekeeper_settings", settings):
                    await LevelUp.assign_quiz.callback(
                        cog,
                        self.make_interaction(),
                        silent_member,
                        "Student",
                        False,
                    )
                    concurrent_gn2_interactions = [
                        self.make_interaction(),
                        self.make_interaction(),
                    ]
                    await asyncio.gather(
                        *(
                            LevelUp.assign_quiz.callback(
                                cog,
                                interaction,
                                silent_member,
                                "GN2",
                                False,
                            )
                            for interaction in concurrent_gn2_interactions
                        )
                    )
                    await LevelUp.assign_quiz.callback(
                        cog,
                        self.make_interaction(),
                        silent_member,
                        "Prima Idol Vocab",
                        False,
                    )

                    duplicate_interaction = self.make_interaction()
                    await LevelUp.assign_quiz.callback(
                        cog,
                        duplicate_interaction,
                        silent_member,
                        "Prima Idol Vocab",
                        False,
                    )

                    for quiz_name in ("GN2", "Prima Idol Vocab"):
                        await LevelUp.assign_quiz.callback(
                            cog,
                            self.make_interaction(),
                            announced_member,
                            quiz_name,
                            True,
                        )

                silent_quizzes = await bot.GET(
                    GET_PASSED_QUIZZES,
                    (self.guild_id, silent_member.id),
                )
                announced_quizzes = await bot.GET(
                    GET_PASSED_QUIZZES,
                    (self.guild_id, announced_member.id),
                )
            finally:
                await bot.close()

        self.assertEqual(
            {row[0] for row in silent_quizzes},
            {"Student", "GN2", "Prima Idol Vocab", "Prima Idol"},
        )
        self.assertEqual(
            {row[0] for row in announced_quizzes},
            {"GN2", "Prima Idol Vocab", "Prima Idol"},
        )
        self.assertEqual(silent_member.roles, [prima_role])
        self.assertEqual(announced_member.roles, [prima_role])
        self.assertEqual(
            silent_member.add_roles.await_args_list,
            [call(student_role), call(prima_role)],
        )
        self.assertEqual(
            announced_member.add_roles.await_args_list,
            [call(prima_role)],
        )
        self.assertEqual(
            announcement_channel.send.await_args_list,
            [
                call("<@101> has passed the GN2 quiz!"),
                call("<@101> is now a Prima Idol!"),
                call("<@101> has passed the Prima Idol Vocab quiz!"),
            ],
        )
        concurrent_messages = {
            interaction.followup.send.await_args.args[0]
            for interaction in concurrent_gn2_interactions
        }
        self.assertEqual(
            concurrent_messages,
            {
                "Successfully assigned `GN2` to <@100>.",
                "<@100> already has `GN2` recorded in gatekeeper.",
            },
        )
        duplicate_interaction.followup.send.assert_awaited_once_with(
            "<@100> already has `Prima Idol Vocab` recorded in gatekeeper.",
            ephemeral=True,
        )


class RanktableTests(unittest.TestCase):
    def test_configured_heading_is_rendered_before_its_rank(self):
        standard_role = make_role(10, [1, 2])
        first_bonus_role = make_role(20, [2])
        second_bonus_role = make_role(30, [])
        guild = Mock(member_count=5)
        guild.get_role.side_effect = {
            10: standard_role,
            20: first_bonus_role,
            30: second_bonus_role,
        }.get
        rank_structure = [
            {"rank_to_get": 10},
            {"rank_to_get": 20, "ranktable_heading": "Bonus Ranks"},
            {"rank_to_get": 30},
        ]

        description = build_ranktable_description(guild, rank_structure)

        self.assertEqual(
            description.splitlines(),
            [
                "<@&10>: 2 (100.00%)",
                "**Bonus Ranks**",
                "<@&20>: 1 (50.00%)",
                "<@&30>: 0 (0.00%)",
                "",
                "Total ranked members: 2",
                "Total member count: 5",
            ],
        )

    def test_empty_ranktable_does_not_divide_by_zero(self):
        empty_role = make_role(10, [])
        guild = Mock(member_count=0)
        guild.get_role.return_value = empty_role

        description = build_ranktable_description(
            guild,
            [{"rank_to_get": 10}],
        )

        self.assertIn("<@&10>: 0 (0.00%)", description)


if __name__ == "__main__":
    unittest.main()
