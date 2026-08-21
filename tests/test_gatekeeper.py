import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from cogs import gatekeeper
from cogs.gatekeeper import (
    COPY_RENAMED_PASSED_QUIZZES,
    DELETE_RENAMED_PASSED_QUIZZES,
    RENAME_QUIZ_ATTEMPTS,
    UPSERT_QUIZ_MENU_MESSAGE,
    DynamicQuizMenu,
    LevelUp,
    build_ranktable_description,
    find_rank_by_name,
    gatekeeper_settings,
)


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
