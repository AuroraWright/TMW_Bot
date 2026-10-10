import logging
import os
import re
from collections import OrderedDict
from urllib.parse import unquote, urlsplit

import discord
import yaml
from discord.ext import commands

from lib.bot import TMWBot

_log = logging.getLogger(__name__)
URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
GIF_HOSTS = ("tenor.com", "giphy.com", "klipy.com")
DEFAULT_MAX_MESSAGE_AGE_SECONDS = 300
SETTINGS_PATH = (
    os.getenv("ALT_GIF_FILTER_SETTINGS_PATH") or "config/gif_filter_settings.yml"
)
with open(SETTINGS_PATH, encoding="utf-8") as settings_file:
    gif_filter_settings = yaml.safe_load(settings_file) or {}


def contains_gif(content, attachments) -> bool:
    """Inspect metadata only: never download or follow user-supplied URLs."""
    for match in URL_PATTERN.finditer(content or ""):
        try:
            url = urlsplit(match.group().rstrip(".,!?;:]}\"'"))
            host = (url.hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if (
            any(host == domain or host.endswith("." + domain) for domain in GIF_HOSTS)
            or host == "gifconvert.vxtwitter.com"
            or unquote(url.path).lower().endswith(".gif")
        ):
            return True
    for attachment in attachments:
        if isinstance(attachment, dict):
            filename = attachment.get("filename") or ""
            content_type = attachment.get("content_type") or ""
        else:
            filename = attachment.filename or ""
            content_type = attachment.content_type or ""
        if (
            filename.lower().endswith(".gif")
            or content_type.lower().split(";", 1)[0].strip() == "image/gif"
        ):
            return True
    return False


class GifFilter(commands.Cog):
    def __init__(self, bot: TMWBot, settings=None):
        self.bot = bot
        self.settings = gif_filter_settings if settings is None else settings
        self._inflight = set()
        self._deleted = OrderedDict()
        if not isinstance(self.settings, dict):
            raise TypeError("GIF filter settings must be keyed by guild ID.")
        for guild_id, config in self.settings.items():
            if (
                type(guild_id) is not int
                or guild_id <= 0
                or not isinstance(config, dict)
                or not isinstance(config.get("channel_ids"), list)
                or not config["channel_ids"]
                or any(
                    type(channel_id) is not int or channel_id <= 0
                    for channel_id in config["channel_ids"]
                )
                or type(config.get("mod_log_channel_id")) is not int
                or config["mod_log_channel_id"] <= 0
                or not isinstance(config.get("dm_message"), str)
                or not 1 <= len(config["dm_message"].strip()) <= 1900
                or type(
                    config.get(
                        "max_message_age_seconds", DEFAULT_MAX_MESSAGE_AGE_SECONDS
                    )
                )
                is not int
                or config.get(
                    "max_message_age_seconds", DEFAULT_MAX_MESSAGE_AGE_SECONDS
                )
                <= 0
            ):
                raise ValueError("Invalid GIF filter guild configuration.")

    def watches(self, guild_id, channel_id):
        config = self.settings.get(guild_id)
        return config is not None and channel_id in config["channel_ids"]

    def is_recent(self, guild_id: int, message_id: int) -> bool:
        # The snowflake encodes the original post time; editing cannot reset it.
        age = (
            discord.utils.utcnow() - discord.utils.snowflake_time(message_id)
        ).total_seconds()
        limit = self.settings[guild_id].get(
            "max_message_age_seconds", DEFAULT_MAX_MESSAGE_AGE_SECONDS
        )
        return 0 <= age <= limit

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self.moderate(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if (
            not self.watches(payload.guild_id, payload.channel_id)
            or not self.is_recent(payload.guild_id, payload.message_id)
            or not payload.data.get("edited_timestamp")
        ):
            return
        # Metadata/preview updates may echo existing content and attachments.
        # Require an actual edit timestamp as well as GIF indicators.
        if not contains_gif(
            payload.data.get("content"), payload.data.get("attachments", [])
        ):
            return
        try:
            channel = self.bot.get_channel(payload.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(payload.channel_id)
            if getattr(getattr(channel, "guild", None), "id", None) != payload.guild_id:
                return
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            _log.warning(
                "Could not inspect edited GIF message %s in channel %s.",
                payload.message_id,
                payload.channel_id,
            )
            return
        await self.moderate(message, source="edit")

    async def moderate(self, message: discord.Message, *, source="message"):
        if (
            message.guild is None
            or message.author.bot
            or not self.watches(message.guild.id, message.channel.id)
            or not self.is_recent(message.guild.id, message.id)
            or not contains_gif(message.content, message.attachments)
            or message.id in self._inflight
            or message.id in self._deleted
        ):
            return
        self._inflight.add(message.id)
        try:
            try:
                await message.delete()
            except discord.NotFound:
                return
            except discord.HTTPException:
                _log.warning(
                    "Could not delete GIF message %s in channel %s; check Manage Messages permission.",
                    message.id,
                    message.channel.id,
                )
                return
            self._deleted[message.id] = None
            if len(self._deleted) > 1024:
                self._deleted.popitem(last=False)
            _log.info(
                "Deleted GIF message %s in guild %s channel %s (source=%s, posted_at=%s).",
                message.id,
                message.guild.id,
                message.channel.id,
                source,
                discord.utils.snowflake_time(message.id).isoformat(),
            )

            config = self.settings[message.guild.id]
            # Report independently of DM delivery, and never copy the GIF/content.
            reported = False
            try:
                channel = self.bot.get_channel(config["mod_log_channel_id"])
                if channel is None:
                    channel = await self.bot.fetch_channel(config["mod_log_channel_id"])
                if (
                    getattr(getattr(channel, "guild", None), "id", None)
                    == message.guild.id
                ):
                    await channel.send(
                        f"<@{message.author.id}> posted a GIF in <#{message.channel.id}>. "
                        f"Message {message.id} was automatically deleted.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    reported = True
                else:
                    _log.warning(
                        "GIF mod-log channel does not belong to configured guild %s.",
                        message.guild.id,
                    )
            except discord.HTTPException:
                _log.warning(
                    "Could not report GIF deletion in guild %s.", message.guild.id
                )

            dm_message = config["dm_message"]
            if reported:
                dm_message += " This incident has been reported to the moderators."
            # A rate-limited log send must not cause an out-of-date warning DM.
            if not self.is_recent(message.guild.id, message.id):
                return
            try:
                await message.author.send(
                    dm_message, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException:
                _log.info(
                    "Could not deliver GIF removal DM for message %s.", message.id
                )
        finally:
            self._inflight.discard(message.id)


async def setup(bot: TMWBot):
    await bot.add_cog(GifFilter(bot))
