import asyncio
import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import discord
import yaml
from discord.ext import commands

from lib.bot import TMWBot

_log = logging.getLogger(__name__)

SUB_SERVER_SETTINGS_PATH = Path(
    os.getenv("ALT_SUB_SERVER_SETTINGS_PATH", "config/sub_server_settings.yml")
)

CREATE_MIRRORED_BANS_TABLE = """
CREATE TABLE IF NOT EXISTS sub_server_mirrored_bans (
    main_guild_id INTEGER NOT NULL,
    sub_guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (main_guild_id, sub_guild_id, user_id)
);"""

RECORD_MIRRORED_BAN = """
INSERT INTO sub_server_mirrored_bans (main_guild_id, sub_guild_id, user_id)
VALUES (?, ?, ?)
ON CONFLICT(main_guild_id, sub_guild_id, user_id) DO NOTHING;"""

DELETE_MIRRORED_BAN = """
DELETE FROM sub_server_mirrored_bans
WHERE main_guild_id = ? AND sub_guild_id = ? AND user_id = ?;"""

GET_MIRRORED_BANS = """
SELECT sub_guild_id, user_id
FROM sub_server_mirrored_bans
WHERE main_guild_id = ?;"""

HAS_MIRRORED_BAN = """
SELECT 1
FROM sub_server_mirrored_bans
WHERE main_guild_id = ? AND sub_guild_id = ? AND user_id = ?;"""


def _parse_sub_guild_id_map(
    configured_id_map: Any,
    sub_guild_ids: tuple[int, ...],
    setting_name: str,
    id_kind: str,
) -> dict[int, tuple[int, ...]]:
    if configured_id_map is None:
        return {}
    if not isinstance(configured_id_map, dict):
        raise TypeError(
            f"{setting_name} must be a mapping of guild IDs to {id_kind} IDs."
        )

    parsed: dict[int, tuple[int, ...]] = {}
    for configured_guild_id, configured_ids in configured_id_map.items():
        try:
            guild_id = int(configured_guild_id)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{setting_name} must contain integer sub-server IDs."
            ) from error
        if guild_id not in sub_guild_ids:
            raise ValueError(
                f"{setting_name} contains unconfigured sub-server {guild_id}."
            )

        id_values = (
            configured_ids if isinstance(configured_ids, list) else [configured_ids]
        )
        try:
            parsed_ids = tuple(dict.fromkeys(int(value) for value in id_values))
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{setting_name} must contain integer Discord {id_kind} IDs."
            ) from error
        if not parsed_ids:
            raise ValueError(
                f"{setting_name} for sub-server {guild_id} cannot be empty."
            )
        parsed[guild_id] = parsed_ids

    return parsed


@dataclass(frozen=True)
class SubServerSettings:
    main_guild_id: int
    sub_guild_ids: tuple[int, ...]
    required_role_ids_by_sub_guild: dict[int, tuple[int, ...]] | None = None
    exempt_role_ids_by_sub_guild: dict[int, tuple[int, ...]] | None = None
    mirrored_sub_guild_ids: tuple[int, ...] | None = None
    exempt_user_ids_by_sub_guild: dict[int, tuple[int, ...]] | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "SubServerSettings":
        try:
            main_guild_id = int(data["main_guild_id"])
            configured_sub_guild_ids = data["sub_guild_ids"]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "Sub-server settings require an integer main_guild_id and a "
                "sub_guild_ids list."
            ) from error

        if not isinstance(configured_sub_guild_ids, list):
            raise TypeError("sub_guild_ids must be a list of Discord guild IDs.")

        try:
            sub_guild_ids = tuple(
                dict.fromkeys(int(guild_id) for guild_id in configured_sub_guild_ids)
            )
        except (TypeError, ValueError) as error:
            raise ValueError("Every sub_guild_ids entry must be an integer.") from error

        if main_guild_id in sub_guild_ids:
            raise ValueError("The main guild cannot also be a sub-server.")

        required_role_ids_by_sub_guild = _parse_sub_guild_id_map(
            data.get("required_role_ids"),
            sub_guild_ids,
            "required_role_ids",
            "role",
        )
        exempt_role_ids_by_sub_guild = _parse_sub_guild_id_map(
            data.get("exempt_role_ids"),
            sub_guild_ids,
            "exempt_role_ids",
            "role",
        )
        exempt_user_ids_by_sub_guild = _parse_sub_guild_id_map(
            data.get("exempt_user_ids"),
            sub_guild_ids,
            "exempt_user_ids",
            "user",
        )

        mirrored_ids_data = data.get("mirrored_sub_guild_ids")
        if mirrored_ids_data is None:
            mirrored_sub_guild_ids = None
        else:
            if not isinstance(mirrored_ids_data, list):
                raise TypeError(
                    "mirrored_sub_guild_ids must be a list of Discord guild IDs."
                )
            try:
                mirrored_sub_guild_ids = tuple(
                    dict.fromkeys(int(guild_id) for guild_id in mirrored_ids_data)
                )
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "Every mirrored_sub_guild_ids entry must be an integer."
                ) from error
            unknown_mirrored_ids = set(mirrored_sub_guild_ids) - set(sub_guild_ids)
            if unknown_mirrored_ids:
                raise ValueError(
                    "mirrored_sub_guild_ids must only contain configured sub-server IDs."
                )

        return cls(
            main_guild_id=main_guild_id,
            sub_guild_ids=sub_guild_ids,
            required_role_ids_by_sub_guild=required_role_ids_by_sub_guild,
            exempt_role_ids_by_sub_guild=exempt_role_ids_by_sub_guild,
            exempt_user_ids_by_sub_guild=exempt_user_ids_by_sub_guild,
            mirrored_sub_guild_ids=mirrored_sub_guild_ids,
        )

    @property
    def mirror_guild_ids(self) -> tuple[int, ...]:
        """Return destinations that receive the structural mirror.

        Older configurations did not distinguish access-only destinations, so
        omitting ``mirrored_sub_guild_ids`` preserves the previous behavior.
        """
        if self.mirrored_sub_guild_ids is None:
            return self.sub_guild_ids
        return self.mirrored_sub_guild_ids

    def required_role_ids_for(self, sub_guild_id: int) -> tuple[int, ...]:
        return (self.required_role_ids_by_sub_guild or {}).get(sub_guild_id, ())

    def exempt_role_ids_for(self, sub_guild_id: int) -> tuple[int, ...]:
        return (self.exempt_role_ids_by_sub_guild or {}).get(sub_guild_id, ())

    def exempt_user_ids_for(self, sub_guild_id: int) -> tuple[int, ...]:
        return (self.exempt_user_ids_by_sub_guild or {}).get(sub_guild_id, ())


def load_sub_server_settings(path: Path) -> SubServerSettings:
    with path.open("r", encoding="utf-8") as settings_file:
        data = yaml.safe_load(settings_file)

    if not isinstance(data, dict):
        raise TypeError("Sub-server settings must be a YAML mapping.")
    return SubServerSettings.from_mapping(data)


sub_server_settings = load_sub_server_settings(SUB_SERVER_SETTINGS_PATH)


class AccessStatus(Enum):
    ELIGIBLE = "eligible"
    NOT_IN_MAIN_GUILD = "not_in_main_guild"
    BANNED_FROM_MAIN_GUILD = "banned_from_main_guild"
    MISSING_REQUIRED_ROLE = "missing_required_role"
    CANNOT_VERIFY = "cannot_verify"


class SubServerAccess(commands.Cog):
    def __init__(
        self,
        bot: TMWBot,
        settings: SubServerSettings = sub_server_settings,
    ):
        self.bot = bot
        self.settings = settings
        self._ban_sync_lock = asyncio.Lock()
        self._reconciliation_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        await self.bot.RUN(CREATE_MIRRORED_BANS_TABLE)

    def _get_main_guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.settings.main_guild_id)

    def _get_sub_guilds(self) -> list[discord.Guild]:
        return [
            guild
            for guild_id in self.settings.sub_guild_ids
            if (guild := self.bot.get_guild(guild_id)) is not None
        ]

    def _is_bot_user(self, user_id: int) -> bool:
        return self.bot.user is not None and user_id == self.bot.user.id

    def _has_access_exemption(self, member: discord.Member) -> bool:
        sub_guild_id = member.guild.id
        if member.id in self.settings.exempt_user_ids_for(sub_guild_id):
            return True
        exempt_role_ids = self.settings.exempt_role_ids_for(sub_guild_id)
        return any(role.id in exempt_role_ids for role in getattr(member, "roles", ()))

    async def _get_access_status(
        self,
        user_id: int,
        sub_guild_id: int | None = None,
    ) -> AccessStatus:
        main_guild = self._get_main_guild()
        if main_guild is None:
            _log.error(
                "Cannot verify sub-server access because main guild %s is unavailable.",
                self.settings.main_guild_id,
            )
            return AccessStatus.CANNOT_VERIFY

        main_member = main_guild.get_member(user_id)
        if main_member is None:
            try:
                main_member = await main_guild.fetch_member(user_id)
            except discord.NotFound:
                return await self._status_for_non_member(main_guild, user_id)
            except (discord.Forbidden, discord.HTTPException) as error:
                _log.warning(
                    "Could not verify whether user %s belongs to main guild %s: %s",
                    user_id,
                    main_guild.id,
                    error,
                )
                return AccessStatus.CANNOT_VERIFY

        required_role_ids = self.settings.required_role_ids_for(sub_guild_id or 0)
        if required_role_ids and not any(
            role.id in required_role_ids for role in main_member.roles
        ):
            return AccessStatus.MISSING_REQUIRED_ROLE

        return AccessStatus.ELIGIBLE

    async def _status_for_non_member(
        self,
        main_guild: discord.Guild,
        user_id: int,
    ) -> AccessStatus:
        try:
            await main_guild.fetch_ban(discord.Object(id=user_id))
        except discord.NotFound:
            return AccessStatus.NOT_IN_MAIN_GUILD
        except (discord.Forbidden, discord.HTTPException) as error:
            _log.warning(
                "Could not check whether non-member %s is banned from main guild %s: %s",
                user_id,
                main_guild.id,
                error,
            )
            return AccessStatus.NOT_IN_MAIN_GUILD
        return AccessStatus.BANNED_FROM_MAIN_GUILD

    async def _kick_from_sub_guild(
        self,
        sub_guild: discord.Guild,
        user_id: int,
        reason: str,
    ) -> None:
        try:
            await sub_guild.kick(discord.Object(id=user_id), reason=reason)
        except discord.NotFound:
            return
        except (discord.Forbidden, discord.HTTPException) as error:
            _log.error(
                "Failed to kick user %s from sub-server %s: %s",
                user_id,
                sub_guild.id,
                error,
            )

    async def _ban_from_sub_guild(
        self,
        sub_guild: discord.Guild,
        user_id: int,
        reason: str,
    ) -> bool:
        try:
            await sub_guild.ban(discord.Object(id=user_id), reason=reason)
        except (discord.Forbidden, discord.HTTPException) as error:
            _log.error(
                "Failed to ban user %s from sub-server %s: %s",
                user_id,
                sub_guild.id,
                error,
            )
            return False
        return True

    async def _unban_from_sub_guild(
        self,
        sub_guild: discord.Guild,
        user_id: int,
        reason: str,
    ) -> bool:
        try:
            await sub_guild.unban(discord.Object(id=user_id), reason=reason)
        except discord.NotFound:
            return True
        except (discord.Forbidden, discord.HTTPException) as error:
            _log.error(
                "Failed to unban user %s from sub-server %s: %s",
                user_id,
                sub_guild.id,
                error,
            )
            return False
        return True

    async def _record_mirrored_ban(
        self,
        sub_guild_id: int,
        user_id: int,
    ) -> None:
        await self.bot.RUN(
            RECORD_MIRRORED_BAN,
            (self.settings.main_guild_id, sub_guild_id, user_id),
        )

    async def _delete_mirrored_ban(
        self,
        sub_guild_id: int,
        user_id: int,
    ) -> None:
        await self.bot.RUN(
            DELETE_MIRRORED_BAN,
            (self.settings.main_guild_id, sub_guild_id, user_id),
        )

    async def _mirror_main_ban(
        self,
        sub_guild: discord.Guild,
        user_id: int,
        reason: str,
    ) -> None:
        if await self._ban_from_sub_guild(sub_guild, user_id, reason):
            await self._record_mirrored_ban(sub_guild.id, user_id)

    async def _remove_mirrored_ban(
        self,
        sub_guild: discord.Guild,
        user_id: int,
        reason: str,
        *,
        known_tracked: bool = False,
    ) -> None:
        if not known_tracked:
            tracked = await self.bot.GET_ONE(
                HAS_MIRRORED_BAN,
                (self.settings.main_guild_id, sub_guild.id, user_id),
            )
            if tracked is None:
                return

        if await self._unban_from_sub_guild(sub_guild, user_id, reason):
            await self._delete_mirrored_ban(sub_guild.id, user_id)

    async def _enforce_sub_member(self, member: discord.Member) -> None:
        if self._is_bot_user(member.id):
            return

        access_status = await self._get_access_status(member.id, member.guild.id)
        if access_status is AccessStatus.ELIGIBLE:
            return
        if access_status in {
            AccessStatus.NOT_IN_MAIN_GUILD,
            AccessStatus.MISSING_REQUIRED_ROLE,
        } and self._has_access_exemption(member):
            _log.info(
                "Allowing member %s to remain in sub-server %s because of an access exemption role.",
                member.id,
                member.guild.id,
            )
            return
        if access_status is AccessStatus.CANNOT_VERIFY:
            _log.warning(
                "Leaving user %s in sub-server %s because access could not be verified.",
                member.id,
                member.guild.id,
            )
            return
        if access_status is AccessStatus.BANNED_FROM_MAIN_GUILD:
            await self._mirror_main_ban(
                member.guild,
                member.id,
                "User is banned from the main server.",
            )
            return

        reasons = {
            AccessStatus.NOT_IN_MAIN_GUILD: "User is not in the main server.",
            AccessStatus.MISSING_REQUIRED_ROLE: (
                "User does not have the required main-server role."
            ),
        }
        await self._kick_from_sub_guild(
            member.guild,
            member.id,
            reasons[access_status],
        )

    async def _reconcile_sub_guild(self, sub_guild: discord.Guild) -> None:
        for member in list(sub_guild.members):
            await self._enforce_sub_member(member)

    async def _get_banned_user_ids(
        self,
        guild: discord.Guild,
    ) -> set[int] | None:
        try:
            return {ban_entry.user.id async for ban_entry in guild.bans(limit=None)}
        except (discord.Forbidden, discord.HTTPException) as error:
            _log.error(
                "Failed to retrieve bans for server %s: %s",
                guild.id,
                error,
            )
            return None

    async def synchronize_main_guild_bans(self) -> None:
        async with self._ban_sync_lock:
            await self._synchronize_main_guild_bans()

    async def _synchronize_main_guild_bans(self) -> None:
        sub_guilds = self._get_sub_guilds()
        if not sub_guilds:
            return

        main_guild = self._get_main_guild()
        if main_guild is None:
            _log.error(
                "Cannot synchronize bans because main guild %s is unavailable.",
                self.settings.main_guild_id,
            )
            return

        main_bans = await self._get_banned_user_ids(main_guild)
        if main_bans is None:
            return

        mirrored_bans = await self.bot.GET(
            GET_MIRRORED_BANS,
            (self.settings.main_guild_id,),
        )
        mirrored_bans_by_sub_guild: dict[int, set[int]] = {}
        for sub_guild_id, user_id in mirrored_bans:
            mirrored_bans_by_sub_guild.setdefault(sub_guild_id, set()).add(user_id)

        for sub_guild in sub_guilds:
            sub_guild_bans = await self._get_banned_user_ids(sub_guild)
            if sub_guild_bans is None:
                continue

            stale_mirrored_bans = (
                mirrored_bans_by_sub_guild.get(sub_guild.id, set()) - main_bans
            )
            for user_id in stale_mirrored_bans:
                if user_id in sub_guild_bans:
                    await self._remove_mirrored_ban(
                        sub_guild,
                        user_id,
                        "User is no longer banned from the main server.",
                        known_tracked=True,
                    )
                else:
                    await self._delete_mirrored_ban(sub_guild.id, user_id)

            for user_id in main_bans - sub_guild_bans:
                await self._mirror_main_ban(
                    sub_guild,
                    user_id,
                    "User is banned from the main server.",
                )

            for user_id in main_bans & sub_guild_bans:
                await self._record_mirrored_ban(sub_guild.id, user_id)

    async def reconcile_all_sub_guilds(self) -> None:
        async with self._reconciliation_lock:
            configured_guilds = set(self.settings.sub_guild_ids)
            sub_guilds = self._get_sub_guilds()
            available_guilds = {guild.id for guild in sub_guilds}
            for missing_guild_id in configured_guilds - available_guilds:
                _log.warning(
                    "Configured sub-server %s is unavailable to the bot.",
                    missing_guild_id,
                )

            for sub_guild in sub_guilds:
                await self._reconcile_sub_guild(sub_guild)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.synchronize_main_guild_bans()
        await self.reconcile_all_sub_guilds()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id == self.settings.main_guild_id:
            await self.synchronize_main_guild_bans()
            await self.reconcile_all_sub_guilds()
        elif guild.id in self.settings.sub_guild_ids:
            await self.synchronize_main_guild_bans()
            await self._reconcile_sub_guild(guild)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id in self.settings.sub_guild_ids:
            await self._enforce_sub_member(member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild.id != self.settings.main_guild_id:
            return
        if self._is_bot_user(member.id):
            return

        for sub_guild in self._get_sub_guilds():
            get_destination_member = getattr(sub_guild, "get_member", None)
            destination_member = (
                get_destination_member(member.id)
                if callable(get_destination_member)
                else None
            )
            if member.id in self.settings.exempt_user_ids_for(sub_guild.id) or (
                destination_member is not None
                and self._has_access_exemption(destination_member)
            ):
                _log.info(
                    "Allowing member %s to remain in sub-server %s because of an access exemption role.",
                    member.id,
                    sub_guild.id,
                )
                continue
            await self._kick_from_sub_guild(
                sub_guild,
                member.id,
                "User left or was removed from the main server.",
            )

    @commands.Cog.listener()
    async def on_member_ban(
        self,
        guild: discord.Guild,
        user: discord.User | discord.Member,
    ) -> None:
        if guild.id != self.settings.main_guild_id:
            return
        if self._is_bot_user(user.id):
            return

        async with self._ban_sync_lock:
            for sub_guild in self._get_sub_guilds():
                await self._mirror_main_ban(
                    sub_guild,
                    user.id,
                    "User was banned from the main server.",
                )

    @commands.Cog.listener()
    async def on_member_unban(
        self,
        guild: discord.Guild,
        user: discord.User,
    ) -> None:
        if guild.id != self.settings.main_guild_id:
            return
        if self._is_bot_user(user.id):
            return

        async with self._ban_sync_lock:
            for sub_guild in self._get_sub_guilds():
                await self._remove_mirrored_ban(
                    sub_guild,
                    user.id,
                    "User was unbanned from the main server.",
                )

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        if after.guild.id != self.settings.main_guild_id:
            return
        if before.roles == after.roles:
            return

        for sub_guild in self._get_sub_guilds():
            destination_member = sub_guild.get_member(after.id)
            if destination_member is not None:
                await self._enforce_sub_member(destination_member)


async def setup(bot: TMWBot) -> None:
    await bot.add_cog(SubServerAccess(bot))
