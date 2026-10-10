import asyncio
import logging
import os
from datetime import time, timezone

import discord
import yaml
from discord.ext import commands, tasks

from lib.bot import TMWBot

_log = logging.getLogger(__name__)
SETTINGS_PATH = os.getenv("ALT_MOTD_SETTINGS_PATH") or "config/motd_settings.yml"
with open(SETTINGS_PATH, encoding="utf-8") as settings_file:
    motd_settings = yaml.safe_load(settings_file) or {}

CREATE_DELIVERIES = """
CREATE TABLE IF NOT EXISTS motd_deliveries (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    utc_date TEXT NOT NULL,
    message_id INTEGER,
    PRIMARY KEY (guild_id, channel_id, utc_date)
);
"""
GET_DELIVERY = """
SELECT message_id FROM motd_deliveries WHERE guild_id = ? AND channel_id = ? AND utc_date = ?;
"""
CLAIM_DELIVERY = """
INSERT INTO motd_deliveries (guild_id, channel_id, utc_date) VALUES (?, ?, ?);
"""
RECORD_DELIVERY = """
UPDATE motd_deliveries SET message_id = ? WHERE guild_id = ? AND channel_id = ? AND utc_date = ?;
"""


class Motd(commands.Cog):
    def __init__(self, bot: TMWBot, settings=None):
        self.bot = bot
        self.settings = motd_settings if settings is None else settings
        self._lock = asyncio.Lock()
        if not isinstance(self.settings, dict):
            raise TypeError("MOTD settings must be keyed by guild ID.")
        for guild_id, config in self.settings.items():
            if (
                type(guild_id) is not int
                or guild_id <= 0
                or not isinstance(config, dict)
                or type(config.get("channel_id")) is not int
                or config["channel_id"] <= 0
                or not isinstance(config.get("message"), str)
                or not 1 <= len(config["message"].strip()) <= 2000
            ):
                raise ValueError(
                    "Invalid MOTD guild configuration (message must be 1–2000 characters)."
                )

    async def cog_load(self):
        await self.bot.RUN(CREATE_DELIVERIES)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.settings and not self.daily_motd.is_running():
            self.daily_motd.start()

    def cog_unload(self):
        self.daily_motd.cancel()

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=timezone.utc))
    async def daily_motd(self):
        date = discord.utils.utcnow().date().isoformat()
        for guild_id, config in self.settings.items():
            try:
                await self.post_motd(guild_id, config, date)
            except Exception:  # noqa: BLE001 - isolate each scheduled guild delivery
                # One server's failure must not stop the daily task for all servers.
                _log.exception("Daily MOTD failed for guild %s.", guild_id)

    @daily_motd.before_loop
    async def before_daily_motd(self):
        await self.bot.wait_until_ready()

    async def post_motd(self, guild_id, config, date):
        key = (guild_id, config["channel_id"], date)
        async with self._lock:
            if await self.bot.GET_ONE(GET_DELIVERY, key) is not None:
                return
            channel = self.bot.get_channel(config["channel_id"])
            if channel is None:
                channel = await self.bot.fetch_channel(config["channel_id"])
            if getattr(getattr(channel, "guild", None), "id", None) != guild_id:
                _log.warning(
                    "MOTD channel does not belong to configured guild %s.", guild_id
                )
                return
            # Reserve before sending: a crash or ambiguous HTTP failure must not
            # post a duplicate. An unconfirmed attempt is retained for that day.
            await self.bot.RUN(CLAIM_DELIVERY, key)
            message = await channel.send(
                config["message"].strip(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.bot.RUN(RECORD_DELIVERY, (message.id, *key))


async def setup(bot: TMWBot):
    await bot.add_cog(Motd(bot))
