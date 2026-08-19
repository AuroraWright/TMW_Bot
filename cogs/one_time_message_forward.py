import asyncio
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any

import discord
import yaml
from discord.ext import commands

from lib.bot import TMWBot

_log = logging.getLogger(__name__)

MESSAGE_FORWARD_SETTINGS_PATH = Path(
    os.getenv(
        "ALT_MESSAGE_FORWARD_SETTINGS_PATH",
        "config/one_time_message_forward.yml",
    )
)

CREATE_FORWARD_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS one_time_message_forward_jobs (
    job_id TEXT PRIMARY KEY,
    source_guild_id INTEGER NOT NULL,
    source_channel_id INTEGER NOT NULL,
    first_message_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL,
    destination_guild_id INTEGER NOT NULL,
    destination_channel_id INTEGER NOT NULL,
    destination_scan_after_id INTEGER,
    status TEXT NOT NULL,
    last_error TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
);"""

CREATE_FORWARD_RESULTS_TABLE = """
CREATE TABLE IF NOT EXISTS one_time_message_forward_results (
    job_id TEXT NOT NULL,
    source_message_id INTEGER NOT NULL,
    destination_message_id INTEGER,
    outcome TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, source_message_id)
);"""

INSERT_FORWARD_JOB = """
INSERT INTO one_time_message_forward_jobs (
    job_id, source_guild_id, source_channel_id, first_message_id,
    last_message_id, destination_guild_id, destination_channel_id,
    destination_scan_after_id, status, started_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
ON CONFLICT(job_id) DO NOTHING;"""

GET_FORWARD_JOB = """
SELECT source_guild_id, source_channel_id, first_message_id, last_message_id,
       destination_guild_id, destination_channel_id, destination_scan_after_id,
       status
FROM one_time_message_forward_jobs
WHERE job_id = ?;"""

GET_FORWARD_RESULTS = """
SELECT source_message_id, outcome
FROM one_time_message_forward_results
WHERE job_id = ?;"""

INSERT_FORWARD_RESULT = """
INSERT INTO one_time_message_forward_results (
    job_id, source_message_id, destination_message_id,
    outcome, details, created_at
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(job_id, source_message_id) DO NOTHING;"""

MARK_FORWARD_JOB_RUNNING = """
UPDATE one_time_message_forward_jobs
SET status = 'running', last_error = NULL
WHERE job_id = ?;"""

MARK_FORWARD_JOB_FAILED = """
UPDATE one_time_message_forward_jobs
SET status = 'failed', last_error = ?
WHERE job_id = ?;"""

MARK_FORWARD_JOB_COMPLETE = """
UPDATE one_time_message_forward_jobs
SET status = ?, last_error = NULL, completed_at = ?
WHERE job_id = ?;"""

GET_FORWARD_RESULT_COUNTS = """
SELECT outcome, COUNT(*)
FROM one_time_message_forward_results
WHERE job_id = ?
GROUP BY outcome;"""

COMPLETE_STATUSES = {"complete", "complete_with_skips"}
FORWARDABLE_MESSAGE_TYPES = {
    discord.MessageType.default,
    discord.MessageType.reply,
    discord.MessageType.chat_input_command,
    discord.MessageType.context_menu_command,
}


class ForwardJobConfigurationError(RuntimeError):
    pass


def _snowflake(data: dict[str, Any], key: str) -> int:
    try:
        value = int(data[key])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{key} must be a Discord ID.") from error
    if value <= 0:
        raise ValueError(f"{key} must be a positive Discord ID.")
    return value


@dataclass(frozen=True)
class MessageForwardSettings:
    enabled: bool
    job_id: str
    source_guild_id: int
    source_channel_id: int
    first_message_id: int
    last_message_id: int
    destination_guild_id: int
    destination_channel_id: int
    send_delay_seconds: float
    retry_delay_seconds: float
    max_attempts: int

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "MessageForwardSettings":
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be true or false.")

        job_id = data.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be a non-empty string.")

        first_message_id = _snowflake(data, "first_message_id")
        last_message_id = _snowflake(data, "last_message_id")
        if first_message_id > last_message_id:
            raise ValueError("first_message_id must not be newer than last_message_id.")

        try:
            send_delay_seconds = float(data.get("send_delay_seconds", 1.25))
            retry_delay_seconds = float(data.get("retry_delay_seconds", 300))
            max_attempts = int(data.get("max_attempts", 3))
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Forward timing and retry settings must be numbers."
            ) from error

        if send_delay_seconds < 1:
            raise ValueError("send_delay_seconds must be at least one second.")
        if retry_delay_seconds < 1:
            raise ValueError("retry_delay_seconds must be at least one second.")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one.")

        return cls(
            enabled=enabled,
            job_id=job_id.strip(),
            source_guild_id=_snowflake(data, "source_guild_id"),
            source_channel_id=_snowflake(data, "source_channel_id"),
            first_message_id=first_message_id,
            last_message_id=last_message_id,
            destination_guild_id=_snowflake(data, "destination_guild_id"),
            destination_channel_id=_snowflake(data, "destination_channel_id"),
            send_delay_seconds=send_delay_seconds,
            retry_delay_seconds=retry_delay_seconds,
            max_attempts=max_attempts,
        )


def load_message_forward_settings(path: Path) -> MessageForwardSettings:
    with path.open("r", encoding="utf-8") as settings_file:
        data = yaml.safe_load(settings_file)
    if not isinstance(data, dict):
        raise TypeError("One-time message-forward settings must be a YAML mapping.")
    return MessageForwardSettings.from_mapping(data)


message_forward_settings = load_message_forward_settings(MESSAGE_FORWARD_SETTINGS_PATH)


class OneTimeMessageForward(commands.Cog):
    def __init__(
        self,
        bot: TMWBot,
        settings: MessageForwardSettings = message_forward_settings,
    ):
        self.bot = bot
        self.settings = settings
        self._forward_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        await self.bot.RUN(CREATE_FORWARD_JOBS_TABLE)
        await self.bot.RUN(CREATE_FORWARD_RESULTS_TABLE)

    def cog_unload(self) -> None:
        if self._forward_task is not None:
            self._forward_task.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.settings.enabled:
            return
        if self._forward_task is None or self._forward_task.done():
            self._forward_task = asyncio.create_task(
                self._run_with_retries(),
                name=f"message-forward-{self.settings.job_id}",
            )

    async def _run_with_retries(self) -> None:
        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                await self._run_once()
                return
            except asyncio.CancelledError:
                raise
            except ForwardJobConfigurationError as error:
                _log.error(
                    "Message-forward job %s stopped: %s", self.settings.job_id, error
                )
                return
            except Exception as error:  # noqa: BLE001 - task boundary must log failures
                await self._mark_failed(str(error))
                _log.exception(
                    "Message-forward job %s failed on attempt %s/%s.",
                    self.settings.job_id,
                    attempt,
                    self.settings.max_attempts,
                )
                if attempt == self.settings.max_attempts:
                    return
                await asyncio.sleep(self.settings.retry_delay_seconds)

    async def _mark_failed(self, error: str) -> None:
        await self.bot.RUN(
            MARK_FORWARD_JOB_FAILED,
            (error[:2000], self.settings.job_id),
        )

    async def _get_channel(self, channel_id: int) -> discord.abc.GuildChannel:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound as error:
                raise ForwardJobConfigurationError(
                    f"Channel {channel_id} does not exist or is unavailable to the bot."
                ) from error
            except discord.Forbidden as error:
                raise ForwardJobConfigurationError(
                    f"The bot cannot access channel {channel_id}."
                ) from error
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise ForwardJobConfigurationError(
                f"Channel {channel_id} is not a text channel or thread."
            )
        return channel

    @staticmethod
    def _check_channel_permissions(
        source: discord.TextChannel | discord.Thread,
        destination: discord.TextChannel | discord.Thread,
    ) -> None:
        source_member = source.guild.me
        destination_member = destination.guild.me
        if source_member is None or destination_member is None:
            raise ForwardJobConfigurationError(
                "The bot must be a member of both configured servers."
            )

        source_permissions = source.permissions_for(source_member)
        if (
            not source_permissions.view_channel
            or not source_permissions.read_message_history
        ):
            raise ForwardJobConfigurationError(
                "The bot needs View Channel and Read Message History in the source channel."
            )

        destination_permissions = destination.permissions_for(destination_member)
        if (
            not destination_permissions.view_channel
            or not destination_permissions.send_messages
        ):
            raise ForwardJobConfigurationError(
                "The bot needs View Channel and Send Messages in the destination channel."
            )

    async def _preflight(
        self,
    ) -> tuple[
        discord.TextChannel | discord.Thread,
        discord.TextChannel | discord.Thread,
        discord.Message,
        discord.Message,
    ]:
        if not self.bot.intents.message_content:
            raise ForwardJobConfigurationError(
                "The Message Content intent is required for native forwarding."
            )

        source = await self._get_channel(self.settings.source_channel_id)
        destination = await self._get_channel(self.settings.destination_channel_id)

        if source.guild.id != self.settings.source_guild_id:
            raise ForwardJobConfigurationError(
                "The source channel does not belong to the configured source server."
            )
        if destination.guild.id != self.settings.destination_guild_id:
            raise ForwardJobConfigurationError(
                "The destination channel does not belong to the configured destination server."
            )
        self._check_channel_permissions(source, destination)

        try:
            first_message = await source.fetch_message(self.settings.first_message_id)
            if self.settings.first_message_id == self.settings.last_message_id:
                last_message = first_message
            else:
                last_message = await source.fetch_message(self.settings.last_message_id)
        except discord.NotFound as error:
            raise ForwardJobConfigurationError(
                "A configured boundary message does not exist in the source channel."
            ) from error
        except discord.Forbidden as error:
            raise ForwardJobConfigurationError(
                "The bot cannot read the configured source messages."
            ) from error

        return source, destination, first_message, last_message

    def _validate_job_record(self, job: tuple) -> None:
        configured_values = (
            self.settings.source_guild_id,
            self.settings.source_channel_id,
            self.settings.first_message_id,
            self.settings.last_message_id,
            self.settings.destination_guild_id,
            self.settings.destination_channel_id,
        )
        if tuple(job[:6]) != configured_values:
            raise ForwardJobConfigurationError(
                "The job_id already exists with different channel or range settings."
            )

    async def _initialise_job(
        self,
        destination: discord.TextChannel | discord.Thread,
    ) -> tuple[str, int | None]:
        started_at = discord.utils.utcnow().astimezone(timezone.utc).isoformat()
        await self.bot.RUN(
            INSERT_FORWARD_JOB,
            (
                self.settings.job_id,
                self.settings.source_guild_id,
                self.settings.source_channel_id,
                self.settings.first_message_id,
                self.settings.last_message_id,
                self.settings.destination_guild_id,
                self.settings.destination_channel_id,
                destination.last_message_id,
                started_at,
            ),
        )
        job = await self.bot.GET_ONE(GET_FORWARD_JOB, (self.settings.job_id,))
        if job is None:
            raise RuntimeError("The message-forward job could not be initialized.")

        self._validate_job_record(job)

        status = job[7]
        if status not in COMPLETE_STATUSES:
            await self.bot.RUN(MARK_FORWARD_JOB_RUNNING, (self.settings.job_id,))
        return status, job[6]

    async def _record_result(
        self,
        source_message_id: int,
        destination_message_id: int | None,
        outcome: str,
        details: str | None = None,
    ) -> None:
        created_at = discord.utils.utcnow().astimezone(timezone.utc).isoformat()
        await self.bot.RUN(
            INSERT_FORWARD_RESULT,
            (
                self.settings.job_id,
                source_message_id,
                destination_message_id,
                outcome,
                details[:2000] if details else None,
                created_at,
            ),
        )

    async def _reconcile_destination(
        self,
        destination: discord.TextChannel | discord.Thread,
        scan_after_id: int | None,
    ) -> None:
        after = discord.Object(id=scan_after_id) if scan_after_id else None
        async for destination_message in destination.history(
            limit=None,
            after=after,
            oldest_first=True,
        ):
            reference = destination_message.reference
            if (
                reference is None
                or reference.type != discord.MessageReferenceType.forward
            ):
                continue
            if reference.channel_id != self.settings.source_channel_id:
                continue
            if reference.message_id is None:
                continue
            if not (
                self.settings.first_message_id
                <= reference.message_id
                <= self.settings.last_message_id
            ):
                continue
            await self._record_result(
                reference.message_id,
                destination_message.id,
                "forwarded",
                "Recovered from destination history after a restart.",
            )

    async def _source_messages(
        self,
        source: discord.TextChannel | discord.Thread,
        first_message: discord.Message,
        last_message: discord.Message,
    ) -> AsyncIterator[discord.Message]:
        yield first_message
        if first_message.id == last_message.id:
            return

        cursor = first_message.id
        while True:
            page = [
                message
                async for message in source.history(
                    limit=100,
                    after=discord.Object(id=cursor),
                    oldest_first=True,
                )
            ]
            if not page:
                break

            reached_last_message = False
            for message in page:
                if message.id >= last_message.id:
                    reached_last_message = True
                    break
                yield message

            if reached_last_message or len(page) < 100:
                break
            cursor = page[-1].id

        yield last_message

    async def _forward_messages(
        self,
        source: discord.TextChannel | discord.Thread,
        destination: discord.TextChannel | discord.Thread,
        first_message: discord.Message,
        last_message: discord.Message,
    ) -> None:
        results = await self.bot.GET(
            GET_FORWARD_RESULTS,
            (self.settings.job_id,),
        )
        processed_message_ids = {row[0] for row in results}
        forwarded_this_run = 0

        async for message in self._source_messages(
            source,
            first_message,
            last_message,
        ):
            if message.id in processed_message_ids:
                continue

            if message.type not in FORWARDABLE_MESSAGE_TYPES:
                details = f"Discord does not support forwarding message type {message.type!s}."
                await self._record_result(message.id, None, "skipped", details)
                _log.warning(
                    "Skipping unsupported message %s in forward job %s: %s",
                    message.id,
                    self.settings.job_id,
                    message.type,
                )
                continue

            try:
                forwarded_message = await message.forward(destination)
            except (discord.Forbidden, discord.NotFound) as error:
                raise ForwardJobConfigurationError(
                    f"Discord rejected forwarding source message {message.id}: {error}"
                ) from error
            except discord.HTTPException as error:
                if error.status == 400:
                    await self._record_result(
                        message.id,
                        None,
                        "skipped",
                        f"Discord rejected this individual message: {error}",
                    )
                    _log.warning(
                        "Discord rejected source message %s in job %s; recording it as skipped.",
                        message.id,
                        self.settings.job_id,
                    )
                    continue
                raise

            await self._record_result(
                message.id,
                forwarded_message.id,
                "forwarded",
            )
            processed_message_ids.add(message.id)
            forwarded_this_run += 1
            if forwarded_this_run % 50 == 0:
                _log.info(
                    "Forward job %s sent %s messages in this run; latest source ID: %s.",
                    self.settings.job_id,
                    forwarded_this_run,
                    message.id,
                )
            await asyncio.sleep(self.settings.send_delay_seconds)

    async def _complete_job(self) -> None:
        counts = dict(
            await self.bot.GET(
                GET_FORWARD_RESULT_COUNTS,
                (self.settings.job_id,),
            )
        )
        forwarded_count = counts.get("forwarded", 0)
        skipped_count = counts.get("skipped", 0)
        status = "complete_with_skips" if skipped_count else "complete"
        completed_at = discord.utils.utcnow().astimezone(timezone.utc).isoformat()
        await self.bot.RUN(
            MARK_FORWARD_JOB_COMPLETE,
            (status, completed_at, self.settings.job_id),
        )
        _log.info(
            "Message-forward job %s finished: %s forwarded, %s skipped.",
            self.settings.job_id,
            forwarded_count,
            skipped_count,
        )

    async def _run_once(self) -> None:
        existing_job = await self.bot.GET_ONE(
            GET_FORWARD_JOB,
            (self.settings.job_id,),
        )
        if existing_job is not None:
            self._validate_job_record(existing_job)
            if existing_job[7] in COMPLETE_STATUSES:
                _log.info(
                    "Message-forward job %s is already complete.",
                    self.settings.job_id,
                )
                return

        source, destination, first_message, last_message = await self._preflight()
        status, scan_after_id = await self._initialise_job(destination)
        if status in COMPLETE_STATUSES:
            _log.info(
                "Message-forward job %s is already complete.", self.settings.job_id
            )
            return

        await self._reconcile_destination(destination, scan_after_id)
        await self._forward_messages(
            source,
            destination,
            first_message,
            last_message,
        )
        await self._complete_job()


async def setup(bot: TMWBot) -> None:
    await bot.add_cog(OneTimeMessageForward(bot))
