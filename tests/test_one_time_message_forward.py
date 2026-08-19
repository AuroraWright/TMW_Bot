import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from cogs.one_time_message_forward import (
    MessageForwardSettings,
    OneTimeMessageForward,
)


def settings(**overrides):
    values = {
        "enabled": True,
        "job_id": "test-forward",
        "source_guild_id": 1,
        "source_channel_id": 2,
        "first_message_id": 10,
        "last_message_id": 20,
        "destination_guild_id": 3,
        "destination_channel_id": 4,
        "send_delay_seconds": 1.25,
        "retry_delay_seconds": 60,
        "max_attempts": 3,
    }
    values.update(overrides)
    return MessageForwardSettings.from_mapping(values)


class FakeSourceChannel:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.cursors = []

    def history(self, *, limit, after, oldest_first):
        self.cursors.append(after.id)
        page = next(self.pages, [])

        async def messages():
            for message in page:
                yield message

        return messages()


class MessageForwardSettingsTests(unittest.TestCase):
    def test_settings_parse_valid_job(self):
        parsed = settings()

        self.assertTrue(parsed.enabled)
        self.assertEqual(parsed.first_message_id, 10)
        self.assertEqual(parsed.last_message_id, 20)
        self.assertEqual(parsed.send_delay_seconds, 1.25)

    def test_rejects_reversed_message_range(self):
        with self.assertRaises(ValueError):
            settings(first_message_id=21, last_message_id=20)

    def test_rejects_aggressive_send_delay(self):
        with self.assertRaises(ValueError):
            settings(send_delay_seconds=0.5)


class OneTimeMessageForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_range_is_inclusive_and_oldest_first(self):
        first = SimpleNamespace(id=10)
        middle = SimpleNamespace(id=15)
        last = SimpleNamespace(id=20)
        outside_range = SimpleNamespace(id=21)
        source = FakeSourceChannel([[middle, last, outside_range]])
        cog = OneTimeMessageForward(SimpleNamespace(), settings())

        messages = [
            message.id async for message in cog._source_messages(source, first, last)
        ]

        self.assertEqual(messages, [10, 15, 20])
        self.assertEqual(source.cursors, [10])

    async def test_single_message_range_is_not_duplicated(self):
        message = SimpleNamespace(id=10)
        source = FakeSourceChannel([])
        cog = OneTimeMessageForward(
            SimpleNamespace(),
            settings(first_message_id=10, last_message_id=10),
        )

        messages = [
            item.id async for item in cog._source_messages(source, message, message)
        ]

        self.assertEqual(messages, [10])
        self.assertEqual(source.cursors, [])

    def test_only_discord_supported_message_types_are_forwarded(self):
        from cogs.one_time_message_forward import FORWARDABLE_MESSAGE_TYPES

        self.assertIn(discord.MessageType.default, FORWARDABLE_MESSAGE_TYPES)
        self.assertIn(discord.MessageType.reply, FORWARDABLE_MESSAGE_TYPES)
        self.assertNotIn(discord.MessageType.call, FORWARDABLE_MESSAGE_TYPES)

    async def test_completed_job_makes_no_discord_requests(self):
        bot = SimpleNamespace(
            GET_ONE=AsyncMock(return_value=(1, 2, 10, 20, 3, 4, None, "complete"))
        )
        cog = OneTimeMessageForward(bot, settings())
        cog._preflight = AsyncMock()

        await cog._run_once()

        cog._preflight.assert_not_awaited()

    async def test_destination_reconciliation_recovers_forward_checkpoint(self):
        forwarded_reference = SimpleNamespace(
            type=discord.MessageReferenceType.forward,
            channel_id=2,
            message_id=15,
        )
        destination_message = SimpleNamespace(id=99, reference=forwarded_reference)

        class FakeDestination:
            def history(self, **kwargs):
                async def messages():
                    yield destination_message

                return messages()

        cog = OneTimeMessageForward(SimpleNamespace(), settings())
        cog._record_result = AsyncMock()

        await cog._reconcile_destination(FakeDestination(), scan_after_id=50)

        cog._record_result.assert_awaited_once_with(
            15,
            99,
            "forwarded",
            "Recovered from destination history after a restart.",
        )


if __name__ == "__main__":
    unittest.main()
