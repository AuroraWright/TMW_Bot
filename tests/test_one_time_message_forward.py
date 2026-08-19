import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.one_time_message_forward import (
    MessageForwardSettings,
    OneTimeMessageForward,
    parse_message_forward_jobs,
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
        "send_delay_seconds": 30.0,
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
        self.assertEqual(parsed.send_delay_seconds, 30.0)

    def test_rejects_unknown_message_filter(self):
        with self.assertRaises(ValueError):
            settings(message_filter="everything")

    def test_rejects_aggressive_send_delay(self):
        with self.assertRaises(ValueError):
            settings(send_delay_seconds=0.5)

    def test_queue_parser_preserves_order_and_applies_shared_settings(self):
        jobs = parse_message_forward_jobs(
            {
                "enabled": True,
                "message_filter": "link_or_attachment",
                "send_delay_seconds": 30,
                "retry_delay_seconds": 300,
                "max_attempts": 3,
                "jobs": [
                    {
                        "job_id": "first",
                        "source_guild_id": 1,
                        "source_channel_id": 2,
                        "destination_guild_id": 3,
                        "destination_channel_id": 4,
                    },
                    {
                        "job_id": "second",
                        "source_guild_id": 1,
                        "source_channel_id": 5,
                        "destination_guild_id": 3,
                        "destination_channel_id": 6,
                    },
                ],
            }
        )

        self.assertEqual([job.job_id for job in jobs], ["first", "second"])
        self.assertTrue(all(job.enabled for job in jobs))
        self.assertTrue(all(job.send_delay_seconds == 30 for job in jobs))

    def test_queue_parser_rejects_duplicate_job_ids(self):
        shared_job = {
            "job_id": "duplicate",
            "source_guild_id": 1,
            "source_channel_id": 2,
            "destination_guild_id": 3,
            "destination_channel_id": 4,
        }
        with self.assertRaises(ValueError):
            parse_message_forward_jobs(
                {
                    "enabled": True,
                    "message_filter": "link_or_attachment",
                    "jobs": [shared_job, shared_job],
                }
            )


class OneTimeMessageForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_runs_jobs_in_configured_order(self):
        jobs = (
            settings(job_id="first"),
            settings(job_id="second"),
            settings(job_id="third"),
        )
        cog = OneTimeMessageForward(SimpleNamespace(), jobs)
        started_jobs = []

        async def complete_current_job():
            started_jobs.append(cog.settings.job_id)
            return True

        cog._run_with_retries = AsyncMock(side_effect=complete_current_job)

        await cog._run_queue()

        self.assertEqual(started_jobs, ["first", "second", "third"])

    async def test_queue_does_not_start_later_jobs_after_failure(self):
        jobs = (
            settings(job_id="first"),
            settings(job_id="second"),
            settings(job_id="must-not-start"),
        )
        cog = OneTimeMessageForward(SimpleNamespace(), jobs)
        started_jobs = []

        async def run_current_job():
            started_jobs.append(cog.settings.job_id)
            return cog.settings.job_id == "first"

        cog._run_with_retries = AsyncMock(side_effect=run_current_job)

        await cog._run_queue()

        self.assertEqual(started_jobs, ["first", "second"])

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
        sleep = AsyncMock()

        with patch(
            "cogs.one_time_message_forward.asyncio.sleep",
            new=sleep,
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
        self.assertEqual(
            [awaited.args for awaited in sleep.await_args_list],
            [(30.0,), (30.0,)],
        )

    async def test_resume_skips_messages_already_checkpointed_in_database(self):
        destination = SimpleNamespace()
        checkpointed_message = SimpleNamespace(
            id=10,
            content="https://example.com/already-forwarded",
            attachments=[],
            type=discord.MessageType.default,
            forward=AsyncMock(),
        )
        pending_message = SimpleNamespace(
            id=20,
            content="",
            attachments=[SimpleNamespace(filename="pending.zip")],
            type=discord.MessageType.default,
            forward=AsyncMock(return_value=SimpleNamespace(id=120)),
        )
        source = FakeSourceChannel([[pending_message]])
        bot = SimpleNamespace(GET=AsyncMock(return_value=[(10, "forwarded")]))
        cog = OneTimeMessageForward(bot, settings())
        cog._record_result = AsyncMock()
        sleep = AsyncMock()

        with patch(
            "cogs.one_time_message_forward.asyncio.sleep",
            new=sleep,
        ):
            await cog._forward_messages(
                source,
                destination,
                checkpointed_message,
                pending_message,
            )

        checkpointed_message.forward.assert_not_awaited()
        pending_message.forward.assert_awaited_once_with(destination)
        cog._record_result.assert_awaited_once_with(20, 120, "forwarded")
        sleep.assert_awaited_once_with(30.0)

    async def test_resume_uses_stored_source_boundaries(self):
        existing_job = (
            1,
            2,
            10,
            20,
            3,
            4,
            50,
            "running",
            "link_or_attachment",
        )
        source = SimpleNamespace()
        destination = SimpleNamespace()
        first_message = SimpleNamespace(id=10)
        last_message = SimpleNamespace(id=20)
        bot = SimpleNamespace(GET_ONE=AsyncMock(return_value=existing_job))
        cog = OneTimeMessageForward(bot, settings())
        cog._preflight = AsyncMock(return_value=(source, destination))
        cog._discover_source_range = AsyncMock()
        cog._fetch_source_range = AsyncMock(return_value=(first_message, last_message))
        cog._initialise_job = AsyncMock(return_value=("running", 50))
        cog._reconcile_destination = AsyncMock()
        cog._forward_messages = AsyncMock()
        cog._complete_job = AsyncMock()

        await cog._run_once()

        cog._discover_source_range.assert_not_awaited()
        cog._fetch_source_range.assert_awaited_once_with(source, 10, 20)
        cog._initialise_job.assert_awaited_once_with(destination, 10, 20)
        cog._reconcile_destination.assert_awaited_once_with(
            destination,
            50,
            10,
            20,
        )
        cog._forward_messages.assert_awaited_once_with(
            source,
            destination,
            first_message,
            last_message,
        )
        cog._complete_job.assert_awaited_once_with()

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
