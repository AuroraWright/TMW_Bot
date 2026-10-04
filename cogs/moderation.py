import asyncio
import os
from datetime import timedelta

import discord
import yaml
from discord import app_commands
from discord.ext import commands

from lib.bot import TMWBot

SETTINGS_PATH = (
    os.getenv("ALT_MODERATION_SETTINGS_PATH") or "config/moderation_settings.yml"
)
with open(SETTINGS_PATH, encoding="utf-8") as settings_file:
    moderation_settings = yaml.safe_load(settings_file)

CREATE_WARNINGS_TABLE = """
CREATE TABLE IF NOT EXISTS moderation_warnings (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    warning_count INTEGER NOT NULL CHECK (warning_count >= 0),
    PRIMARY KEY (guild_id, user_id)
);
"""
GET_WARNINGS = """
SELECT warning_count FROM moderation_warnings WHERE guild_id = ? AND user_id = ?;
"""
STORE_WARNINGS = """
INSERT INTO moderation_warnings (guild_id, user_id, warning_count) VALUES (?, ?, ?)
ON CONFLICT (guild_id, user_id) DO UPDATE SET warning_count = excluded.warning_count;
"""
RESET_WARNINGS = """
DELETE FROM moderation_warnings WHERE guild_id = ? AND user_id = ?;
"""


class Moderation(commands.Cog):
    def __init__(self, bot: TMWBot):
        self.bot = bot
        # Serialize commands and unban events so counter updates cannot race.
        self._lock = asyncio.Lock()

    async def cog_load(self):
        await self.bot.RUN(CREATE_WARNINGS_TABLE)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        settings = moderation_settings.get(interaction.guild_id)
        if settings is None:
            await interaction.response.send_message(
                "This command is not available in this server.", ephemeral=True
            )
            return False
        staff_role_ids = [settings["moderator_role_id"], settings["admin_role_id"]]
        if not any(role.id in staff_role_ids for role in interaction.user.roles):
            raise app_commands.MissingAnyRole(staff_role_ids)
        return True

    async def warning_count(self, guild_id: int, user_id: int) -> int:
        row = await self.bot.GET_ONE(GET_WARNINGS, (guild_id, user_id))
        return row[0] if row else 0

    @staticmethod
    def can_moderate(interaction: discord.Interaction, member: discord.Member) -> bool:
        return (
            member.id != interaction.user.id
            and member.id != interaction.guild.owner_id
            and (
                interaction.user.id == interaction.guild.owner_id
                or member.top_role < interaction.user.top_role
            )
        )

    @app_commands.command(
        name="warn", description="Warn a user and apply the warning penalty."
    )
    @app_commands.guilds(*moderation_settings)
    @app_commands.guild_only()
    @app_commands.describe(
        user="The user to warn.",
        warning_message="The warning message to send in a DM.",
        increment="Number of warnings to add (default: 1).",
    )
    async def warn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        warning_message: app_commands.Range[str, 1, 1800],
        increment: app_commands.Range[int, 1] = 1,
    ):
        await interaction.response.defer(ephemeral=True)
        if not warning_message.strip():
            await interaction.followup.send(
                "The warning message cannot be blank.", ephemeral=True
            )
            return
        if not self.can_moderate(interaction, user):
            await interaction.followup.send(
                "You cannot warn yourself, the server owner, or a member with an equal or higher role.",
                ephemeral=True,
            )
            return

        async with self._lock:
            count = await self.warning_count(interaction.guild_id, user.id) + increment
            reason = f"/warn by {interaction.user.id}: {warning_message}"[:512]
            dm_failed = False

            async def send_warning():
                nonlocal dm_failed
                try:
                    await user.send(
                        f"You have been warned in {interaction.guild.name}:\n{warning_message}",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    dm_failed = True

            try:
                if count >= 3:
                    # Deliver while we still share the guild with the member.
                    await send_warning()
                    await interaction.guild.ban(
                        user, reason=reason, delete_message_seconds=0
                    )
                    action = "banned"
                else:
                    duration = (
                        timedelta(minutes=10) if count == 1 else timedelta(weeks=1)
                    )
                    await user.timeout(duration, reason=reason)
                    action = (
                        "timed out for 10 minutes"
                        if count == 1
                        else "timed out for 1 week"
                    )
            except discord.HTTPException:
                await interaction.followup.send(
                    "Could not apply the warning penalty. Check my permissions and role hierarchy. "
                    "The warning count was not changed.",
                    ephemeral=True,
                )
                return

            await self.bot.RUN(STORE_WARNINGS, (interaction.guild_id, user.id, count))
            if count < 3:
                await send_warning()
            message = (
                f"{user.mention} now has {count} warning(s) and has been {action}."
            )
            if dm_failed:
                message += " The warning DM could not be delivered."
            await interaction.followup.send(
                message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )

    @app_commands.command(
        name="unwarn", description="Remove one warning and clear the user's timeout."
    )
    @app_commands.guilds(*moderation_settings)
    @app_commands.guild_only()
    @app_commands.describe(user="The user to remove a warning from.")
    async def unwarn(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer(ephemeral=True)
        async with self._lock:
            try:
                member = await interaction.guild.fetch_member(user.id)
            except discord.NotFound:
                member = None
            except discord.HTTPException:
                await interaction.followup.send(
                    "Could not fetch this member. Please try again.", ephemeral=True
                )
                return
            if member is not None:
                if not self.can_moderate(interaction, member):
                    await interaction.followup.send(
                        "You cannot unwarn yourself, the server owner, or a member with an equal or higher role.",
                        ephemeral=True,
                    )
                    return
                try:
                    await member.timeout(
                        None, reason=f"/unwarn by {interaction.user.id}"
                    )
                except discord.HTTPException:
                    await interaction.followup.send(
                        "Could not remove the timeout. Check my permissions and role hierarchy. "
                        "The warning count was not changed.",
                        ephemeral=True,
                    )
                    return
            count = max(0, await self.warning_count(interaction.guild_id, user.id) - 1)
            await self.bot.RUN(STORE_WARNINGS, (interaction.guild_id, user.id, count))
            await interaction.followup.send(
                f"{user.mention} now has {count} warning(s)."
                + (" Their timeout has been removed." if member is not None else ""),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        if guild.id in moderation_settings:
            async with self._lock:
                await self.bot.RUN(RESET_WARNINGS, (guild.id, user.id))


async def setup(bot: TMWBot):
    await bot.add_cog(Moderation(bot))
