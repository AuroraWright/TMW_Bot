import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
        "message_filter": "link_or_attachment",
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
        self.assertEqual(parsed.message_filter, "link_or_attachment")
        self.assertEqual(parsed.send_delay_seconds, 1.25)

    def test_rejects_unknown_message_filter(self):
        with self.assertRaises(ValueError):
            settings(message_filter="everything")

    def test_rejects_aggressive_send_delay(self):
        with self.assertRaises(ValueError):
            settings(send_delay_seconds=0.5)


class OneTimeMessageForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_cog_load_migrates_existing_job_table_for_filter_identity(self):
        bot = SimpleNamespace(
            RUN=AsyncMock(),
            GET=AsyncMock(
                return_value=[
                    (0, "job_id"),
                    (1, "source_channel_id"),
                    (2, "status"),
                ]
            ),
        )
        cog = OneTimeMessageForward(bot, settings())

        await cog.cog_load()

        executed_sql = [call.args[0] for call in bot.RUN.await_args_list]
        self.assertTrue(
            any("ADD COLUMN message_filter" in query for query in executed_sql)
        )

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
        cog = OneTimeMessageForward(SimpleNamespace(), settings())

        messages = [
            item.id async for item in cog._source_messages(source, message, message)
        ]

        self.assertEqual(messages, [10])
        self.assertEqual(source.cursors, [])

    async def test_source_range_is_fixed_to_oldest_and_latest_messages(self):
        first = SimpleNamespace(id=10)
        last = SimpleNamespace(id=20)

        class FakeBoundarySource:
            def history(self, *, limit, oldest_first=False):
                async def messages():
                    yield first if oldest_first else last

                return messages()

        discovered = await OneTimeMessageForward._discover_source_range(
            FakeBoundarySource()
        )

        self.assertEqual(discovered, (first, last))

    def test_filter_matches_http_links_or_any_attachment(self):
        cog = OneTimeMessageForward(SimpleNamespace(), settings())

        self.assertTrue(
            cog._matches_message_filter(
                SimpleNamespace(content="See HTTPS://example.com/file", attachments=[])
            )
        )
        self.assertTrue(
            cog._matches_message_filter(
                SimpleNamespace(content="", attachments=[SimpleNamespace()])
            )
        )
        self.assertFalse(
            cog._matches_message_filter(
                SimpleNamespace(content="No resources here", attachments=[])
            )
        )

    def test_only_discord_supported_message_types_are_forwarded(self):
        from cogs.one_time_message_forward import FORWARDABLE_MESSAGE_TYPES

        self.assertIn(discord.MessageType.default, FORWARDABLE_MESSAGE_TYPES)
        self.assertIn(discord.MessageType.reply, FORWARDABLE_MESSAGE_TYPES)
        self.assertNotIn(discord.MessageType.call, FORWARDABLE_MESSAGE_TYPES)

    async def test_completed_job_makes_no_discord_requests(self):
        bot = SimpleNamespace(
            GET_ONE=AsyncMock(
                return_value=(
                    1,
                    2,
                    10,
                    20,
                    3,
                    4,
                    None,
                    "complete",
                    "link_or_attachment",
                )
            )
        )
        cog = OneTimeMessageForward(bot, settings())
        cog._preflight = AsyncMock()

        await cog._run_once()

        cog._preflight.assert_not_awaited()

    async def test_forwarding_ignores_messages_without_link_or_attachment(self):
        destination = SimpleNamespace()
        linked_message = SimpleNamespace(
            id=10,
            content="https://example.com",
            attachments=[],
            type=discord.MessageType.default,
            forward=AsyncMock(return_value=SimpleNamespace(id=110)),
        )
        plain_message = SimpleNamespace(
            id=15,
            content="plain text",
            attachments=[],
            type=discord.MessageType.default,
            forward=AsyncMock(),
        )
        attached_message = SimpleNamespace(
            id=20,
            content="",
            attachments=[SimpleNamespace(filename="resource.mp4")],
            type=discord.MessageType.default,
            forward=AsyncMock(return_value=SimpleNamespace(id=120)),
        )
        source = FakeSourceChannel([[plain_message, attached_message]])
        bot = SimpleNamespace(GET=AsyncMock(return_value=[]))
        cog = OneTimeMessageForward(bot, settings())
        cog._record_result = AsyncMock()

        with patch(
            "cogs.one_time_message_forward.asyncio.sleep",
            new=AsyncMock(),
        ):
            await cog._forward_messages(
                source,
                destination,
                linked_message,
                attached_message,
            )

        linked_message.forward.assert_awaited_once_with(destination)
        plain_message.forward.assert_not_awaited()
        attached_message.forward.assert_awaited_once_with(destination)
        self.assertEqual(cog._record_result.await_count, 2)

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

        await cog._reconcile_destination(
            FakeDestination(),
            scan_after_id=50,
            first_message_id=10,
            last_message_id=20,
        )

        cog._record_result.assert_awaited_once_with(
            15,
            99,
            "forwarded",
            "Recovered from destination history after a restart.",
        )


if __name__ == "__main__":
    unittest.main()
