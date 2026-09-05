"""Award destination roles from message activity in a configured source guild."""

import asyncio
import logging
import os
import time
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

GET_MESSAGE_COUNT_FOR_USER = """
SELECT message_count
FROM message_count_role_counts
WHERE rule_name = ? AND user_id = ?;"""

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
    auto_receive_interval_minutes: float = 15.0
    history_batch_size: int = 100
    history_batch_delay_seconds: float = 0.25
    history_scan_worker_count: int = 2
    award_worker_count: int = 2
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
            auto_receive_interval_minutes = float(
                data.get(
                    "auto_receive_interval_minutes",
                    cls().auto_receive_interval_minutes,
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
                "reconcile_interval_minutes, auto_receive_interval_minutes, and "
                "history_batch_delay_seconds must be numbers."
            ) from error
        if reconcile_interval_minutes <= 0:
            raise ValueError("reconcile_interval_minutes must be greater than zero.")
        if auto_receive_interval_minutes <= 0:
            raise ValueError("auto_receive_interval_minutes must be greater than zero.")
        if history_batch_delay_seconds < 0:
            raise ValueError("history_batch_delay_seconds cannot be negative.")

        history_batch_size = _integer(
            data.get("history_batch_size", cls().history_batch_size),
            "history_batch_size",
            minimum=1,
        )
        history_scan_worker_count = _integer(
            data.get("history_scan_worker_count", cls().history_scan_worker_count),
            "history_scan_worker_count",
            minimum=1,
        )
        if history_scan_worker_count > 4:
            raise ValueError("history_scan_worker_count cannot be greater than 4.")
        award_worker_count = _integer(
            data.get("award_worker_count", cls().award_worker_count),
            "award_worker_count",
            minimum=1,
        )
        if award_worker_count > 8:
            raise ValueError("award_worker_count cannot be greater than 8.")

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
            auto_receive_interval_minutes=auto_receive_interval_minutes,
            history_batch_size=history_batch_size,
            history_batch_delay_seconds=history_batch_delay_seconds,
            history_scan_worker_count=history_scan_worker_count,
            award_worker_count=award_worker_count,
            rules=rules,
        )


def load_message_count_role_settings(path: Path) -> MessageCountRoleSettings:
    with path.open("r", encoding="utf-8") as settings_file:
        data = yaml.safe_load(settings_file)
    return MessageCountRoleSettings.from_mapping(data)


message_count_role_settings = load_message_count_role_settings(
    MESSAGE_COUNT_ROLE_SETTINGS_PATH
)


@dataclass
class _AwardRequest:
    """A deduplicated role award waiting in the priority queue."""

    key: tuple[str, int]
    rule: MessageCountRoleRule
    member: discord.Member
    source: str
    priority: int
    sequence: int
    generation: int
    future: asyncio.Future[bool]
    enqueued_at: float
    started: bool = False


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
        self._inline_award_lock = asyncio.Lock()
        self._award_queue: asyncio.PriorityQueue[
            tuple[int, int, int, _AwardRequest]
        ] = asyncio.PriorityQueue()
        self._award_workers: list[asyncio.Task[None]] = []
        self._pending_awards: dict[tuple[str, int], _AwardRequest] = {}
        self._award_sequence = 0
        self._destination_member_cache: dict[tuple[int, int], discord.Member] = {}
        self._awarded_users: set[tuple[str, int]] = set()
        self._messages_since_flush = 0

    _AWARD_PRIORITY_LIVE = 0
    _AWARD_PRIORITY_BACKFILL = 10

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
        self.auto_receive_reconcile.change_interval(
            minutes=self.settings.auto_receive_interval_minutes
        )

    async def cog_unload(self) -> None:
        self.periodic_reconcile.cancel()
        self.auto_receive_reconcile.cancel()
        await self._stop_award_workers()
        await self._flush_dirty_counts()

    @staticmethod
    def _is_forum_parent(channel: Any) -> bool:
        forum_channel = getattr(discord, "ForumChannel", None)
        if forum_channel is not None and isinstance(channel, forum_channel):
            return True
        channel_type = getattr(getattr(channel, "type", None), "value", None)
        return channel_type == 15  # Discord's forum channel type.

    @staticmethod
    def _is_thread_channel(channel: Any) -> bool:
        thread_type = getattr(discord, "Thread", None)
        if thread_type is not None and isinstance(channel, thread_type):
            return True
        channel_type = getattr(getattr(channel, "type", None), "value", None)
        return channel_type in {10, 11, 12}

    @classmethod
    def _is_excluded_channel(
        cls,
        channel: Any,
        excluded_channel_ids: frozenset[int],
    ) -> bool:
        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None)
        return channel_id in excluded_channel_ids or (
            cls._is_thread_channel(channel) and parent_id in excluded_channel_ids
        )

    @staticmethod
    def _can_manage_threads(
        source_guild: discord.Guild,
        channel: Any,
    ) -> bool | None:
        """Return the bot's channel-level Manage Threads permission when known."""
        me = getattr(source_guild, "me", None)
        permissions_for = getattr(channel, "permissions_for", None)
        if me is None or not callable(permissions_for):
            return None
        try:
            permissions = permissions_for(me)
        except Exception:  # noqa: BLE001 - permission lookup is best effort
            return None
        return bool(getattr(permissions, "manage_threads", False))

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
        archive_parent_ids: set[int] = set()

        def add_archive_parent(channel: Any) -> None:
            channel_id = getattr(channel, "id", None)
            if channel_id is None or channel_id in archive_parent_ids:
                return
            archive_parent_ids.add(channel_id)
            archive_parents.append(channel)

        source_channels = list(getattr(source_guild, "channels", ()))
        fetch_channels = getattr(source_guild, "fetch_channels", None)
        if callable(fetch_channels):
            try:
                source_channels.extend(await fetch_channels())
            except (
                discord.ClientException,
                discord.Forbidden,
                discord.HTTPException,
            ) as error:
                _log.warning(
                    "Could not refresh channels for source guild %s; using the local channel cache: %s",
                    source_guild.id,
                    error,
                )

        for channel in source_channels:
            if self._is_forum_parent(channel):
                add_archive_parent(channel)
            else:
                add_channel(channel)
                if callable(getattr(channel, "archived_threads", None)):
                    add_archive_parent(channel)

        source_threads = list(getattr(source_guild, "threads", ()))
        active_threads = getattr(source_guild, "active_threads", None)
        if callable(active_threads):
            try:
                source_threads.extend(await active_threads())
            except (
                discord.ClientException,
                discord.Forbidden,
                discord.HTTPException,
            ) as error:
                _log.warning(
                    "Could not refresh active threads for source guild %s; using the local thread cache: %s",
                    source_guild.id,
                    error,
                )

        for thread in source_threads:
            add_channel(thread)
            parent = getattr(thread, "parent", None)
            if parent is not None:
                add_archive_parent(parent)

        for parent in archive_parents:
            if self._is_excluded_channel(parent, rule.excluded_channel_ids):
                continue
            archived_threads = getattr(parent, "archived_threads", None)
            if not callable(archived_threads):
                continue
            if self._is_forum_parent(parent):
                archive_requests = ({"limit": None},)
            else:
                can_manage_threads = self._can_manage_threads(source_guild, parent)
                # A bot with Manage Threads can enumerate every private
                # archived thread.  Without it, Discord only permits the
                # joined-private endpoint.  If the permission cache is
                # unavailable, try the complete endpoint and fall back on 403.
                archive_requests = (
                    {"private": False, "limit": None},
                    {
                        "private": True,
                        "joined": can_manage_threads is False,
                        "limit": None,
                    },
                )
            for kwargs in archive_requests:
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
                except discord.Forbidden as error:
                    if kwargs.get("private") and not kwargs.get("joined"):
                        _log.warning(
                            "Could not enumerate all private archived threads for channel %s in guild %s; retrying joined private threads only: %s",
                            getattr(parent, "id", "unknown"),
                            source_guild.id,
                            error,
                        )
                        try:
                            async for thread in archived_threads(
                                private=True,
                                joined=True,
                                limit=None,
                            ):
                                add_channel(thread)
                        except (TypeError, discord.Forbidden, discord.HTTPException) as fallback_error:
                            _log.warning(
                                "Could not enumerate joined private archived threads for channel %s in guild %s: %s",
                                getattr(parent, "id", "unknown"),
                                source_guild.id,
                                fallback_error,
                            )
                        continue
                    _log.warning(
                        "Could not enumerate %s archived threads for channel %s in guild %s: %s",
                        "private" if kwargs.get("private") else "public",
                        getattr(parent, "id", "unknown"),
                        source_guild.id,
                        error,
                    )
                except discord.HTTPException as error:
                    _log.warning(
                        "Could not enumerate %s archived threads for channel %s in guild %s: %s",
                        "private" if kwargs.get("private") else "public",
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
            await self._award_cached_member(
                rule,
                user_id,
                priority=(
                    self._AWARD_PRIORITY_BACKFILL
                    if from_history
                    else self._AWARD_PRIORITY_LIVE
                ),
                source="history-threshold" if from_history else "live-threshold",
                wait=not from_history,
                fetch_if_missing=not from_history,
            )
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

    def _award_workers_are_running(self) -> bool:
        """Return whether at least one award worker can service the queue."""
        active_workers = [worker for worker in self._award_workers if not worker.done()]
        self._award_workers = active_workers
        return bool(active_workers)

    def _start_award_workers(self) -> None:
        """Start the bounded award workers after the bot has connected."""
        if not self.settings.enabled or not self.settings.rules:
            return
        if self._award_workers_are_running():
            return
        self._award_workers = [
            asyncio.create_task(
                self._award_worker(index),
                name=f"message-count-role-award-{index}",
            )
            for index in range(self.settings.award_worker_count)
        ]
        _log.info(
            "Started %s message-count role award workers (live awards have priority over backfill).",
            len(self._award_workers),
        )

    async def _stop_award_workers(self) -> None:
        """Stop workers and resolve queued requests during cog shutdown/reload."""
        workers = self._award_workers
        self._award_workers = []
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

        for request in self._pending_awards.values():
            if not request.future.done():
                request.future.set_result(False)
        self._pending_awards.clear()

        while True:
            try:
                self._award_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._award_queue.task_done()

    async def _award_worker(self, worker_index: int) -> None:
        """Process role awards with priority and per-user deduplication."""
        while True:
            priority, _sequence, generation, request = await self._award_queue.get()
            try:
                if (
                    self._pending_awards.get(request.key) is not request
                    or request.generation != generation
                    or request.started
                ):
                    continue

                request.started = True
                started_at = time.monotonic()
                try:
                    result = await self._award_member(request.rule, request.member)
                except asyncio.CancelledError:
                    self._pending_awards.pop(request.key, None)
                    if not request.future.done():
                        request.future.set_result(False)
                    raise
                except Exception:  # noqa: BLE001 - keep the worker alive
                    _log.exception(
                        "Unexpected failure while awarding message-count role to member %s in rule %s.",
                        request.member.id,
                        request.rule.name,
                    )
                    result = False

                finished_at = time.monotonic()
                self._pending_awards.pop(request.key, None)
                if not request.future.done():
                    request.future.set_result(result)
                _log.info(
                    "Completed message-count role award for member %s in rule %s (source=%s, priority=%s, queue_wait=%.2fs, request_time=%.2fs, result=%s, worker=%s).",
                    request.member.id,
                    request.rule.name,
                    request.source,
                    priority,
                    max(0.0, started_at - request.enqueued_at),
                    max(0.0, finished_at - started_at),
                    result,
                    worker_index,
                )
            finally:
                self._award_queue.task_done()

    def _enqueue_award_request(
        self,
        rule: MessageCountRoleRule,
        member: discord.Member,
        *,
        priority: int,
        source: str,
    ) -> asyncio.Future[bool] | None:
        """Submit an award and return its shared future when workers are active."""
        if not self._award_workers_are_running():
            return None

        key = (rule.name, member.id)
        existing = self._pending_awards.get(key)
        if existing is not None and not existing.future.done():
            if priority < existing.priority and not existing.started:
                existing.priority = priority
                existing.source = source
                existing.generation += 1
                existing.sequence = self._award_sequence
                self._award_sequence += 1
                self._award_queue.put_nowait(
                    (
                        existing.priority,
                        existing.sequence,
                        existing.generation,
                        existing,
                    )
                )
            return existing.future

        loop = asyncio.get_running_loop()
        request = _AwardRequest(
            key=key,
            rule=rule,
            member=member,
            source=source,
            priority=priority,
            sequence=self._award_sequence,
            generation=0,
            future=loop.create_future(),
            enqueued_at=time.monotonic(),
        )
        self._award_sequence += 1
        self._pending_awards[key] = request
        self._award_queue.put_nowait(
            (request.priority, request.sequence, request.generation, request)
        )
        return request.future

    async def _queue_award(
        self,
        rule: MessageCountRoleRule,
        member: discord.Member,
        *,
        priority: int,
        source: str,
    ) -> bool:
        """Queue a role award, or process it inline before workers are ready."""
        future = self._enqueue_award_request(
            rule,
            member,
            priority=priority,
            source=source,
        )
        if future is not None:
            return await asyncio.shield(future)

        async with self._inline_award_lock:
            started_at = time.monotonic()
            result = await self._award_member(rule, member)
            _log.info(
                "Completed inline message-count role award for member %s in rule %s (source=%s, request_time=%.2fs, result=%s).",
                member.id,
                rule.name,
                source,
                max(0.0, time.monotonic() - started_at),
                result,
            )
            return result

    async def _award_member_safely(
        self,
        rule: MessageCountRoleRule,
        member: discord.Member,
        *,
        priority: int | None = None,
        source: str = "event",
    ) -> bool:
        """Award through the live-priority queue by default."""
        return await self._queue_award(
            rule,
            member,
            priority=(
                self._AWARD_PRIORITY_LIVE
                if priority is None
                else priority
            ),
            source=source,
        )

    async def _award_cached_member(
        self,
        rule: MessageCountRoleRule,
        user_id: int,
        *,
        priority: int = _AWARD_PRIORITY_LIVE,
        source: str = "threshold",
        wait: bool = True,
        fetch_if_missing: bool = True,
    ) -> None:
        destination_guild = self._get_destination_guild(rule)
        if destination_guild is None:
            return
        member = destination_guild.get_member(user_id)
        if member is None:
            member = self._destination_member_cache.get((destination_guild.id, user_id))
        if member is None:
            if not fetch_if_missing:
                return
            fetch_member = getattr(destination_guild, "fetch_member", None)
            if callable(fetch_member):
                try:
                    member = await fetch_member(user_id)
                except discord.NotFound:
                    return
                except (
                    discord.ClientException,
                    discord.Forbidden,
                    discord.HTTPException,
                ) as error:
                    _log.warning(
                        "Could not fetch destination member %s in guild %s after reaching the message-count threshold: %s",
                        user_id,
                        destination_guild.id,
                        error,
                    )
                    return
        if member is not None:
            self._destination_member_cache[(destination_guild.id, member.id)] = member
            if not wait and self._award_workers_are_running():
                self._enqueue_award_request(
                    rule,
                    member,
                    priority=priority,
                    source=source,
                )
                return
            await self._award_member_safely(
                rule,
                member,
                priority=priority,
                source=source,
            )

    async def _refresh_counts_from_database(
        self,
        rule: MessageCountRoleRule,
    ) -> None:
        """Merge persisted counts into memory before an auto-receive pass."""
        count_rows = await self.bot.GET(GET_MESSAGE_COUNT_ROWS, (rule.name,))
        if not count_rows:
            return

        async with self._state_lock:
            counts = self._counts.setdefault(rule.name, {})
            for user_id, message_count in count_rows:
                user_id = int(user_id)
                message_count = int(message_count)
                if message_count > counts.get(user_id, 0):
                    counts[user_id] = message_count

    async def _refresh_count_for_user(
        self,
        rule: MessageCountRoleRule,
        user_id: int,
    ) -> int:
        """Refresh one member's count before handling a destination join event."""
        try:
            row = await self.bot.GET(
                GET_MESSAGE_COUNT_FOR_USER,
                (rule.name, user_id),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - retain the in-memory fallback
            _log.warning(
                "Could not refresh message count for user %s in rule %s; using the in-memory count: %s",
                user_id,
                rule.name,
                error,
            )
            return self._counts.get(rule.name, {}).get(user_id, 0)
        if not row:
            return self._counts.get(rule.name, {}).get(user_id, 0)

        persisted_count = int(row[0][0])
        async with self._state_lock:
            current_count = self._counts.setdefault(rule.name, {}).get(user_id, 0)
            if persisted_count > current_count:
                self._counts[rule.name][user_id] = persisted_count
            return max(current_count, persisted_count)

    async def _get_qualified_destination_members(
        self,
        destination_guild: discord.Guild,
        qualifying_user_ids: set[int],
    ) -> list[discord.Member]:
        """Find qualifying destination members, including members absent from cache."""
        members_by_id = {
            member.id: member
            for member in getattr(destination_guild, "members", ())
            if member.id in qualifying_user_ids
        }
        for member in members_by_id.values():
            self._destination_member_cache[(destination_guild.id, member.id)] = member

        for user_id in qualifying_user_ids - members_by_id.keys():
            member = self._destination_member_cache.get((destination_guild.id, user_id))
            if member is not None:
                members_by_id[user_id] = member

        missing_user_ids = qualifying_user_ids - members_by_id.keys()
        if not missing_user_ids:
            return list(members_by_id.values())

        fetch_members = getattr(destination_guild, "fetch_members", None)
        if callable(fetch_members):
            try:
                async for member in fetch_members(limit=None):
                    if member is None:
                        continue
                    if member.id not in missing_user_ids:
                        continue
                    members_by_id[member.id] = member
                    self._destination_member_cache[
                        (destination_guild.id, member.id)
                    ] = member
                    missing_user_ids.discard(member.id)
                    if not missing_user_ids:
                        break
            except (
                TypeError,
                discord.ClientException,
                discord.Forbidden,
                discord.HTTPException,
            ) as error:
                _log.warning(
                    "Could not fetch all members in destination guild %s for message-count role backfill: %s",
                    destination_guild.id,
                    error,
                )

        fetch_member = getattr(destination_guild, "fetch_member", None)
        if missing_user_ids and callable(fetch_member):
            for user_id in tuple(missing_user_ids):
                try:
                    member = await fetch_member(user_id)
                except discord.NotFound:
                    continue
                except (
                    TypeError,
                    discord.ClientException,
                    discord.Forbidden,
                    discord.HTTPException,
                ) as error:
                    _log.warning(
                        "Could not fetch destination member %s in guild %s for message-count role backfill: %s",
                        user_id,
                        destination_guild.id,
                        error,
                    )
                    continue
                if member is None:
                    continue
                members_by_id[user_id] = member
                self._destination_member_cache[(destination_guild.id, user_id)] = member

        return list(members_by_id.values())

    async def _award_qualified_members(
        self,
        rule: MessageCountRoleRule,
        destination_guild: discord.Guild,
    ) -> None:
        async with self._state_lock:
            qualifying_user_ids = {
                user_id
                for user_id, message_count in self._counts.get(rule.name, {}).items()
                if message_count >= rule.message_threshold
            }
        if not qualifying_user_ids:
            _log.info(
                "Message-count role backfill found no qualifying users for rule %s.",
                rule.name,
            )
            return

        destination_members = await self._get_qualified_destination_members(
            destination_guild,
            qualifying_user_ids,
        )
        award_results = await asyncio.gather(
            *(
                self._award_member_safely(
                    rule,
                    member,
                    priority=self._AWARD_PRIORITY_BACKFILL,
                    source="backfill",
                )
                for member in destination_members
            ),
            return_exceptions=True,
        )
        processed_count = sum(
            1 for result in award_results if result is True
        )
        for member, result in zip(destination_members, award_results, strict=False):
            if isinstance(result, Exception):
                _log.error(
                    "Message-count role backfill award failed for member %s in rule %s: %s",
                    member.id,
                    rule.name,
                    result,
                )
        _log.info(
            "Message-count role backfill processed %s destination members out of %s qualifying users (%s destination candidates) in guild %s.",
            processed_count,
            len(qualifying_user_ids),
            len(destination_members),
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

    async def _scan_channels(
        self,
        rule: MessageCountRoleRule,
        channels: list[Any],
    ) -> None:
        """Scan distinct channels concurrently with a bounded worker pool."""
        if not channels:
            return

        channel_queue: asyncio.Queue[Any] = asyncio.Queue()
        for channel in channels:
            channel_queue.put_nowait(channel)

        async def worker(worker_index: int) -> None:
            while True:
                channel = await channel_queue.get()
                try:
                    await self._scan_channel(rule, channel)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - keep other channels scanning
                    _log.exception(
                        "Unexpected failure while scanning channel %s for message-count role rule %s (worker=%s).",
                        getattr(channel, "id", "unknown"),
                        rule.name,
                        worker_index,
                    )
                finally:
                    channel_queue.task_done()

        workers = [
            asyncio.create_task(
                worker(index),
                name=f"message-count-role-scan-{rule.name}-{index}",
            )
            for index in range(
                min(self.settings.history_scan_worker_count, len(channels))
            )
        ]
        try:
            await channel_queue.join()
        finally:
            for worker_task in workers:
                worker_task.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)

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
                "Scanning %s message channels for message-count role rule %s with %s workers.",
                len(channels),
                rule.name,
                min(self.settings.history_scan_worker_count, len(channels)),
            )
            await self._scan_channels(rule, channels)
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

    async def auto_receive_all(self) -> None:
        """Retry all qualifying destination role grants independently of history scans."""
        if not self.settings.enabled:
            return
        for rule in self.settings.rules:
            destination_guild = self._get_destination_guild(rule)
            if destination_guild is None:
                continue
            try:
                await self._refresh_counts_from_database(rule)
                await self._award_qualified_members(rule, destination_guild)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep other rules running
                _log.exception(
                    "Unexpected failure during message-count auto-receive for rule %s.",
                    rule.name,
                )

    @tasks.loop(minutes=15)
    async def auto_receive_reconcile(self) -> None:
        await self.auto_receive_all()

    @auto_receive_reconcile.before_loop
    async def before_auto_receive_reconcile(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if (
            self.settings.enabled
            and self.settings.rules
        ):
            self._start_award_workers()
        if (
            self.settings.enabled
            and self.settings.rules
            and not self.periodic_reconcile.is_running()
        ):
            self.periodic_reconcile.start()
        if (
            self.settings.enabled
            and self.settings.rules
            and not self.auto_receive_reconcile.is_running()
        ):
            self.auto_receive_reconcile.start()

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
            self._destination_member_cache[(member.guild.id, member.id)] = member
            lookup_started_at = time.monotonic()
            message_count = await self._refresh_count_for_user(rule, member.id)
            _log.info(
                "Checked message-count role eligibility for member %s in rule %s (count=%s, threshold=%s, lookup_time=%.2fs).",
                member.id,
                rule.name,
                message_count,
                rule.message_threshold,
                max(0.0, time.monotonic() - lookup_started_at),
            )
            if message_count >= rule.message_threshold:
                await self._award_member_safely(
                    rule,
                    member,
                    priority=self._AWARD_PRIORITY_LIVE,
                    source="member-join",
                )

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
            self._destination_member_cache[(after.guild.id, after.id)] = after
            if not any(
                existing_role.id == rule.destination_role_id
                for existing_role in after.roles
            ):
                self._awarded_users.discard((rule.name, after.id))
            if (
                self._counts.get(rule.name, {}).get(after.id, 0)
                >= rule.message_threshold
            ):
                await self._award_member_safely(
                    rule,
                    after,
                    priority=self._AWARD_PRIORITY_LIVE,
                    source="member-update",
                )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        for rule in self.settings.rules:
            if member.guild.id != rule.destination_guild_id:
                continue
            self._destination_member_cache.pop((member.guild.id, member.id), None)
            self._awarded_users.discard((rule.name, member.id))


async def setup(bot: TMWBot) -> None:
    await bot.add_cog(MessageCountRoles(bot))
