import asyncio
import sqlite3
import unittest
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.motd import CREATE_DELIVERIES, Motd


class MotdTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute(CREATE_DELIVERIES)

        async def run(query, params=()):
            self.db.execute(query, params)
            self.db.commit()

        async def get_one(query, params=()):
            return self.db.execute(query, params).fetchone()

        self.settings = {1: {"channel_id": 10, "message": "Today's message: @everyone"}}
        self.channel = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            send=AsyncMock(return_value=SimpleNamespace(id=100)),
        )
        self.bot = SimpleNamespace(
            RUN=AsyncMock(side_effect=run),
            GET_ONE=AsyncMock(side_effect=get_one),
            get_channel=lambda channel_id: self.channel,
            fetch_channel=AsyncMock(return_value=self.channel),
            wait_until_ready=AsyncMock(),
        )
        self.cog = Motd(self.bot, self.settings)

    async def post(self, date="2026-10-10"):
        await self.cog.post_motd(1, self.settings[1], date)

    async def test_posts_configured_message_without_mentions(self):
        await self.post()
        self.channel.send.assert_awaited_once()
        self.assertEqual(
            self.channel.send.call_args.args, (self.settings[1]["message"],)
        )
        self.assertEqual(
            self.channel.send.call_args.kwargs["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )
        self.assertEqual(
            self.db.execute("SELECT message_id FROM motd_deliveries").fetchone(), (100,)
        )

    async def test_parallel_calls_and_reload_do_not_duplicate(self):
        await asyncio.gather(self.post(), self.post())
        reloaded = Motd(self.bot, self.settings)
        await reloaded.post_motd(1, self.settings[1], "2026-10-10")
        self.channel.send.assert_awaited_once()
        await self.post("2026-10-11")
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_claim_is_persisted_before_send(self):
        async def send(*args, **kwargs):
            self.assertEqual(
                self.db.execute("SELECT message_id FROM motd_deliveries").fetchone(),
                (None,),
            )
            return SimpleNamespace(id=100)

        self.channel.send.side_effect = send
        await self.post()

    async def test_unconfirmed_delivery_does_not_retry_and_risk_duplicate(self):
        self.channel.send.side_effect = discord.HTTPException(
            SimpleNamespace(status=500, reason="Server Error"), "Failure"
        )
        with self.assertRaises(discord.HTTPException):
            await self.post()
        await self.post()
        self.channel.send.assert_awaited_once()
        self.assertEqual(
            self.db.execute("SELECT message_id FROM motd_deliveries").fetchone(),
            (None,),
        )

    async def test_wrong_guild_channel_is_never_used(self):
        self.channel.guild.id = 2
        await self.post()
        self.channel.send.assert_not_awaited()
        self.assertIsNone(
            self.db.execute("SELECT message_id FROM motd_deliveries").fetchone()
        )

    async def test_uncached_channel_is_fetched(self):
        self.bot.get_channel = lambda channel_id: None
        await self.post()
        self.bot.fetch_channel.assert_awaited_once_with(10)
        self.channel.send.assert_awaited_once()

    async def test_one_guild_failure_does_not_stop_others(self):
        self.cog.settings = {
            1: self.settings[1],
            2: {"channel_id": 20, "message": "Another server"},
        }
        self.cog.post_motd = AsyncMock(
            side_effect=[RuntimeError("database unavailable"), None]
        )
        with self.assertLogs("cogs.motd", level="ERROR"):
            await self.cog.daily_motd.coro(self.cog)
        self.assertEqual(self.cog.post_motd.await_count, 2)

    async def test_schedule_waits_for_ready_and_stops_on_unload(self):
        schedule = self.cog.daily_motd.time
        self.assertEqual(
            (schedule[0].hour, schedule[0].minute, schedule[0].tzinfo),
            (0, 0, timezone.utc),
        )
        await self.cog.before_daily_motd()
        self.bot.wait_until_ready.assert_awaited_once()
        with patch.object(self.cog.daily_motd, "start") as start:
            await self.cog.cog_load()
            start.assert_not_called()
            with patch.object(
                self.cog.daily_motd, "is_running", side_effect=[False, True]
            ):
                await self.cog.on_ready()
                await self.cog.on_ready()
            start.assert_called_once()
            self.channel.send.assert_not_awaited()
        with patch.object(self.cog.daily_motd, "cancel") as cancel:
            self.cog.cog_unload()
            cancel.assert_called_once()

    async def test_empty_config_does_not_start_task(self):
        empty = Motd(self.bot, {})
        with patch.object(empty.daily_motd, "start") as start:
            await empty.cog_load()
            await empty.on_ready()
            start.assert_not_called()

    async def test_invalid_config_is_rejected(self):
        with self.assertRaises(TypeError):
            Motd(self.bot, [])
        for config in (
            {"channel_id": 10, "message": ""},
            {"channel_id": 10, "message": "a" * 2001},
            {"channel_id": "10", "message": "text"},
        ):
            with self.assertRaises(ValueError):
                Motd(self.bot, {1: config})


if __name__ == "__main__":
    unittest.main()
