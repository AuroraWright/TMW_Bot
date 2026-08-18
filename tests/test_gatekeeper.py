import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import cogs.gatekeeper as gatekeeper
from cogs.gatekeeper import (
    KOTOBA_BOT_ID,
    LevelUp,
)


class GatekeeperAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.user = object()
        self.level_up = LevelUp(self.bot)

        self.guild = SimpleNamespace(id=12345)
        self.member = SimpleNamespace(
            id=67890,
            mention="<@67890>",
            guild=self.guild,
            roles=[],
            send=AsyncMock(),
        )
        self.guild.get_member = lambda _: self.member

        self.message = SimpleNamespace(
            author=SimpleNamespace(id=KOTOBA_BOT_ID, bot=True),
            guild=self.guild,
            content="k!q",
            channel=SimpleNamespace(send=AsyncMock()),
        )

        self.quiz_data = {"name": "N5", "rank_to_get": None, "require_role": None}
        self.quiz_message = f"{self.member.mention} has passed the {self.quiz_data['name']} quiz!"
        self.quiz_result = {
            "participants": [{"discordUser": {"id": str(self.member.id)}}],
        }

    async def test_level_up_suppresses_generic_announcement_when_award_blocked(self):
        with (
            patch.object(self.level_up, "is_command_input_valid", AsyncMock(return_value=True)),
            patch.object(gatekeeper, "get_quiz_id", AsyncMock(return_value="quiz-id")),
            patch.object(gatekeeper, "extract_quiz_result_from_id", AsyncMock(return_value=self.quiz_result)),
            patch.object(self.level_up, "get_corresponding_quiz_data", AsyncMock(return_value=self.quiz_data)),
            patch.object(self.level_up, "rank_has_cooldown", AsyncMock(return_value=False)),
            patch.object(self.level_up, "is_on_cooldown", AsyncMock(return_value=False)),
            patch.object(gatekeeper, "verify_quiz_settings", AsyncMock(return_value=(True, self.quiz_message))),
            patch.object(self.level_up, "already_owns_higher_or_same_role", AsyncMock(return_value=False)),
            patch.object(self.level_up, "reward_user", AsyncMock(return_value=False)),
            patch.object(self.level_up, "send_in_announcement_channel", AsyncMock()) as send_announcement,
        ):
            await self.level_up.level_up_routine(self.message)

        send_announcement.assert_not_awaited()
        self.member.send.assert_awaited_once_with(
            f"Congratulations! You passed the {self.quiz_data['name']} quiz!"
        )

    async def test_level_up_announces_generic_pass_when_not_blocked(self):
        with (
            patch.object(self.level_up, "is_command_input_valid", AsyncMock(return_value=True)),
            patch.object(gatekeeper, "get_quiz_id", AsyncMock(return_value="quiz-id")),
            patch.object(gatekeeper, "extract_quiz_result_from_id", AsyncMock(return_value=self.quiz_result)),
            patch.object(self.level_up, "get_corresponding_quiz_data", AsyncMock(return_value=self.quiz_data)),
            patch.object(self.level_up, "rank_has_cooldown", AsyncMock(return_value=False)),
            patch.object(self.level_up, "is_on_cooldown", AsyncMock(return_value=False)),
            patch.object(gatekeeper, "verify_quiz_settings", AsyncMock(return_value=(True, self.quiz_message))),
            patch.object(self.level_up, "already_owns_higher_or_same_role", AsyncMock(return_value=False)),
            patch.object(self.level_up, "reward_user", AsyncMock(return_value=True)),
            patch.object(self.level_up, "send_in_announcement_channel", AsyncMock()) as send_announcement,
        ):
            await self.level_up.level_up_routine(self.message)

        send_announcement.assert_awaited_once_with(self.member, self.quiz_message)
        self.member.send.assert_awaited_once_with(
            f"Congratulations! You passed the {self.quiz_data['name']} quiz!"
        )

    async def test_combination_rank_check_reports_blocked_award(self):
        combination_rank = {
            "name": "Combo Rank",
            "combination_rank": True,
            "rank_to_get": 111,
            "quizzes_required": ["N5", "N4"],
        }

        with (
            patch.object(gatekeeper, "gatekeeper_settings", {"rank_structure": {self.guild.id: [combination_rank]}}),
            patch.object(self.level_up.bot, "GET", AsyncMock(return_value=[("N5",), ("N4",)])),
            patch.object(self.level_up, "already_owns_higher_or_same_role", AsyncMock(return_value=True)),
            patch.object(self.level_up, "reward_user", AsyncMock()) as reward_user,
            patch.object(self.level_up, "send_in_announcement_channel", AsyncMock()) as send_announcement,
        ):
            blocked = await self.level_up.check_if_combination_rank_earned(self.member)

        self.assertEqual(blocked, False)
        reward_user.assert_not_awaited()
        send_announcement.assert_not_awaited()

    async def test_combination_rank_check_announces_when_rank_is_earned(self):
        combination_rank = {
            "name": "Combo Rank",
            "combination_rank": True,
            "rank_to_get": 111,
            "quizzes_required": ["N5", "N4"],
        }
        role = SimpleNamespace(name="Combo Role")
        self.guild.get_role = lambda _: role

        with (
            patch.object(gatekeeper, "gatekeeper_settings", {"rank_structure": {self.guild.id: [combination_rank]}}),
            patch.object(self.level_up.bot, "GET", AsyncMock(return_value=[("N5",), ("N4",)])),
            patch.object(self.level_up, "already_owns_higher_or_same_role", AsyncMock(return_value=False)),
            patch.object(self.level_up, "reward_user", AsyncMock(return_value=True)) as reward_user,
            patch.object(self.level_up, "send_in_announcement_channel", AsyncMock()) as send_announcement,
        ):
            status = await self.level_up.check_if_combination_rank_earned(self.member)

        self.assertEqual(status, True)
        reward_user.assert_awaited_once_with(self.member, combination_rank)
        send_announcement.assert_awaited_once_with(
            self.member,
            f"{self.member.mention} is now a {role.name}!",
        )


if __name__ == "__main__":
    unittest.main()
