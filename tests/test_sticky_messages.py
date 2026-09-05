import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cogs.sticky_messages import StickyMessages


def make_message(
    message_id,
    *,
    author_id=100,
    bot=False,
    content="message",
    interaction=None,
    guild_id=1,
    channel=None,
):
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=author_id, bot=bot),
        content=content,
        embeds=[],
        attachments=[],
        interaction=interaction,
        guild=SimpleNamespace(id=guild_id),
        channel=channel,
    )


class StickyMessagesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = SimpleNamespace(id=2, send=AsyncMock())
        self.bot = Mock()
        self.bot.RUN = AsyncMock()
        self.bot.GET_ONE = AsyncMock(return_value=None)
        self.bot.cached_messages = []
        self.bot.user = SimpleNamespace(id=999)
        self.cog = StickyMessages(self.bot)
        self.interaction = SimpleNamespace(
            id=777,
            guild_id=1,
            channel_id=self.channel.id,
            channel=self.channel,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    @staticmethod
    def history_with(*messages):
        async def history(**kwargs):
            for message in messages:
                yield message

        return history

    async def test_command_can_sticky_a_bot_message(self):
        source_message = make_message(
            10,
            author_id=200,
            bot=True,
            content="automated update",
            channel=self.channel,
        )
        self.channel.history = self.history_with(source_message)
        self.channel.send.return_value = SimpleNamespace(id=20)

        await self.cog.sticky_last_message.callback(self.cog, self.interaction)

        self.channel.send.assert_awaited_once_with(
            "📌 **Sticky Message:**\n\nautomated update",
            embed=None,
            files=[],
        )
        self.bot.RUN.assert_awaited_once()

    async def test_command_does_not_discard_another_apps_interaction_message(self):
        source_message = make_message(
            10,
            author_id=200,
            bot=True,
            content="application output",
            interaction=SimpleNamespace(id=123, name="sticky_last_message"),
            channel=self.channel,
        )
        self.channel.history = self.history_with(source_message)
        self.channel.send.return_value = SimpleNamespace(id=20)

        await self.cog.sticky_last_message.callback(self.cog, self.interaction)

        self.channel.send.assert_awaited_once()
        self.assertIn("application output", self.channel.send.await_args.args[0])

    async def test_bot_message_reposts_the_configured_sticky(self):
        old_sticky = SimpleNamespace(delete=AsyncMock())
        original_message = make_message(
            10,
            author_id=200,
            bot=True,
            content="automated update",
            channel=self.channel,
        )
        self.bot.GET_ONE.return_value = (original_message.id, 20)
        self.cog._get_message = AsyncMock(side_effect=[old_sticky, original_message])
        self.channel.send.return_value = SimpleNamespace(id=30)
        incoming_message = make_message(
            40,
            author_id=200,
            bot=True,
            channel=self.channel,
        )

        await self.cog.on_message(incoming_message)

        old_sticky.delete.assert_awaited_once()
        self.channel.send.assert_awaited_once()
        self.bot.RUN.assert_awaited_once()

    async def test_own_sticky_message_does_not_trigger_another_repost(self):
        self.cog._self_sticky_message_ids.add(30)
        own_sticky = make_message(
            30,
            author_id=self.bot.user.id,
            bot=True,
            channel=self.channel,
        )

        await self.cog.on_message(own_sticky)

        self.bot.GET_ONE.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
