import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
import yaml
from discord.ext import commands, tasks

from cogs.sub_server_access import SUB_SERVER_SETTINGS_PATH, sub_server_settings
from lib.bot import TMWBot

_log = logging.getLogger(__name__)

MIRROR_REASON = "One-way mirror from the configured main server"

CREATE_MIRROR_ENTITIES_TABLE = """
CREATE TABLE IF NOT EXISTS sub_server_mirror_entities (
    main_guild_id INTEGER NOT NULL,
    sub_guild_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    source_entity_id INTEGER NOT NULL,
    destination_entity_id INTEGER NOT NULL,
    PRIMARY KEY (main_guild_id, sub_guild_id, entity_type, source_entity_id),
    UNIQUE (main_guild_id, sub_guild_id, entity_type, destination_entity_id)
);"""

GET_MIRROR_ENTITIES = """
SELECT source_entity_id, destination_entity_id
FROM sub_server_mirror_entities
WHERE main_guild_id = ? AND sub_guild_id = ? AND entity_type = ?;"""

UPSERT_MIRROR_ENTITY = """
INSERT INTO sub_server_mirror_entities (
    main_guild_id, sub_guild_id, entity_type,
    source_entity_id, destination_entity_id
)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(main_guild_id, sub_guild_id, entity_type, source_entity_id)
DO UPDATE SET destination_entity_id = excluded.destination_entity_id;"""

DELETE_MIRROR_ENTITY = """
DELETE FROM sub_server_mirror_entities
WHERE main_guild_id = ? AND sub_guild_id = ?
  AND entity_type = ? AND source_entity_id = ?;"""


@dataclass(frozen=True)
class MirrorSettings:
    enabled: bool = False
    reconcile_interval_minutes: float = 30.0
    event_debounce_seconds: float = 10.0
    mutation_delay_seconds: float = 0.5
    emoji_create_delay_seconds: float = 5.0
    delete_unmapped_roles: bool = False
    delete_unmapped_channels: bool = False
    delete_unmapped_emojis: bool = False
    mirror_guild_settings: bool = True
    mirror_member_roles: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "MirrorSettings":
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise TypeError("The sub-server mirror setting must be a YAML mapping.")

        values = {
            field: data.get(field, getattr(cls(), field))
            for field in cls.__dataclass_fields__
        }
        boolean_fields = {
            "enabled",
            "delete_unmapped_roles",
            "delete_unmapped_channels",
            "delete_unmapped_emojis",
            "mirror_guild_settings",
            "mirror_member_roles",
        }
        for field in boolean_fields:
            if not isinstance(values[field], bool):
                raise TypeError(f"mirror.{field} must be true or false.")

        numeric_fields = {
            "reconcile_interval_minutes",
            "event_debounce_seconds",
            "mutation_delay_seconds",
            "emoji_create_delay_seconds",
        }
        for field in numeric_fields:
            try:
                values[field] = float(values[field])
            except (TypeError, ValueError) as error:
                raise TypeError(f"mirror.{field} must be a number.") from error
            if values[field] < 0:
                raise ValueError(f"mirror.{field} cannot be negative.")

        if values["reconcile_interval_minutes"] == 0:
            raise ValueError(
                "mirror.reconcile_interval_minutes must be greater than zero."
            )
        return cls(**values)


def load_mirror_settings(path: Path) -> MirrorSettings:
    with path.open("r", encoding="utf-8") as settings_file:
        data = yaml.safe_load(settings_file)
    if not isinstance(data, Mapping):
        raise TypeError("Sub-server settings must be a YAML mapping.")
    return MirrorSettings.from_mapping(data.get("mirror"))


mirror_settings = load_mirror_settings(SUB_SERVER_SETTINGS_PATH)


class SubServerMirror(commands.Cog):
    """Maintains a strictly one-way main-guild to sub-server mirror."""

    def __init__(
        self,
        bot: TMWBot,
        settings: MirrorSettings = mirror_settings,
    ) -> None:
        self.bot = bot
        self.access_settings = sub_server_settings
        self.settings = settings
        self._mirror_lock = asyncio.Lock()
        self._debounced_reconcile: asyncio.Task | None = None
        self._reconcile_requested = False
        self._member_sync_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._member_sync_requested: set[tuple[int, int]] = set()

    async def cog_load(self) -> None:
        await self.bot.RUN(CREATE_MIRROR_ENTITIES_TABLE)
        self.periodic_reconcile.change_interval(
            minutes=self.settings.reconcile_interval_minutes
        )

    async def cog_unload(self) -> None:
        self.periodic_reconcile.cancel()
        if self._debounced_reconcile is not None:
            self._debounced_reconcile.cancel()
        for task in self._member_sync_tasks.values():
            task.cancel()
        self._member_sync_tasks.clear()
        self._member_sync_requested.clear()

    def _get_main_guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.access_settings.main_guild_id)

    def _get_sub_guilds(self) -> list[discord.Guild]:
        return [
            guild
            for guild_id in self.access_settings.sub_guild_ids
            if (guild := self.bot.get_guild(guild_id)) is not None
        ]

    def _is_mirrored_guild_id(self, guild_id: int) -> bool:
        return guild_id == self.access_settings.main_guild_id or (
            guild_id in self.access_settings.sub_guild_ids
        )

    def _validate_direction(
        self,
        source_guild: discord.Guild,
        destination_guild: discord.Guild,
    ) -> None:
        if source_guild.id != self.access_settings.main_guild_id:
            raise RuntimeError("Mirror source is not the configured main guild.")
        if destination_guild.id not in self.access_settings.sub_guild_ids:
            raise RuntimeError("Mirror destination is not a configured sub-server.")
        if source_guild.id == destination_guild.id:
            raise RuntimeError("The mirror source and destination must differ.")

    async def _mutation_pause(self, delay: float | None = None) -> None:
        seconds = self.settings.mutation_delay_seconds if delay is None else delay
        if seconds:
            await asyncio.sleep(seconds)

    async def _load_entity_map(
        self,
        destination_guild_id: int,
        entity_type: str,
    ) -> dict[int, int]:
        rows = await self.bot.GET(
            GET_MIRROR_ENTITIES,
            (
                self.access_settings.main_guild_id,
                destination_guild_id,
                entity_type,
            ),
        )
        return {source_id: destination_id for source_id, destination_id in rows}

    async def _save_entity_mapping(
        self,
        destination_guild_id: int,
        entity_type: str,
        source_id: int,
        destination_id: int,
    ) -> None:
        await self.bot.RUN(
            UPSERT_MIRROR_ENTITY,
            (
                self.access_settings.main_guild_id,
                destination_guild_id,
                entity_type,
                source_id,
                destination_id,
            ),
        )

    async def _delete_entity_mapping(
        self,
        destination_guild_id: int,
        entity_type: str,
        source_id: int,
    ) -> None:
        await self.bot.RUN(
            DELETE_MIRROR_ENTITY,
            (
                self.access_settings.main_guild_id,
                destination_guild_id,
                entity_type,
                source_id,
            ),
        )

    @staticmethod
    def _role_signature(
        role: discord.Role, *, include_display_icon: bool = True
    ) -> tuple[Any, ...]:
        icon_signature = None
        if include_display_icon:
            display_icon = getattr(role, "display_icon", None)
            if isinstance(display_icon, str):
                icon_signature = ("unicode", display_icon)
            elif display_icon is not None:
                icon_signature = ("asset", getattr(display_icon, "key", None))
        return (
            role.name,
            role.permissions.value,
            role.colour.value,
            getattr(getattr(role, "secondary_colour", None), "value", None),
            getattr(getattr(role, "tertiary_colour", None), "value", None),
            role.hoist,
            role.mentionable,
            icon_signature,
        )

    @staticmethod
    def _supports_role_icons(guild: discord.Guild) -> bool:
        return guild.premium_tier >= 2 or "ROLE_ICONS" in guild.features

    @staticmethod
    def _matching_bot_role(
        source_role: discord.Role,
        destination_guild: discord.Guild,
    ) -> discord.Role | None:
        source_tags = getattr(source_role, "tags", None)
        bot_id = getattr(source_tags, "bot_id", None)
        if bot_id is None:
            return None
        destination_member = destination_guild.get_member(bot_id)
        if destination_member is None:
            return None
        return next(
            (
                role
                for role in destination_member.roles
                if getattr(getattr(role, "tags", None), "bot_id", None) == bot_id
            ),
            None,
        )

    async def _role_display_icon(self, role: discord.Role) -> bytes | str | None:
        display_icon = getattr(role, "display_icon", None)
        if display_icon is None or isinstance(display_icon, str):
            return display_icon
        try:
            return await display_icon.read()
        except (discord.HTTPException, OSError) as error:
            _log.warning("Could not read display icon for role %s: %s", role.id, error)
            return None

    async def _create_role(
        self,
        destination_guild: discord.Guild,
        source_role: discord.Role,
    ) -> discord.Role | None:
        kwargs = {
            "name": source_role.name,
            "permissions": source_role.permissions,
            "colour": source_role.colour,
            "secondary_colour": source_role.secondary_colour,
            "tertiary_colour": source_role.tertiary_colour,
            "hoist": source_role.hoist,
            "mentionable": source_role.mentionable,
            "reason": MIRROR_REASON,
        }
        if self._supports_role_icons(destination_guild):
            display_icon = await self._role_display_icon(source_role)
            if display_icon is not None:
                kwargs["display_icon"] = display_icon
        try:
            role = await destination_guild.create_role(**kwargs)
        except discord.Forbidden as error:
            _log.error("Failed to create mirrored role %s: %s", source_role.id, error)
            return None
        except discord.HTTPException as error:
            if "display_icon" not in kwargs:
                _log.error(
                    "Failed to create mirrored role %s: %s", source_role.id, error
                )
                return None
            _log.warning(
                "Role icon for %s could not be mirrored; retrying without it: %s",
                source_role.id,
                error,
            )
            kwargs.pop("display_icon")
            try:
                role = await destination_guild.create_role(**kwargs)
            except (discord.Forbidden, discord.HTTPException) as retry_error:
                _log.error(
                    "Failed to create mirrored role %s: %s",
                    source_role.id,
                    retry_error,
                )
                return None
        await self._mutation_pause()
        return role

    async def _edit_role(
        self,
        destination_role: discord.Role,
        source_role: discord.Role,
    ) -> discord.Role:
        include_display_icon = self._supports_role_icons(destination_role.guild)
        if destination_role.managed or self._role_signature(
            destination_role, include_display_icon=include_display_icon
        ) == self._role_signature(
            source_role, include_display_icon=include_display_icon
        ):
            return destination_role
        kwargs = {
            "name": source_role.name,
            "permissions": source_role.permissions,
            "colour": source_role.colour,
            "secondary_colour": source_role.secondary_colour,
            "tertiary_colour": source_role.tertiary_colour,
            "hoist": source_role.hoist,
            "mentionable": source_role.mentionable,
            "reason": MIRROR_REASON,
        }
        if include_display_icon:
            kwargs["display_icon"] = await self._role_display_icon(source_role)
        try:
            updated = await destination_role.edit(**kwargs)
        except (discord.Forbidden, discord.HTTPException) as error:
            _log.error("Failed to update mirrored role %s: %s", source_role.id, error)
            return destination_role
        await self._mutation_pause()
        return updated or destination_role

    async def _sync_roles(
        self,
        source_guild: discord.Guild,
        destination_guild: discord.Guild,
    ) -> dict[int, discord.Role]:
        mapping_ids = await self._load_entity_map(destination_guild.id, "role")
        role_map: dict[int, discord.Role] = {
            source_guild.default_role.id: destination_guild.default_role
        }
        used_destination_ids = {destination_guild.default_role.id}
        source_roles = [role for role in source_guild.roles if not role.is_default()]
        destination_supports_role_icons = self._supports_role_icons(destination_guild)

        if (
            destination_guild.default_role.permissions
            != source_guild.default_role.permissions
        ):
            try:
                await destination_guild.default_role.edit(
                    permissions=source_guild.default_role.permissions,
                    reason=MIRROR_REASON,
                )
                await self._mutation_pause()
            except (discord.Forbidden, discord.HTTPException) as error:
                _log.error("Failed to mirror @everyone permissions: %s", error)

        for source_role in source_roles:
            destination_role = self._matching_bot_role(source_role, destination_guild)
            mapped_id = mapping_ids.get(source_role.id)
            if destination_role is None and mapped_id is not None:
                destination_role = destination_guild.get_role(mapped_id)
                if destination_role is None:
                    await self._delete_entity_mapping(
                        destination_guild.id, "role", source_role.id
                    )

            if destination_role is None:
                destination_role = next(
                    (
                        role
                        for role in destination_guild.roles
                        if role.id not in used_destination_ids
                        and not role.managed
                        and self._role_signature(
                            role,
                            include_display_icon=destination_supports_role_icons,
                        )
                        == self._role_signature(
                            source_role,
                            include_display_icon=destination_supports_role_icons,
                        )
                    ),
                    None,
                )

            if destination_role is None:
                destination_role = await self._create_role(
                    destination_guild, source_role
                )
            elif not destination_role.managed:
                destination_role = await self._edit_role(destination_role, source_role)
            if destination_role is None:
                continue

            role_map[source_role.id] = destination_role
            used_destination_ids.add(destination_role.id)
            await self._save_entity_mapping(
                destination_guild.id,
                "role",
                source_role.id,
                destination_role.id,
            )

        source_ids = {role.id for role in source_roles}
        for stale_source_id, destination_id in mapping_ids.items():
            if stale_source_id in source_ids:
                continue
            stale_role = destination_guild.get_role(destination_id)
            if stale_role is not None and not stale_role.managed:
                try:
                    await stale_role.delete(reason=MIRROR_REASON)
                    await self._mutation_pause()
                except (discord.Forbidden, discord.HTTPException) as error:
                    _log.error(
                        "Failed to delete stale mirrored role %s: %s",
                        destination_id,
                        error,
                    )
                    continue
            await self._delete_entity_mapping(
                destination_guild.id, "role", stale_source_id
            )

        if self.settings.delete_unmapped_roles:
            for role in list(destination_guild.roles):
                if role.is_default() or role.managed or role.id in used_destination_ids:
                    continue
                try:
                    await role.delete(reason=MIRROR_REASON)
                    await self._mutation_pause()
                except (discord.Forbidden, discord.HTTPException) as error:
                    _log.error(
                        "Failed to delete unmapped sub-server role %s: %s",
                        role.id,
                        error,
                    )

        positions = {
            role_map[source_role.id]: source_role.position
            for source_role in source_roles
            if source_role.id in role_map
            and not role_map[source_role.id].managed
            and role_map[source_role.id].is_assignable()
            and role_map[source_role.id].position != source_role.position
        }
        if positions:
            try:
                await destination_guild.edit_role_positions(
                    positions=positions,
                    reason=MIRROR_REASON,
                )
                await self._mutation_pause()
            except (discord.Forbidden, discord.HTTPException) as error:
                _log.error(
                    "Failed to mirror role positions in %s: %s",
                    destination_guild.id,
                    error,
                )

        return role_map

    @staticmethod
    def _mapped_emoji_roles(
        source_emoji: discord.Emoji,
        role_map: Mapping[int, discord.Role],
    ) -> list[discord.Role]:
        return [
            role_map[role.id]
            for role in source_emoji.roles
            if role.id in role_map and not role_map[role.id].is_default()
        ]

    async def _sync_emojis(
        self,
        source_guild: discord.Guild,
        destination_guild: discord.Guild,
        role_map: Mapping[int, discord.Role],
    ) -> dict[int, discord.Emoji]:
        mapping_ids = await self._load_entity_map(destination_guild.id, "emoji")
        emoji_map: dict[int, discord.Emoji] = {}
        used_destination_ids: set[int] = set()
        source_ids = {emoji.id for emoji in source_guild.emojis}
        deleted_destination_ids: set[int] = set()

        for stale_source_id, destination_id in mapping_ids.items():
            if stale_source_id in source_ids:
                continue
            stale_emoji = destination_guild.get_emoji(destination_id)
            if stale_emoji is not None:
                try:
                    await stale_emoji.delete(reason=MIRROR_REASON)
                    deleted_destination_ids.add(stale_emoji.id)
                    await self._mutation_pause()
                except (discord.Forbidden, discord.HTTPException) as error:
                    _log.error(
                        "Failed to delete stale mirrored emoji %s: %s",
                        destination_id,
                        error,
                    )
                    continue
            await self._delete_entity_mapping(
                destination_guild.id, "emoji", stale_source_id
            )

        destination_emojis = {
            emoji.id: emoji
            for emoji in destination_guild.emojis
            if emoji.id not in deleted_destination_ids
        }
        missing_source_emojis: list[tuple[discord.Emoji, list[discord.Role]]] = []

        for source_emoji in source_guild.emojis:
            mapped_id = mapping_ids.get(source_emoji.id)
            destination_emoji = (
                destination_emojis.get(mapped_id) if mapped_id is not None else None
            )
            if destination_emoji is None:
                destination_emoji = next(
                    (
                        emoji
                        for emoji in destination_emojis.values()
                        if emoji.id not in used_destination_ids
                        and emoji.name == source_emoji.name
                        and emoji.animated == source_emoji.animated
                    ),
                    None,
                )

            desired_roles = self._mapped_emoji_roles(source_emoji, role_map)
            if destination_emoji is None:
                missing_source_emojis.append((source_emoji, desired_roles))
                continue

            desired_role_ids = {role.id for role in desired_roles}
            if (
                destination_emoji.name != source_emoji.name
                or {role.id for role in destination_emoji.roles} != desired_role_ids
            ):
                try:
                    destination_emoji = await destination_emoji.edit(
                        name=source_emoji.name,
                        roles=desired_roles,
                        reason=MIRROR_REASON,
                    )
                    await self._mutation_pause()
                except (discord.Forbidden, discord.HTTPException) as error:
                    _log.error(
                        "Failed to update mirrored emoji %s: %s",
                        source_emoji.id,
                        error,
                    )
            emoji_map[source_emoji.id] = destination_emoji
            used_destination_ids.add(destination_emoji.id)
            await self._save_entity_mapping(
                destination_guild.id,
                "emoji",
                source_emoji.id,
                destination_emoji.id,
            )

        if self.settings.delete_unmapped_emojis:
            for emoji in destination_emojis.values():
                if emoji.id in used_destination_ids:
                    continue
                try:
                    await emoji.delete(reason=MIRROR_REASON)
                    deleted_destination_ids.add(emoji.id)
                    await self._mutation_pause()
                except (discord.Forbidden, discord.HTTPException) as error:
                    _log.error(
                        "Failed to delete unmapped sub-server emoji %s: %s",
                        emoji.id,
                        error,
                    )

        retained_emojis = [
            emoji
            for emoji in destination_emojis.values()
            if emoji.id not in deleted_destination_ids
        ]
        static_count = sum(not emoji.animated for emoji in retained_emojis)
        animated_count = sum(emoji.animated for emoji in retained_emojis)
        limit = destination_guild.emoji_limit
        full_slot_types: set[str] = set()

        for source_emoji, desired_roles in missing_source_emojis:
            current_count = animated_count if source_emoji.animated else static_count
            if current_count >= limit:
                slot_type = "animated" if source_emoji.animated else "static"
                if slot_type not in full_slot_types:
                    _log.info(
                        "No %s emoji slot is available in sub-server %s; remaining emojis will be retried later.",
                        slot_type,
                        destination_guild.id,
                    )
                    full_slot_types.add(slot_type)
                continue

            try:
                image = await source_emoji.read()
                destination_emoji = await destination_guild.create_custom_emoji(
                    name=source_emoji.name,
                    image=image,
                    roles=desired_roles,
                    reason=MIRROR_REASON,
                )
            except (discord.Forbidden, discord.HTTPException, OSError) as error:
                _log.error("Failed to copy emoji %s: %s", source_emoji.id, error)
                continue
            if source_emoji.animated:
                animated_count += 1
            else:
                static_count += 1
            emoji_map[source_emoji.id] = destination_emoji
            used_destination_ids.add(destination_emoji.id)
            await self._save_entity_mapping(
                destination_guild.id,
                "emoji",
                source_emoji.id,
                destination_emoji.id,
            )
            await self._mutation_pause(self.settings.emoji_create_delay_seconds)

        return emoji_map

    @staticmethod
    def _translate_emoji(
        source_emoji: discord.PartialEmoji | discord.Emoji | str | None,
        emoji_map: Mapping[int, discord.Emoji],
    ) -> discord.Emoji | str | None:
        if source_emoji is None or isinstance(source_emoji, str):
            return source_emoji
        if source_emoji.id is None:
            return source_emoji.name
        return emoji_map.get(source_emoji.id)

    def _translate_overwrites(
        self,
        source_channel: discord.abc.GuildChannel,
        destination_guild: discord.Guild,
        role_map: Mapping[int, discord.Role],
    ) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
        translated: dict[
            discord.Role | discord.Member, discord.PermissionOverwrite
        ] = {}
        for target, overwrite in source_channel.overwrites.items():
            if isinstance(target, discord.Role):
                destination_target = role_map.get(target.id)
            else:
                destination_target = destination_guild.get_member(target.id)
            if destination_target is not None:
                translated[destination_target] = overwrite
        return translated

    @staticmethod
    def _overwrite_signature(
        overwrites: Mapping[discord.Role | discord.Member, discord.PermissionOverwrite],
    ) -> set[tuple[bool, int, int, int]]:
        signature = set()
        for target, overwrite in overwrites.items():
            allow, deny = overwrite.pair()
            signature.add(
                (isinstance(target, discord.Role), target.id, allow.value, deny.value)
            )
        return signature

    def _forum_tags(
        self,
        source_channel: discord.ForumChannel,
        emoji_map: Mapping[int, discord.Emoji],
    ) -> list[discord.ForumTag]:
        return [
            discord.ForumTag(
                name=tag.name,
                moderated=tag.moderated,
                emoji=self._translate_emoji(tag.emoji, emoji_map),
            )
            for tag in source_channel.available_tags
        ]

    @staticmethod
    def _forum_tag_signature(tags: list[discord.ForumTag]) -> list[tuple[Any, ...]]:
        return [
            (
                tag.name,
                tag.moderated,
                getattr(tag.emoji, "id", None),
                getattr(tag.emoji, "name", tag.emoji),
            )
            for tag in tags
        ]

    @staticmethod
    def _channel_kind(channel: discord.abc.GuildChannel) -> str:
        channel_type = channel.type
        if channel_type is discord.ChannelType.category:
            return "category"
        if channel_type in (discord.ChannelType.text, discord.ChannelType.news):
            return "text"
        if channel_type is discord.ChannelType.voice:
            return "voice"
        if channel_type is discord.ChannelType.stage_voice:
            return "stage"
        if channel_type in (discord.ChannelType.forum, discord.ChannelType.media):
            return "forum"
        return f"unsupported:{channel_type.value}"

    async def _create_channel(
        self,
        source_channel: discord.abc.GuildChannel,
        destination_guild: discord.Guild,
        category: discord.CategoryChannel | None,
        overwrites: Mapping[discord.Role | discord.Member, discord.PermissionOverwrite],
        emoji_map: Mapping[int, discord.Emoji],
    ) -> discord.abc.GuildChannel | None:
        common = {
            "position": source_channel.position,
            "overwrites": overwrites,
            "reason": MIRROR_REASON,
        }
        kind = self._channel_kind(source_channel)
        try:
            if kind == "category":
                channel = await destination_guild.create_category(
                    source_channel.name,
                    **common,
                )
                if getattr(source_channel, "nsfw", False):
                    channel = (
                        await channel.edit(nsfw=True, reason=MIRROR_REASON) or channel
                    )
            elif kind == "text":
                channel = await destination_guild.create_text_channel(
                    source_channel.name,
                    category=category,
                    news=source_channel.type is discord.ChannelType.news,
                    topic=source_channel.topic,
                    slowmode_delay=source_channel.slowmode_delay,
                    nsfw=source_channel.nsfw,
                    default_auto_archive_duration=source_channel.default_auto_archive_duration,
                    default_thread_slowmode_delay=source_channel.default_thread_slowmode_delay,
                    **common,
                )
            elif kind in ("voice", "stage"):
                creator = (
                    destination_guild.create_stage_channel
                    if kind == "stage"
                    else destination_guild.create_voice_channel
                )
                channel = await creator(
                    source_channel.name,
                    category=category,
                    bitrate=int(
                        min(source_channel.bitrate, destination_guild.bitrate_limit)
                    ),
                    user_limit=source_channel.user_limit,
                    rtc_region=source_channel.rtc_region,
                    video_quality_mode=source_channel.video_quality_mode,
                    nsfw=source_channel.nsfw,
                    **common,
                )
            elif kind == "forum":
                is_media = source_channel.type is discord.ChannelType.media
                forum_kwargs: dict[str, Any] = {
                    "category": category,
                    "topic": source_channel.topic,
                    "slowmode_delay": source_channel.slowmode_delay,
                    "nsfw": source_channel.nsfw,
                    "default_auto_archive_duration": source_channel.default_auto_archive_duration,
                    "default_thread_slowmode_delay": source_channel.default_thread_slowmode_delay,
                    "available_tags": self._forum_tags(source_channel, emoji_map),
                    "media": is_media,
                    **common,
                }
                if source_channel.default_sort_order is not None:
                    forum_kwargs["default_sort_order"] = (
                        source_channel.default_sort_order
                    )
                default_reaction = self._translate_emoji(
                    source_channel.default_reaction_emoji, emoji_map
                )
                if default_reaction is not None:
                    forum_kwargs["default_reaction_emoji"] = default_reaction
                if not is_media:
                    forum_kwargs["default_layout"] = source_channel.default_layout
                channel = await destination_guild.create_forum(
                    source_channel.name,
                    **forum_kwargs,
                )
            else:
                _log.warning(
                    "Skipping unsupported main-guild channel type %s for channel %s.",
                    source_channel.type,
                    source_channel.id,
                )
                return None
        except (
            discord.Forbidden,
            discord.HTTPException,
            TypeError,
            ValueError,
        ) as error:
            _log.error(
                "Failed to create mirrored channel %s: %s", source_channel.id, error
            )
            return None
        await self._mutation_pause()
        return channel

    def _channel_needs_edit(
        self,
        source_channel: discord.abc.GuildChannel,
        destination_channel: discord.abc.GuildChannel,
        category: discord.CategoryChannel | None,
        overwrites: Mapping[discord.Role | discord.Member, discord.PermissionOverwrite],
        emoji_map: Mapping[int, discord.Emoji],
    ) -> bool:
        if source_channel.name != destination_channel.name:
            return True
        if source_channel.position != destination_channel.position:
            return True
        if self._overwrite_signature(overwrites) != self._overwrite_signature(
            destination_channel.overwrites
        ):
            return True
        if self._channel_kind(source_channel) == "category":
            return getattr(source_channel, "nsfw", False) != getattr(
                destination_channel, "nsfw", False
            )
        if getattr(destination_channel, "category_id", None) != getattr(
            category, "id", None
        ):
            return True
        kind = self._channel_kind(source_channel)
        if kind == "text":
            return any(
                (
                    source_channel.type != destination_channel.type,
                    source_channel.topic != destination_channel.topic,
                    source_channel.slowmode_delay != destination_channel.slowmode_delay,
                    source_channel.nsfw != destination_channel.nsfw,
                    source_channel.default_auto_archive_duration
                    != destination_channel.default_auto_archive_duration,
                    source_channel.default_thread_slowmode_delay
                    != destination_channel.default_thread_slowmode_delay,
                )
            )
        if kind in ("voice", "stage"):
            return any(
                (
                    int(
                        min(
                            source_channel.bitrate,
                            destination_channel.guild.bitrate_limit,
                        )
                    )
                    != destination_channel.bitrate,
                    source_channel.user_limit != destination_channel.user_limit,
                    source_channel.rtc_region != destination_channel.rtc_region,
                    source_channel.video_quality_mode
                    != destination_channel.video_quality_mode,
                    source_channel.nsfw != destination_channel.nsfw,
                    source_channel.slowmode_delay != destination_channel.slowmode_delay,
                )
            )
        if kind == "forum":
            desired_tags = self._forum_tags(source_channel, emoji_map)
            desired_reaction = self._translate_emoji(
                source_channel.default_reaction_emoji, emoji_map
            )
            return any(
                (
                    source_channel.type != destination_channel.type,
                    source_channel.topic != destination_channel.topic,
                    source_channel.slowmode_delay != destination_channel.slowmode_delay,
                    source_channel.nsfw != destination_channel.nsfw,
                    source_channel.default_auto_archive_duration
                    != destination_channel.default_auto_archive_duration,
                    source_channel.default_thread_slowmode_delay
                    != destination_channel.default_thread_slowmode_delay,
                    source_channel.default_sort_order
                    != destination_channel.default_sort_order,
                    source_channel.type is not discord.ChannelType.media
                    and source_channel.default_layout
                    != destination_channel.default_layout,
                    self._forum_tag_signature(desired_tags)
                    != self._forum_tag_signature(destination_channel.available_tags),
                    getattr(desired_reaction, "id", None)
                    != getattr(destination_channel.default_reaction_emoji, "id", None),
                    getattr(desired_reaction, "name", desired_reaction)
                    != getattr(
                        destination_channel.default_reaction_emoji,
                        "name",
                        destination_channel.default_reaction_emoji,
                    ),
                    source_channel.flags.require_tag
                    != destination_channel.flags.require_tag,
                )
            )
        return False

    async def _edit_channel(
        self,
        source_channel: discord.abc.GuildChannel,
        destination_channel: discord.abc.GuildChannel,
        category: discord.CategoryChannel | None,
        overwrites: Mapping[discord.Role | discord.Member, discord.PermissionOverwrite],
        emoji_map: Mapping[int, discord.Emoji],
    ) -> discord.abc.GuildChannel:
        if not self._channel_needs_edit(
            source_channel, destination_channel, category, overwrites, emoji_map
        ):
            return destination_channel

        kind = self._channel_kind(source_channel)
        kwargs: dict[str, Any] = {
            "name": source_channel.name,
            "position": source_channel.position,
            "overwrites": overwrites,
            "reason": MIRROR_REASON,
        }
        if kind == "category":
            kwargs["nsfw"] = getattr(source_channel, "nsfw", False)
        else:
            kwargs["category"] = category
        if kind == "text":
            kwargs.update(
                type=source_channel.type,
                topic=source_channel.topic,
                slowmode_delay=source_channel.slowmode_delay,
                nsfw=source_channel.nsfw,
                default_auto_archive_duration=source_channel.default_auto_archive_duration,
                default_thread_slowmode_delay=source_channel.default_thread_slowmode_delay,
            )
        elif kind in ("voice", "stage"):
            kwargs.update(
                bitrate=int(
                    min(
                        source_channel.bitrate,
                        destination_channel.guild.bitrate_limit,
                    )
                ),
                user_limit=source_channel.user_limit,
                rtc_region=source_channel.rtc_region,
                video_quality_mode=source_channel.video_quality_mode,
                nsfw=source_channel.nsfw,
                slowmode_delay=source_channel.slowmode_delay,
            )
        elif kind == "forum":
            kwargs.update(
                topic=source_channel.topic,
                slowmode_delay=source_channel.slowmode_delay,
                nsfw=source_channel.nsfw,
                default_auto_archive_duration=source_channel.default_auto_archive_duration,
                default_thread_slowmode_delay=source_channel.default_thread_slowmode_delay,
                default_sort_order=source_channel.default_sort_order,
                default_reaction_emoji=self._translate_emoji(
                    source_channel.default_reaction_emoji, emoji_map
                ),
                available_tags=self._forum_tags(source_channel, emoji_map),
                require_tag=source_channel.flags.require_tag,
            )
            if source_channel.type is not discord.ChannelType.media:
                kwargs["default_layout"] = source_channel.default_layout
        try:
            updated = await destination_channel.edit(**kwargs)
        except (
            discord.Forbidden,
            discord.HTTPException,
            TypeError,
            ValueError,
        ) as error:
            _log.error(
                "Failed to update mirrored channel %s: %s", source_channel.id, error
            )
            return destination_channel
        await self._mutation_pause()
        return updated or destination_channel

    async def _sync_channels(
        self,
        source_guild: discord.Guild,
        destination_guild: discord.Guild,
        role_map: Mapping[int, discord.Role],
        emoji_map: Mapping[int, discord.Emoji],
    ) -> dict[int, discord.abc.GuildChannel]:
        mapping_ids = await self._load_entity_map(destination_guild.id, "channel")
        channel_map: dict[int, discord.abc.GuildChannel] = {}
        used_destination_ids: set[int] = set()
        source_channels = list(source_guild.channels)
        source_ids = {channel.id for channel in source_channels}

        for stale_source_id, destination_id in mapping_ids.items():
            if stale_source_id in source_ids:
                continue
            stale_channel = destination_guild.get_channel(destination_id)
            if stale_channel is not None:
                try:
                    await stale_channel.delete(reason=MIRROR_REASON)
                    await self._mutation_pause()
                except (discord.Forbidden, discord.HTTPException) as error:
                    _log.error(
                        "Failed to delete stale mirrored channel %s: %s",
                        destination_id,
                        error,
                    )
                    continue
            await self._delete_entity_mapping(
                destination_guild.id, "channel", stale_source_id
            )

        ordered_sources = [
            *sorted(source_guild.categories, key=lambda channel: channel.position),
            *[
                channel
                for channel in source_channels
                if channel.type is not discord.ChannelType.category
            ],
        ]
        for source_channel in ordered_sources:
            kind = self._channel_kind(source_channel)
            if kind.startswith("unsupported:"):
                _log.warning(
                    "Skipping unsupported main-guild channel type %s for channel %s.",
                    source_channel.type,
                    source_channel.id,
                )
                continue
            source_category_id = getattr(source_channel, "category_id", None)
            category = (
                channel_map.get(source_category_id)
                if source_category_id is not None
                else None
            )
            overwrites = self._translate_overwrites(
                source_channel, destination_guild, role_map
            )
            mapped_channel_id = mapping_ids.get(source_channel.id)
            destination_channel = (
                destination_guild.get_channel(mapped_channel_id)
                if mapped_channel_id is not None
                else None
            )
            if destination_channel is not None and (
                self._channel_kind(destination_channel) != kind
                or (kind == "forum" and destination_channel.type != source_channel.type)
            ):
                try:
                    await destination_channel.delete(reason=MIRROR_REASON)
                    await self._mutation_pause()
                except (discord.Forbidden, discord.HTTPException) as error:
                    _log.error(
                        "Failed to replace wrong-type mirrored channel %s: %s",
                        destination_channel.id,
                        error,
                    )
                    continue
                destination_channel = None
                await self._delete_entity_mapping(
                    destination_guild.id, "channel", source_channel.id
                )

            if destination_channel is None:
                destination_channel = next(
                    (
                        channel
                        for channel in destination_guild.channels
                        if channel.id not in used_destination_ids
                        and channel.name == source_channel.name
                        and self._channel_kind(channel) == kind
                        and getattr(channel, "category_id", None)
                        == getattr(category, "id", None)
                    ),
                    None,
                )
            if destination_channel is None:
                destination_channel = await self._create_channel(
                    source_channel,
                    destination_guild,
                    category,
                    overwrites,
                    emoji_map,
                )
            else:
                destination_channel = await self._edit_channel(
                    source_channel,
                    destination_channel,
                    category,
                    overwrites,
                    emoji_map,
                )
            if destination_channel is None:
                continue
            channel_map[source_channel.id] = destination_channel
            used_destination_ids.add(destination_channel.id)
            await self._save_entity_mapping(
                destination_guild.id,
                "channel",
                source_channel.id,
                destination_channel.id,
            )

        if self.settings.delete_unmapped_channels:
            for channel in list(destination_guild.channels):
                if channel.id in used_destination_ids:
                    continue
                try:
                    await channel.delete(reason=MIRROR_REASON)
                    await self._mutation_pause()
                except (discord.Forbidden, discord.HTTPException) as error:
                    _log.error(
                        "Failed to delete unmapped sub-server channel %s: %s",
                        channel.id,
                        error,
                    )

        return channel_map

    async def _ensure_destination_community(
        self,
        source_guild: discord.Guild,
        destination_guild: discord.Guild,
        channel_map: Mapping[int, discord.abc.GuildChannel],
    ) -> tuple[discord.Guild, bool]:
        if (
            not self.settings.mirror_guild_settings
            or "COMMUNITY" not in source_guild.features
            or "COMMUNITY" in destination_guild.features
        ):
            return destination_guild, False

        source_rules = source_guild.rules_channel
        source_updates = source_guild.public_updates_channel
        rules_channel = (
            channel_map.get(source_rules.id) if source_rules is not None else None
        )
        updates_channel = (
            channel_map.get(source_updates.id) if source_updates is not None else None
        )
        if rules_channel is None or updates_channel is None:
            _log.warning(
                "Cannot enable Community in sub-server %s until the main guild's "
                "rules and public-updates channels have both been mirrored.",
                destination_guild.id,
            )
            return destination_guild, False

        try:
            await destination_guild.edit(
                community=True,
                rules_channel=rules_channel,
                public_updates_channel=updates_channel,
                reason=MIRROR_REASON,
            )
            await self._mutation_pause()
        except (
            discord.Forbidden,
            discord.HTTPException,
            TypeError,
            ValueError,
        ) as error:
            _log.error(
                "Failed to enable Community in mirrored sub-server %s: %s",
                destination_guild.id,
                error,
            )
            return destination_guild, False
        # Guild.edit returns a detached Guild instance whose channel/member caches are
        # incomplete. Keep using the connected cache object for the second channel pass.
        return destination_guild, True

    async def _sync_guild_settings(
        self,
        source_guild: discord.Guild,
        destination_guild: discord.Guild,
        channel_map: Mapping[int, discord.abc.GuildChannel],
    ) -> None:
        if not self.settings.mirror_guild_settings:
            return
        kwargs: dict[str, Any] = {}
        comparable = {
            "name": source_guild.name,
            "verification_level": source_guild.verification_level,
            "default_notifications": source_guild.default_notifications,
            "explicit_content_filter": source_guild.explicit_content_filter,
            "afk_timeout": source_guild.afk_timeout,
            "system_channel_flags": source_guild.system_channel_flags,
        }
        if "COMMUNITY" in destination_guild.features:
            comparable.update(
                description=source_guild.description,
                preferred_locale=source_guild.preferred_locale,
            )
        for edit_name, value in comparable.items():
            if getattr(destination_guild, edit_name) != value:
                kwargs[edit_name] = value

        source_icon = source_guild.icon
        destination_icon = destination_guild.icon
        if getattr(source_icon, "key", None) != getattr(destination_icon, "key", None):
            if (
                source_icon is not None
                and source_icon.is_animated()
                and "ANIMATED_ICON" not in destination_guild.features
            ):
                _log.info(
                    "The animated main-guild icon cannot be copied to sub-server %s at its current boost level.",
                    destination_guild.id,
                )
            else:
                try:
                    kwargs["icon"] = await source_icon.read() if source_icon else None
                except (discord.HTTPException, OSError) as error:
                    _log.warning("Could not read the main guild icon: %s", error)

        channel_settings = {
            "afk_channel": source_guild.afk_channel,
            "system_channel": source_guild.system_channel,
        }
        if "COMMUNITY" in destination_guild.features:
            channel_settings.update(
                rules_channel=source_guild.rules_channel,
                public_updates_channel=source_guild.public_updates_channel,
            )
        for name, source_channel in channel_settings.items():
            destination_channel = (
                channel_map.get(source_channel.id)
                if source_channel is not None
                else None
            )
            if getattr(destination_guild, name) != destination_channel:
                kwargs[name] = destination_channel

        if not kwargs:
            return
        try:
            await destination_guild.edit(reason=MIRROR_REASON, **kwargs)
            await self._mutation_pause()
        except (
            discord.Forbidden,
            discord.HTTPException,
            TypeError,
            ValueError,
        ) as error:
            _log.error(
                "Failed to mirror guild settings to %s: %s", destination_guild.id, error
            )

    async def _sync_member_roles_unlocked(
        self,
        source_member: discord.Member,
        destination_member: discord.Member,
        role_map: Mapping[int, discord.Role],
    ) -> None:
        desired_ids = {
            role_map[role.id].id
            for role in source_member.roles
            if role.id in role_map
            and not role_map[role.id].is_default()
            and not role_map[role.id].managed
            and role_map[role.id].is_assignable()
        }
        mirrored_roles = {
            role.id: role
            for source_role_id, role in role_map.items()
            if source_role_id != self.access_settings.main_guild_id
            and not role.managed
            and role.is_assignable()
        }
        current_ids = {role.id for role in destination_member.roles}
        to_add = [
            role
            for role_id, role in mirrored_roles.items()
            if role_id in desired_ids and role_id not in current_ids
        ]
        to_remove = [
            role
            for role_id, role in mirrored_roles.items()
            if role_id not in desired_ids and role_id in current_ids
        ]
        try:
            if to_add:
                await destination_member.add_roles(*to_add, reason=MIRROR_REASON)
                await self._mutation_pause()
            if to_remove:
                await destination_member.remove_roles(*to_remove, reason=MIRROR_REASON)
                await self._mutation_pause()
        except (discord.Forbidden, discord.HTTPException) as error:
            _log.error(
                "Failed to mirror roles for member %s in %s: %s",
                source_member.id,
                destination_member.guild.id,
                error,
            )

    async def _sync_all_member_roles(
        self,
        source_guild: discord.Guild,
        destination_guild: discord.Guild,
        role_map: Mapping[int, discord.Role],
    ) -> None:
        if not self.settings.mirror_member_roles:
            return
        for destination_member in list(destination_guild.members):
            if self.bot.user is not None and destination_member.id == self.bot.user.id:
                continue
            source_member = source_guild.get_member(destination_member.id)
            if source_member is not None:
                await self._sync_member_roles_unlocked(
                    source_member, destination_member, role_map
                )

    async def _sync_member_channel_overwrites_unlocked(
        self,
        source_member: discord.Member,
        destination_member: discord.Member,
        channel_map: Mapping[int, discord.abc.GuildChannel],
    ) -> None:
        for source_channel in source_member.guild.channels:
            destination_channel = channel_map.get(source_channel.id)
            if destination_channel is None:
                continue
            source_overwrite = source_channel.overwrites_for(source_member)
            destination_overwrite = destination_channel.overwrites_for(
                destination_member
            )
            source_allow, source_deny = source_overwrite.pair()
            destination_allow, destination_deny = destination_overwrite.pair()
            if source_allow == destination_allow and source_deny == destination_deny:
                continue
            overwrite = (
                source_overwrite if source_allow.value or source_deny.value else None
            )
            try:
                await destination_channel.set_permissions(
                    destination_member,
                    overwrite=overwrite,
                    reason=MIRROR_REASON,
                )
                await self._mutation_pause()
            except (discord.Forbidden, discord.HTTPException) as error:
                _log.error(
                    "Failed to mirror channel overwrite for member %s in channel %s: %s",
                    source_member.id,
                    destination_channel.id,
                    error,
                )

    async def _reconcile_guild(
        self,
        source_guild: discord.Guild,
        destination_guild: discord.Guild,
    ) -> None:
        self._validate_direction(source_guild, destination_guild)
        _log.info(
            "Starting one-way guild mirror reconciliation from %s to %s.",
            source_guild.id,
            destination_guild.id,
        )
        role_map = await self._sync_roles(source_guild, destination_guild)
        emoji_map = await self._sync_emojis(source_guild, destination_guild, role_map)
        channel_map = await self._sync_channels(
            source_guild,
            destination_guild,
            role_map,
            emoji_map,
        )
        destination_guild, community_enabled = await self._ensure_destination_community(
            source_guild, destination_guild, channel_map
        )
        if community_enabled:
            channel_map = await self._sync_channels(
                source_guild,
                destination_guild,
                role_map,
                emoji_map,
            )
        await self._sync_guild_settings(source_guild, destination_guild, channel_map)
        await self._sync_all_member_roles(source_guild, destination_guild, role_map)
        _log.info(
            "Completed one-way guild mirror reconciliation from %s to %s.",
            source_guild.id,
            destination_guild.id,
        )

    async def reconcile_all(self) -> None:
        if not self.settings.enabled:
            return
        async with self._mirror_lock:
            source_guild = self._get_main_guild()
            if source_guild is None:
                _log.error(
                    "Cannot mirror because main guild %s is unavailable.",
                    self.access_settings.main_guild_id,
                )
                return
            for destination_guild in self._get_sub_guilds():
                try:
                    await self._reconcile_guild(source_guild, destination_guild)
                except Exception:  # noqa: BLE001 - keep the recurring sync alive
                    _log.exception(
                        "Unexpected failure while mirroring main guild %s to sub-server %s.",
                        source_guild.id,
                        destination_guild.id,
                    )

    async def sync_member_roles(self, destination_member: discord.Member) -> None:
        if not self.settings.enabled or not self.settings.mirror_member_roles:
            return
        async with self._mirror_lock:
            source_guild = self._get_main_guild()
            if source_guild is None:
                return
            self._validate_direction(source_guild, destination_member.guild)
            source_member = source_guild.get_member(destination_member.id)
            if source_member is None:
                return
            mapping_ids = await self._load_entity_map(
                destination_member.guild.id, "role"
            )
            role_map = {source_guild.id: destination_member.guild.default_role}
            role_map.update(
                {
                    source_id: role
                    for source_id, destination_id in mapping_ids.items()
                    if (role := destination_member.guild.get_role(destination_id))
                    is not None
                }
            )
            await self._sync_member_roles_unlocked(
                source_member, destination_member, role_map
            )
            channel_mapping_ids = await self._load_entity_map(
                destination_member.guild.id, "channel"
            )
            channel_map = {
                source_id: channel
                for source_id, destination_id in channel_mapping_ids.items()
                if (channel := destination_member.guild.get_channel(destination_id))
                is not None
            }
            await self._sync_member_channel_overwrites_unlocked(
                source_member, destination_member, channel_map
            )

    def _schedule_reconcile(self) -> None:
        if not self.settings.enabled:
            return
        if (
            self._debounced_reconcile is not None
            and not self._debounced_reconcile.done()
        ):
            self._reconcile_requested = True
            return
        self._debounced_reconcile = asyncio.create_task(
            self._run_debounced_reconcile(),
            name="sub-server-mirror-debounce",
        )

    async def _run_debounced_reconcile(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.settings.event_debounce_seconds)
                self._reconcile_requested = False
                await self.reconcile_all()
                if not self._reconcile_requested:
                    return
        except asyncio.CancelledError:
            return

    def _schedule_member_sync(self, member: discord.Member) -> None:
        key = (member.guild.id, member.id)
        existing = self._member_sync_tasks.get(key)
        if existing is not None and not existing.done():
            self._member_sync_requested.add(key)
            return

        async def run() -> None:
            try:
                while True:
                    await asyncio.sleep(1)
                    self._member_sync_requested.discard(key)
                    await self.sync_member_roles(member)
                    if key not in self._member_sync_requested:
                        return
            except asyncio.CancelledError:
                return
            finally:
                self._member_sync_requested.discard(key)
                if self._member_sync_tasks.get(key) is asyncio.current_task():
                    self._member_sync_tasks.pop(key, None)

        self._member_sync_tasks[key] = asyncio.create_task(
            run(), name=f"sub-server-member-role-sync-{member.guild.id}-{member.id}"
        )

    @tasks.loop(minutes=30)
    async def periodic_reconcile(self) -> None:
        await self.reconcile_all()

    @periodic_reconcile.before_loop
    async def before_periodic_reconcile(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.settings.enabled and not self.periodic_reconcile.is_running():
            self.periodic_reconcile.start()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        if self._is_mirrored_guild_id(guild.id):
            self._schedule_reconcile()

    @commands.Cog.listener()
    async def on_guild_update(
        self, before: discord.Guild, after: discord.Guild
    ) -> None:
        if self._is_mirrored_guild_id(after.id):
            self._schedule_reconcile()

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        if self._is_mirrored_guild_id(role.guild.id):
            self._schedule_reconcile()

    @commands.Cog.listener()
    async def on_guild_role_update(
        self, before: discord.Role, after: discord.Role
    ) -> None:
        if self._is_mirrored_guild_id(after.guild.id):
            self._schedule_reconcile()

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if self._is_mirrored_guild_id(role.guild.id):
            self._schedule_reconcile()

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        if self._is_mirrored_guild_id(channel.guild.id):
            self._schedule_reconcile()

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        if self._is_mirrored_guild_id(after.guild.id):
            self._schedule_reconcile()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if self._is_mirrored_guild_id(channel.guild.id):
            self._schedule_reconcile()

    @commands.Cog.listener()
    async def on_guild_emojis_update(
        self,
        guild: discord.Guild,
        before: tuple[discord.Emoji, ...],
        after: tuple[discord.Emoji, ...],
    ) -> None:
        if self._is_mirrored_guild_id(guild.id):
            self._schedule_reconcile()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id in self.access_settings.sub_guild_ids:
            self._schedule_member_sync(member)

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if before.roles == after.roles:
            return
        if after.guild.id == self.access_settings.main_guild_id:
            for sub_guild in self._get_sub_guilds():
                destination_member = sub_guild.get_member(after.id)
                if destination_member is not None:
                    self._schedule_member_sync(destination_member)
        elif after.guild.id in self.access_settings.sub_guild_ids:
            self._schedule_member_sync(after)


async def setup(bot: TMWBot) -> None:
    await bot.add_cog(SubServerMirror(bot))
