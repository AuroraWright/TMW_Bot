"""Cog that enables certain roles to automatically receive other roles."""

import asyncio
import logging
import os

import discord
import yaml
from discord.ext import commands, tasks

from lib.bot import TMWBot

_log = logging.getLogger(__name__)

AUTO_RECEIVE_LOCK = asyncio.Lock()
AUTO_RECIEVE_SETTINGS_PATH = (
    os.getenv("ALT_AUTO_RECEIVE_SETTINGS_PATH") or "config/auto_receive.yml"
)


class AutoReceive(commands.Cog):
    def __init__(self, bot: TMWBot):
        self.bot = bot
        self.auto_receive_config: dict[int, dict[int, tuple[int, ...]]] = {}
        self._pending_role_additions: set[tuple[int, int, int]] = set()

    async def cog_load(self):
        self.load_settings()

    async def cog_unload(self):
        self.give_auto_roles.cancel()

    def load_settings(self):
        try:
            with open(AUTO_RECIEVE_SETTINGS_PATH, "r", encoding="utf-8") as file:
                settings = yaml.safe_load(file) or {}
            self.auto_receive_config = self._normalize_settings(settings)
        except FileNotFoundError:
            _log.warning(
                "Auto receive settings file not found: %s",
                AUTO_RECIEVE_SETTINGS_PATH,
            )
            self.auto_receive_config = {}
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            _log.exception("Failed to load auto receive settings")
            self.auto_receive_config = {}

    @staticmethod
    def _normalize_settings(settings) -> dict[int, dict[int, tuple[int, ...]]]:
        if not isinstance(settings, dict):
            raise TypeError("Auto receive settings must be a mapping")

        normalized = {}
        for guild_id, guild_settings in settings.items():
            if not isinstance(guild_settings, dict):
                raise TypeError(
                    f"Auto receive settings for guild {guild_id} must be a mapping"
                )

            normalized_rules = {}
            for role_to_have_id, configured_targets in guild_settings.items():
                if isinstance(configured_targets, list):
                    target_ids = configured_targets
                else:
                    target_ids = [configured_targets]

                normalized_targets = tuple(
                    dict.fromkeys(int(target_id) for target_id in target_ids)
                )
                if not normalized_targets:
                    raise ValueError(
                        f"Auto receive role {role_to_have_id} must have at least one target role"
                    )

                normalized_rules[int(role_to_have_id)] = normalized_targets

            normalized[int(guild_id)] = normalized_rules

        return normalized

    def _target_role_ids_for(self, member: discord.Member) -> tuple[int, ...]:
        guild_settings = self.auto_receive_config.get(member.guild.id, {})
        member_role_ids = {role.id for role in member.roles}
        target_role_ids = []

        for role_to_have_id, role_to_get_ids in guild_settings.items():
            if role_to_have_id not in member_role_ids:
                continue

            for role_to_get_id in role_to_get_ids:
                if role_to_get_id not in target_role_ids:
                    target_role_ids.append(role_to_get_id)

        return tuple(target_role_ids)

    async def _give_configured_roles(
        self,
        member: discord.Member,
        *,
        delay_seconds: float = 0,
    ) -> None:
        member_role_ids = {role.id for role in member.roles}

        for role_to_get_id in self._target_role_ids_for(member):
            pending_key = (member.guild.id, member.id, role_to_get_id)
            if role_to_get_id in member_role_ids:
                self._pending_role_additions.discard(pending_key)
                continue
            if pending_key in self._pending_role_additions:
                continue

            role_to_get = member.guild.get_role(role_to_get_id)
            if role_to_get is None:
                _log.warning(
                    "Auto receive target role %s was not found in guild %s",
                    role_to_get_id,
                    member.guild.id,
                )
                continue

            self._pending_role_additions.add(pending_key)
            try:
                if delay_seconds:
                    await asyncio.sleep(delay_seconds)
                await member.add_roles(
                    role_to_get,
                    reason="Configured automatic role assignment",
                )
                member_role_ids.add(role_to_get_id)
                _log.info(
                    "Added role %s to member %s in guild %s",
                    role_to_get.name,
                    member,
                    member.guild.name,
                )
            except (discord.Forbidden, discord.HTTPException):
                self._pending_role_additions.discard(pending_key)
                _log.exception(
                    "Failed to add role %s to member %s in guild %s",
                    role_to_get.name,
                    member,
                    member.guild.name,
                )

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.give_auto_roles.is_running():
            self.load_settings()
            self.give_auto_roles.start()

    @commands.Cog.listener()
    async def on_member_update(
        self,
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        before_role_ids = {role.id for role in before.roles}
        after_role_ids = {role.id for role in after.roles}
        if before_role_ids == after_role_ids:
            return

        for role_id in before_role_ids | after_role_ids:
            self._pending_role_additions.discard((after.guild.id, after.id, role_id))

        await self._give_configured_roles(after)

    @tasks.loop(minutes=15)
    async def give_auto_roles(self):
        async with AUTO_RECEIVE_LOCK:
            self.load_settings()

            for guild in self.bot.guilds:
                guild_settings = self.auto_receive_config.get(guild.id, {})
                members_to_check = {}

                for role_to_have_id in guild_settings:
                    role_to_have = guild.get_role(role_to_have_id)
                    if role_to_have is None:
                        _log.warning(
                            "Auto receive source role %s was not found in guild %s",
                            role_to_have_id,
                            guild.id,
                        )
                        continue

                    for member in role_to_have.members:
                        members_to_check[member.id] = member

                for member in members_to_check.values():
                    await self._give_configured_roles(member, delay_seconds=1)


async def setup(bot: TMWBot):
    await bot.add_cog(AutoReceive(bot))
