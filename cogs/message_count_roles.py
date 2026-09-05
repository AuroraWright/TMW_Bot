"""Award destination roles from message activity in a configured source guild."""

import asyncio
import logging
import os
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
import yaml
from discord.ext import commands, tasks

from lib.bot import TMWBot

_log = logging.getLogger(__name__)

MESSAGE_COUNT_ROLE_SETTINGS_PATH = Path(
    os.getenv(
        "ALT_MESSAGE_COUNT_ROLE_SETTINGS_PATH",
        "config/message_count_roles.yml",
    )
)

CREATE_MESSAGE_COUNT_ROLE_COUNTS_TABLE = """
CREATE TABLE IF NOT EXISTS message_count_role_counts (
    rule_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    message_count INTEGER NOT NULL,
    PRIMARY KEY (rule_name, user_id)
);"""

CREATE_MESSAGE_COUNT_ROLE_SCAN_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS message_count_role_scan_state (
    rule_name TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL,
    PRIMARY KEY (rule_name, channel_id)
);"""

GET_MESSAGE_COUNT_ROWS = """
SELECT user_id, message_count
FROM message_count_role_counts
WHERE rule_name = ?;"""

UPSERT_MESSAGE_COUNT = """
INSERT INTO message_count_role_counts (rule_name, user_id, message_count)
VALUES (?, ?, ?)
ON CONFLICT(rule_name, user_id)
DO UPDATE SET message_count = excluded.message_count;"""

GET_MESSAGE_COUNT_SCAN_STATE = """
SELECT channel_id, last_message_id
FROM message_count_role_scan_state
WHERE rule_name = ?;"""

UPSERT_MESSAGE_COUNT_SCAN_STATE = """
INSERT INTO message_count_role_scan_state (rule_name, channel_id, last_message_id)
VALUES (?, ?, ?)
ON CONFLICT(rule_name, channel_id)
DO UPDATE SET last_message_id = excluded.last_message_id;"""


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{label} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise TypeError(f"{label} must be an integer.") from error
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{label} must be at least {minimum}.")
    return parsed


@dataclass(frozen=True)
class MessageCountRoleRule:
    name: str
    source_guild_id: int
    destination_guild_id: int
    destination_role_id: int
    message_threshold: int
    excluded_channel_ids: frozenset[int]

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        default_name: str | None = None,
    ) -> "MessageCountRoleRule":
        if not isinstance(data, Mapping):
            raise TypeError("Every message-count role rule must be a mapping.")

        name = data.get("name", default_name)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Every message-count role rule needs a non-empty name.")

        try:
            source_guild_id = _integer(data["source_guild_id"], "source_guild_id")
            destination_guild_id = _integer(
                data["destination_guild_id"], "destination_guild_id"
            )
            destination_role_id = _integer(
                data["destination_role_id"], "destination_role_id"
            )
            message_threshold = _integer(
                data["message_threshold"],
                "message_threshold",
                minimum=1,
            )
        except KeyError as error:
            raise ValueError(
                f"Message-count role rule {name!r} is missing {error.args[0]}."
            ) from error

        configured_exclusions = data.get("excluded_channel_ids", [])
        if not isinstance(configured_exclusions, list):
            raise TypeError(f"excluded_channel_ids for rule {name!r} must be a list.")
        excluded_channel_ids = frozenset(
            _integer(channel_id, "excluded_channel_ids entry")
            for channel_id in configured_exclusions
        )

        if source_guild_id == destination_guild_id:
            raise ValueError(
                f"Message-count role rule {name!r} needs different source and destination guilds."
            )

        return cls(
            name=name.strip(),
            source_guild_id=source_guild_id,
            destination_guild_id=destination_guild_id,
            destination_role_id=destination_role_id,
            message_threshold=message_threshold,
            excluded_channel_ids=excluded_channel_ids,
        )


@dataclass(frozen=True)
class MessageCountRoleSettings:
    enabled: bool = True
    retroactive_scan: bool = True
    monitor_new_messages: bool = True
    reconcile_interval_minutes: float = 30.0
    history_batch_size: int = 100
    history_batch_delay_seconds: float = 0.25
    rules: tuple[MessageCountRoleRule, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any] | None,
    ) -> "MessageCountRoleSettings":
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise TypeError("Message-count role settings must be a YAML mapping.")

        boolean_fields = {
            "enabled",
            "retroactive_scan",
            "monitor_new_messages",
        }
        values: dict[str, Any] = {
            field: data.get(field, getattr(cls(), field))
            for field in (
                "enabled",
                "retroactive_scan",
                "monitor_new_messages",
            )
        }
        for field in boolean_fields:
            if not isinstance(values[field], bool):
                raise TypeError(f"message_count_roles.{field} must be true or false.")

        try:
            reconcile_interval_minutes = float(
                data.get(
                    "reconcile_interval_minutes",
                    cls().reconcile_interval_minutes,
                )
            )
            history_batch_delay_seconds = float(
                data.get(
                    "history_batch_delay_seconds",
                    cls().history_batch_delay_seconds,
                )
            )
        except (TypeError, ValueError) as error:
            raise TypeError(
                "reconcile_interval_minutes and history_batch_delay_seconds must be numbers."
            ) from error
        if reconcile_interval_minutes <= 0:
            raise ValueError("reconcile_interval_minutes must be greater than zero.")
        if history_batch_delay_seconds < 0:
            raise ValueError("history_batch_delay_seconds cannot be negative.")

        history_batch_size = _integer(
            data.get("history_batch_size", cls().history_batch_size),
            "history_batch_size",
            minimum=1,
        )

        configured_rules = data.get("rules", [])
        if isinstance(configured_rules, Mapping):
            rule_items = []
            for rule_name, rule_data in configured_rules.items():
                if not isinstance(rule_data, Mapping):
                    raise TypeError(
                        f"Message-count role rule {rule_name!r} must be a mapping."
                    )
                normalized_rule = dict(rule_data)
                normalized_rule.setdefault("name", rule_name)
                rule_items.append(normalized_rule)
        elif isinstance(configured_rules, list):
            rule_items = configured_rules
        else:
            raise TypeError("rules must be a list or mapping of message-count rules.")

        rules = tuple(MessageCountRoleRule.from_mapping(rule) for rule in rule_items)
        names = [rule.name for rule in rules]
        if len(names) != len(set(names)):
            raise ValueError("Message-count role rule names must be unique.")

        return cls(
            enabled=values["enabled"],
            retroactive_scan=values["retroactive_scan"],
            monitor_new_messages=values["monitor_new_messages"],
            reconcile_interval_minutes=reconcile_interval_minutes,
            history_batch_size=history_batch_size,
            history_batch_delay_seconds=history_batch_delay_seconds,
            rules=rules,
        )


def load_message_count_role_settings(path: Path) -> MessageCountRoleSettings:
    with path.open("r", encoding="utf-8") as settings_file:
        data = yaml.safe_load(settings_file)
    return MessageCountRoleSettings.from_mapping(data)


message_count_role_settings = load_message_count_role_settings(
    MESSAGE_COUNT_ROLE_SETTINGS_PATH
)


class MessageCountRoles(commands.Cog):
    """Count source-guild messages and grant roles in configured destinations."""

    def __init__(
        self,
        bot: TMWBot,
        settings: MessageCountRoleSettings = message_count_role_settings,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self._counts: dict[str, dict[int, int]] = {
            rule.name: {} for rule in settings.rules
        }
        self._scan_cursors: dict[str, dict[int, int]] = defaultdict(dict)
        self._dirty_counts: set[tuple[str, int]] = set()
        self._live_message_ids: dict[tuple[str, int], set[int]] = defaultdict(set)
        self._state_lock = asyncio.Lock()
        self._scan_lock = asyncio.Lock()
        self._award_lock = asyncio.Lock()
        self._awarded_users: set[tuple[str, int]] = set()
        self._messages_since_flush = 0

    async def cog_load(self) -> None:
        await self.bot.RUN(CREATE_MESSAGE_COUNT_ROLE_COUNTS_TABLE)
        await self.bot.RUN(CREATE_MESSAGE_COUNT_ROLE_SCAN_STATE_TABLE)
        for rule in self.settings.rules:
            count_rows = await self.bot.GET(GET_MESSAGE_COUNT_ROWS, (rule.name,))
            self._counts[rule.name] = {
                int(user_id): int(message_count)
                for user_id, message_count in (count_rows or [])
            }
            cursor_rows = await self.bot.GET(
                GET_MESSAGE_COUNT_SCAN_STATE,
                (rule.name,),
            )
            self._scan_cursors[rule.name] = {
                int(channel_id): int(last_message_id)
                for channel_id, last_message_id in (cursor_rows or [])
            }

        self.periodic_reconcile.change_interval(
            minutes=self.settings.reconcile_interval_minutes
        )

    async def cog_unload(self) -> None:
        self.periodic_reconcile.cancel()
        await self._flush_dirty_counts()

    @staticmethod
    def _is_forum_parent(channel: Any) -> bool:
        forum_channel = getattr(discord, "ForumChannel", None)
        if forum_channel is not None and isinstance(channel, forum_channel):
            return True
        channel_type = getattr(getattr(channel, "type", None), "value", None)
        return channel_type == 15  # Discord's forum channel type.

    @staticmethod
    def _is_excluded_channel(
        channel: Any,
        excluded_channel_ids: frozenset[int],
    ) -> bool:
        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None)
        return channel_id in excluded_channel_ids or parent_id in excluded_channel_ids

    async def _message_channels(
        self,
        rule: MessageCountRoleRule,
        source_guild: discord.Guild,
    ) -> list[Any]:
        channels: list[Any] = []
        seen_channel_ids: set[int] = set()

        def add_channel(channel: Any) -> None:
            channel_id = getattr(channel, "id", None)
            if (
                channel_id is None
                or channel_id in seen_channel_ids
                or self._is_excluded_channel(channel, rule.excluded_channel_ids)
                or not callable(getattr(channel, "history", None))
            ):
                return
            seen_channel_ids.add(channel_id)
            channels.append(channel)

        archive_parents: list[Any] = []
        for channel in getattr(source_guild, "channels", ()):
            if self._is_forum_parent(channel):
                archive_parents.append(channel)
            else:
                add_channel(channel)
                if callable(getattr(channel, "archived_threads", None)):
                    archive_parents.append(channel)

        for thread in getattr(source_guild, "threads", ()):
            add_channel(thread)
            parent = getattr(thread, "parent", None)
            if parent is not None and parent not in archive_parents:
                archive_parents.append(parent)

        for parent in archive_parents:
            if self._is_excluded_channel(parent, rule.excluded_channel_ids):
                continue
            archived_threads = getattr(parent, "archived_threads", None)
            if not callable(archived_threads):
                continue
            archive_modes = (False,) if self._is_forum_parent(parent) else (False, True)
            for private in archive_modes:
                kwargs: dict[str, Any] = {"private": private, "limit": None}
                if private:
                    kwargs["joined"] = True
                if self._is_forum_parent(parent):
                    kwargs = {"limit": None}
                try:
                    async for thread in archived_threads(**kwargs):
                        add_channel(thread)
                except TypeError as error:
                    _log.warning(
                        "Could not enumerate archived threads for channel %s in guild %s: %s",
                        getattr(parent, "id", "unknown"),
                        source_guild.id,
                        error,
                    )
                except (discord.Forbidden, discord.HTTPException) as error:
                    _log.warning(
                        "Could not enumerate %s archived threads for channel %s in guild %s: %s",
                        "private" if private else "public",
                        getattr(parent, "id", "unknown"),
                        source_guild.id,
                        error,
                    )

        return channels

    def _message_is_countable(
        self,
        rule: MessageCountRoleRule,
        message: discord.Message,
    ) -> bool:
        source_guild = getattr(message, "guild", None)
        if source_guild is None or source_guild.id != rule.source_guild_id:
            return False
        channel = getattr(message, "channel", None)
        if channel is None or self._is_excluded_channel(
            channel, rule.excluded_channel_ids
        ):
            return False
        author = getattr(message, "author", None)
        return author is not None and not getattr(author, "bot", False)

    async def _record_message(
        self,
        rule: MessageCountRoleRule,
        message: discord.Message,
        *,
        from_history: bool,
    ) -> bool:
        if not self._message_is_countable(rule, message):
            return False

        channel_id = message.channel.id
        message_id = message.id
        user_id = message.author.id
        key = (rule.name, channel_id)
        crossed_threshold = False
        async with self._state_lock:
            live_message_ids = self._live_message_ids[key]
            if from_history:
                if message_id in live_message_ids:
                    live_message_ids.discard(message_id)
                    return False
            elif message_id in live_message_ids:
                return False
            else:
                live_message_ids.add(message_id)

            current_count = self._counts.setdefault(rule.name, {}).get(user_id, 0)
            if current_count >= rule.message_threshold:
                live_message_ids.discard(message_id)
                return False
            new_count = min(current_count + 1, rule.message_threshold)
            self._counts[rule.name][user_id] = new_count
            self._dirty_counts.add((rule.name, user_id))
            self._messages_since_flush += 1
            crossed_threshold = new_count == rule.message_threshold

        if crossed_threshold:
            await self._award_cached_member(rule, user_id)
        return True

    async def _flush_dirty_counts(self) -> None:
        async with self._state_lock:
            snapshots = [
                (rule_name, user_id, self._counts[rule_name][user_id])
                for rule_name, user_id in self._dirty_counts
            ]
            if snapshots:
                self._messages_since_flush = 0
        if not snapshots:
            return

        await self.bot.RUN_MANY(UPSERT_MESSAGE_COUNT, snapshots)
        async with self._state_lock:
            for rule_name, user_id, message_count in snapshots:
                if self._counts.get(rule_name, {}).get(user_id) == message_count:
                    self._dirty_counts.discard((rule_name, user_id))

    async def _save_scan_cursor(
        self,
        rule: MessageCountRoleRule,
        channel_id: int,
        last_message_id: int,
    ) -> None:
        await self.bot.RUN(
            UPSERT_MESSAGE_COUNT_SCAN_STATE,
            (rule.name, channel_id, last_message_id),
        )

    def _get_destination_guild(
        self,
        rule: MessageCountRoleRule,
    ) -> discord.Guild | None:
        destination_guild = self.bot.get_guild(rule.destination_guild_id)
        if destination_guild is None:
            _log.warning(
                "Message-count role destination guild %s is unavailable.",
                rule.destination_guild_id,
            )
        return destination_guild

    async def _award_member(
        self,
        rule: MessageCountRoleRule,
        member: discord.Member,
    ) -> bool:
        if getattr(member, "bot", False):
            return False
        destination_guild = self._get_destination_guild(rule)
        if destination_guild is None:
            return False
        role = destination_guild.get_role(rule.destination_role_id)
        if role is None:
            _log.warning(
                "Message-count destination role %s was not found in guild %s.",
                rule.destination_role_id,
                destination_guild.id,
            )
            return False
        if getattr(role, "managed", False) or not role.is_assignable():
            _log.error(
                "Message-count destination role %s in guild %s is not assignable by the bot.",
                role.id,
                destination_guild.id,
            )
            return False
        award_key = (rule.name, member.id)
        if award_key in self._awarded_users:
            return True
        if any(existing_role.id == role.id for existing_role in member.roles):
            self._awarded_users.add(award_key)
            return True

        try:
            await member.add_roles(
                role,
                reason="Reached the configured message-count role threshold",
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            _log.error(
                "Failed to award message-count role %s to member %s in guild %s: %s",
                role.id,
                member.id,
                destination_guild.id,
                error,
            )
            return False
        self._awarded_users.add(award_key)
        _log.info(
            "Awarded message-count role %s to member %s in guild %s.",
            role.id,
            member.id,
            destination_guild.id,
        )
        return True

    async def _award_member_safely(
        self,
        rule: MessageCountRoleRule,
        member: discord.Member,
    ) -> bool:
        async with self._award_lock:
            return await self._award_member(rule, member)

    async def _award_cached_member(
        self,
        rule: MessageCountRoleRule,
        user_id: int,
    ) -> None:
        destination_guild = self._get_destination_guild(rule)
        if destination_guild is None:
            return
        member = destination_guild.get_member(user_id)
        if member is not None:
            await self._award_member_safely(rule, member)

    async def _award_qualified_members(
        self,
        rule: MessageCountRoleRule,
        destination_guild: discord.Guild,
    ) -> None:
        qualifying_user_ids = {
            user_id
            for user_id, message_count in self._counts.get(rule.name, {}).items()
            if message_count >= rule.message_threshold
        }
        awarded_count = 0
        for member in list(destination_guild.members):
            if member.id not in qualifying_user_ids:
                continue
            if await self._award_member_safely(rule, member):
                awarded_count += 1
        _log.info(
            "Message-count role backfill checked %s qualifying members in guild %s.",
            awarded_count,
            destination_guild.id,
        )

    async def _scan_channel(
        self,
        rule: MessageCountRoleRule,
        channel: Any,
    ) -> None:
        channel_id = channel.id
        cursor = self._scan_cursors.get(rule.name, {}).get(channel_id, 0)
        latest_message_id = cursor
        messages_since_flush = 0
        history_kwargs: dict[str, Any] = {
            "limit": None,
            "oldest_first": True,
        }
        if cursor:
            history_kwargs["after"] = discord.Object(id=cursor)

        try:
            async for message in channel.history(**history_kwargs):
                if message.id <= latest_message_id:
                    continue
                latest_message_id = message.id
                self._scan_cursors.setdefault(rule.name, {})[channel_id] = (
                    latest_message_id
                )
                await self._record_message(rule, message, from_history=True)
                messages_since_flush += 1
                if messages_since_flush < self.settings.history_batch_size:
                    continue
                await self._flush_dirty_counts()
                await self._save_scan_cursor(rule, channel_id, latest_message_id)
                messages_since_flush = 0
                if self.settings.history_batch_delay_seconds:
                    await asyncio.sleep(self.settings.history_batch_delay_seconds)
        except (discord.Forbidden, discord.HTTPException) as error:
            _log.warning(
                "Could not scan message history for channel %s in rule %s; progress through message %s was saved: %s",
                channel_id,
                rule.name,
                latest_message_id,
                error,
            )
        finally:
            await self._flush_dirty_counts()
            if latest_message_id != cursor:
                await self._save_scan_cursor(rule, channel_id, latest_message_id)

    async def _reconcile_rule(self, rule: MessageCountRoleRule) -> None:
        source_guild = self.bot.get_guild(rule.source_guild_id)
        destination_guild = self._get_destination_guild(rule)
        if source_guild is None:
            _log.warning(
                "Message-count role source guild %s is unavailable for rule %s.",
                rule.source_guild_id,
                rule.name,
            )
            return
        if destination_guild is None:
            return

        if self.settings.retroactive_scan:
            channels = await self._message_channels(rule, source_guild)
            _log.info(
                "Scanning %s message channels for message-count role rule %s.",
                len(channels),
                rule.name,
            )
            for channel in channels:
                await self._scan_channel(rule, channel)
            await self._flush_dirty_counts()

        await self._award_qualified_members(rule, destination_guild)

    async def reconcile_all(self) -> None:
        if not self.settings.enabled:
            return
        async with self._scan_lock:
            for rule in self.settings.rules:
                try:
                    await self._reconcile_rule(rule)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - keep other rules running
                    _log.exception(
                        "Unexpected failure while processing message-count role rule %s.",
                        rule.name,
                    )

    @tasks.loop(minutes=30)
    async def periodic_reconcile(self) -> None:
        await self.reconcile_all()

    @periodic_reconcile.before_loop
    async def before_periodic_reconcile(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if (
            self.settings.enabled
            and self.settings.rules
            and not self.periodic_reconcile.is_running()
        ):
            self.periodic_reconcile.start()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self.settings.enabled or not self.settings.monitor_new_messages:
            return
        for rule in self.settings.rules:
            if (
                getattr(getattr(message, "guild", None), "id", None)
                != rule.source_guild_id
            ):
                continue
            await self._record_message(rule, message, from_history=False)

        async with self._state_lock:
            should_flush = (
                self._messages_since_flush >= self.settings.history_batch_size
            )
        if should_flush:
            await self._flush_dirty_counts()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self.settings.enabled:
            return
        for rule in self.settings.rules:
            if member.guild.id != rule.destination_guild_id:
                continue
            if (
                self._counts.get(rule.name, {}).get(member.id, 0)
                >= rule.message_threshold
            ):
                await self._award_member_safely(rule, member)

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        if not self.settings.enabled or before.roles == after.roles:
            return
        for rule in self.settings.rules:
            if after.guild.id != rule.destination_guild_id:
                continue
            if not any(
                existing_role.id == rule.destination_role_id
                for existing_role in after.roles
            ):
                self._awarded_users.discard((rule.name, after.id))
            if (
                self._counts.get(rule.name, {}).get(after.id, 0)
                >= rule.message_threshold
            ):
                await self._award_member_safely(rule, after)


async def setup(bot: TMWBot) -> None:
    await bot.add_cog(MessageCountRoles(bot))
