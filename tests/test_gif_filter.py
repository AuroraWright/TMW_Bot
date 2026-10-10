import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.gif_filter import GifFilter, contains_gif


def http_error(kind=discord.Forbidden):
    status = 404 if kind is discord.NotFound else 403
    return kind(SimpleNamespace(status=status, reason="Failure"), "Failure")


class GifDetectionTests(unittest.TestCase):
    def test_gif_links(self):
        for url in (
            "https://tenor.com/view/hello-123",
            "https://media.tenor.com/foo.mp4",
            "https://giphy.com/gifs/hello",
            "https://i.giphy.com/media/hello",
            "https://klipy.com/gifs/hello",
            "https://media.klipy.com/hello",
            "https://gifconvert.vxtwitter.com/hello",
            "https://cdn.discordapp.com/attachments/1/2/picture.GIF?size=100",
            "https://example.com/picture%2Egif",
            "https://example.com/picture.gif#preview",
            "[GIF](https://example.com/picture.gif)",
            "<https://TENOR.COM/view/123>",
        ):
            with self.subTest(url=url):
                self.assertTrue(contains_gif(url, []))

    def test_non_gifs_and_spoofed_hosts(self):
        for text in (
            "ordinary text",
            "https://vxtwitter.com/user/status/123",
            "https://eviltenor.com/view/123",
            "https://tenor.com.evil.example/view/123",
            "https://tenor.com@evil.example/view/123",
            "https://notgifconvert.vxtwitter.com/view/123",
            "https://example.com/picture.gif.exe",
            "https://example.com/picture.gifv",
            "https://example.com/page?filename=picture.gif",
            "https://example.com/picture.png",
            "https://[invalid-host/view/123",
        ):
            with self.subTest(text=text):
                self.assertFalse(contains_gif(text, []))

    def test_attachment_metadata(self):
        for filename, mime, expected in (
            ("picture.GIF", None, True),
            ("picture.bin", "image/gif", True),
            ("picture.bin", "IMAGE/GIF; charset=binary", True),
            ("picture.gif.png", "image/png", False),
            (None, None, False),
        ):
            attachment = {"filename": filename, "content_type": mime}
            self.assertEqual(contains_gif("", [attachment]), expected)
            self.assertEqual(
                contains_gif("", [SimpleNamespace(**attachment)]), expected
            )


class GifFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        clock = patch("cogs.gif_filter.discord.utils.utcnow", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.settings = {
            1: {
                "channel_ids": [10],
                "mod_log_channel_id": 20,
                "dm_message": "No GIFs. (See Rule 5).",
                "max_message_age_seconds": 300,
            }
        }
        self.guild = SimpleNamespace(id=1)
        self.log_channel = SimpleNamespace(guild=self.guild, send=AsyncMock())
        self.channel = SimpleNamespace(
            id=10, guild=self.guild, fetch_message=AsyncMock()
        )
        self.bot = SimpleNamespace(
            get_channel=lambda channel_id: {10: self.channel, 20: self.log_channel}.get(
                channel_id
            ),
            fetch_channel=AsyncMock(return_value=self.log_channel),
        )
        self.cog = GifFilter(self.bot, self.settings)
        self.message = SimpleNamespace(
            id=discord.utils.time_snowflake(self.now),
            guild=self.guild,
            channel=self.channel,
            author=SimpleNamespace(id=200, bot=False, send=AsyncMock()),
            content="https://tenor.com/view/123",
            attachments=[],
            delete=AsyncMock(),
        )
        self.channel.fetch_message.return_value = self.message

    async def test_deletes_then_logs_and_dms_without_pings_or_gif_content(self):
        actions = []

        async def delete():
            actions.append("delete")

        async def report(*args, **kwargs):
            actions.append("report")

        async def dm(*args, **kwargs):
            actions.append("dm")

        self.message.delete.side_effect = delete
        self.log_channel.send.side_effect = report
        self.message.author.send.side_effect = dm
        await self.cog.on_message(self.message)
        self.assertEqual(actions, ["delete", "report", "dm"])
        report_text = self.log_channel.send.call_args.args[0]
        self.assertIn("<@200>", report_text)
        self.assertNotIn(self.message.content, report_text)
        for send in (self.log_channel.send, self.message.author.send):
            self.assertEqual(
                send.call_args.kwargs["allowed_mentions"].to_dict(),
                discord.AllowedMentions.none().to_dict(),
            )
        self.assertIn("reported", self.message.author.send.call_args.args[0])

    async def test_dms_bots_other_guilds_and_unwatched_channels_are_ignored(self):
        for guild, channel_id, bot in (
            (None, 10, False),
            (self.guild, 10, True),
            (SimpleNamespace(id=2), 10, False),
            (self.guild, 11, False),
        ):
            self.message.guild = guild
            self.message.channel.id = channel_id
            self.message.author.bot = bot
            await self.cog.on_message(self.message)
        self.message.delete.assert_not_awaited()

    async def test_failed_or_already_deleted_message_has_no_dm_or_report(self):
        for kind in (discord.Forbidden, discord.NotFound, discord.HTTPException):
            self.message.delete.side_effect = http_error(kind)
            await self.cog.on_message(self.message)
        self.log_channel.send.assert_not_awaited()
        self.message.author.send.assert_not_awaited()

    async def test_blocked_dm_does_not_prevent_report(self):
        self.message.author.send.side_effect = http_error()
        await self.cog.on_message(self.message)
        self.log_channel.send.assert_awaited_once()

    async def test_failed_report_does_not_claim_incident_was_reported(self):
        self.log_channel.send.side_effect = http_error()
        await self.cog.on_message(self.message)
        self.assertEqual(
            self.message.author.send.call_args.args[0], self.settings[1]["dm_message"]
        )

    async def test_log_channel_must_belong_to_same_guild(self):
        self.log_channel.guild = SimpleNamespace(id=2)
        await self.cog.on_message(self.message)
        self.log_channel.send.assert_not_awaited()
        self.message.author.send.assert_awaited_once()

    async def test_duplicate_and_concurrent_events_only_moderate_once(self):
        await asyncio.gather(
            self.cog.on_message(self.message), self.cog.on_message(self.message)
        )
        await self.cog.on_message(self.message)
        self.message.delete.assert_awaited_once()
        self.log_channel.send.assert_awaited_once()
        self.message.author.send.assert_awaited_once()

    async def test_uncached_message_edit_is_fetched_and_checked(self):
        payload = SimpleNamespace(
            guild_id=1,
            channel_id=10,
            message_id=self.message.id,
            data={
                "content": self.message.content,
                "edited_timestamp": self.now.isoformat(),
            },
        )
        await self.cog.on_raw_message_edit(payload)
        self.channel.fetch_message.assert_awaited_once_with(self.message.id)
        self.message.delete.assert_awaited_once()

    async def test_embed_only_and_unwatched_edits_do_not_fetch(self):
        for guild_id, channel_id, data in (
            (1, 10, {"embeds": [{}]}),
            (1, 11, {"content": self.message.content}),
            (2, 10, {"content": self.message.content}),
        ):
            await self.cog.on_raw_message_edit(
                SimpleNamespace(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=self.message.id,
                    data=data,
                )
            )
        self.channel.fetch_message.assert_not_awaited()

    async def test_edited_message_is_rechecked_before_deleting(self):
        payload = SimpleNamespace(
            guild_id=1,
            channel_id=10,
            message_id=self.message.id,
            data={
                "content": self.message.content,
                "edited_timestamp": self.now.isoformat(),
            },
        )
        self.message.content = "GIF has already been removed from this message"
        await self.cog.on_raw_message_edit(payload)
        self.message.delete.assert_not_awaited()

    async def test_missing_edited_message_is_harmless(self):
        self.channel.fetch_message.side_effect = http_error(discord.NotFound)
        await self.cog.on_raw_message_edit(
            SimpleNamespace(
                guild_id=1,
                channel_id=10,
                message_id=self.message.id,
                data={
                    "content": self.message.content,
                    "edited_timestamp": self.now.isoformat(),
                },
            )
        )
        self.message.delete.assert_not_awaited()

    async def test_deleted_id_cache_is_bounded(self):
        for index in range(1023):
            self.cog._deleted[index] = None
        self.cog._deleted[self.message.id] = None
        await self.cog.on_message(self.message)
        self.message.delete.assert_not_awaited()
        # A new deletion evicts the oldest retained IDs.
        self.message.id += 1
        await self.cog.on_message(self.message)
        self.assertEqual(len(self.cog._deleted), 1024)
        self.assertNotIn(0, self.cog._deleted)

    async def test_old_message_update_does_not_fetch_delete_log_or_dm(self):
        self.message.id = discord.utils.time_snowflake(self.now - timedelta(days=30))
        await self.cog.on_raw_message_edit(
            SimpleNamespace(
                guild_id=1,
                channel_id=10,
                message_id=self.message.id,
                data={
                    "content": self.message.content,
                    "edited_timestamp": self.now.isoformat(),
                },
            )
        )
        self.channel.fetch_message.assert_not_awaited()
        self.message.delete.assert_not_awaited()
        self.log_channel.send.assert_not_awaited()
        self.message.author.send.assert_not_awaited()

    async def test_metadata_update_with_gif_content_does_not_fetch_or_delete(self):
        for timestamp in (None, "missing"):
            data = {
                "content": self.message.content,
                "attachments": [{"filename": "old.gif"}],
                "embeds": [{}],
            }
            if timestamp is None:
                data["edited_timestamp"] = None
            await self.cog.on_raw_message_edit(
                SimpleNamespace(
                    guild_id=1,
                    channel_id=10,
                    message_id=self.message.id,
                    data=data,
                )
            )
        self.channel.fetch_message.assert_not_awaited()
        self.message.delete.assert_not_awaited()
        self.message.author.send.assert_not_awaited()

    async def test_old_message_create_event_is_also_ignored(self):
        self.message.id = discord.utils.time_snowflake(self.now - timedelta(days=30))
        await self.cog.on_message(self.message)
        self.message.delete.assert_not_awaited()
        self.log_channel.send.assert_not_awaited()
        self.message.author.send.assert_not_awaited()

    async def test_recency_uses_original_post_time_and_configured_limit(self):
        for age, expected in (
            (0, True),
            (299, True),
            (300, True),
            (301, False),
            (-1, False),
        ):
            with self.subTest(age=age):
                message_id = discord.utils.time_snowflake(
                    self.now - timedelta(seconds=age)
                )
                self.assertEqual(self.cog.is_recent(1, message_id), expected)
        self.settings[1]["max_message_age_seconds"] = 60
        self.assertFalse(
            self.cog.is_recent(
                1, discord.utils.time_snowflake(self.now - timedelta(seconds=61))
            )
        )
        del self.settings[1]["max_message_age_seconds"]
        self.assertTrue(
            self.cog.is_recent(
                1, discord.utils.time_snowflake(self.now - timedelta(seconds=299))
            )
        )

    async def test_message_age_is_rechecked_after_edit_fetch(self):
        self.message.id = discord.utils.time_snowflake(
            self.now - timedelta(seconds=299)
        )

        async def fetch(message_id):
            self.now += timedelta(seconds=2)
            return self.message

        self.channel.fetch_message.side_effect = fetch
        with patch(
            "cogs.gif_filter.discord.utils.utcnow", side_effect=lambda: self.now
        ):
            await self.cog.on_raw_message_edit(
                SimpleNamespace(
                    guild_id=1,
                    channel_id=10,
                    message_id=self.message.id,
                    data={
                        "content": self.message.content,
                        "edited_timestamp": self.now.isoformat(),
                    },
                )
            )
        self.channel.fetch_message.assert_awaited_once()
        self.message.delete.assert_not_awaited()
        self.message.author.send.assert_not_awaited()

    async def test_slow_log_delivery_does_not_send_stale_dm(self):
        async def report(*args, **kwargs):
            self.now += timedelta(minutes=6)

        self.log_channel.send.side_effect = report
        with patch(
            "cogs.gif_filter.discord.utils.utcnow", side_effect=lambda: self.now
        ):
            await self.cog.on_message(self.message)
        self.message.delete.assert_awaited_once()
        self.log_channel.send.assert_awaited_once()
        self.message.author.send.assert_not_awaited()

    async def test_dm_waits_for_successful_deletion(self):
        deleting = asyncio.Event()
        allow_delete = asyncio.Event()

        async def delete():
            deleting.set()
            await allow_delete.wait()

        self.message.delete.side_effect = delete
        task = asyncio.create_task(self.cog.on_message(self.message))
        try:
            await deleting.wait()
            self.message.author.send.assert_not_awaited()
            self.log_channel.send.assert_not_awaited()
        finally:
            allow_delete.set()
            await task
        self.message.author.send.assert_awaited_once()

    async def test_nonpositive_or_invalid_age_limits_are_rejected(self):
        for limit in (0, -1, True, "300", 300.0):
            with self.subTest(limit=limit):
                self.settings[1]["max_message_age_seconds"] = limit
                with self.assertRaises(ValueError):
                    GifFilter(self.bot, self.settings)

    async def test_invalid_config_is_rejected(self):
        with self.assertRaises(TypeError):
            GifFilter(self.bot, [])
        with self.assertRaises(ValueError):
            GifFilter(self.bot, {1: {"channel_ids": ["10"]}})


if __name__ == "__main__":
    unittest.main()
