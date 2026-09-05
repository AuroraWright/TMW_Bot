import asyncio

import discord
from discord.ext import commands

from lib.bot import TMWBot

CREATE_STICKY_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS sticky_messages (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    original_message_id INTEGER NOT NULL,
    stickied_message_id INTEGER,
    PRIMARY KEY (guild_id, channel_id));"""

GET_STICKY_MESSAGE = """
SELECT original_message_id, stickied_message_id 
FROM sticky_messages 
WHERE guild_id = ? AND channel_id = ?;"""

UPDATE_STICKY_MESSAGE = """
INSERT INTO sticky_messages (guild_id, channel_id, original_message_id, stickied_message_id)
VALUES (?, ?, ?, ?)
ON CONFLICT (guild_id, channel_id) DO UPDATE SET
original_message_id = excluded.original_message_id,
stickied_message_id = excluded.stickied_message_id;"""

DELETE_STICKY_MESSAGE = """
DELETE FROM sticky_messages 
WHERE guild_id = ? AND channel_id = ?;"""

FETCH_LOCK = asyncio.Lock()


class StickyMessages(commands.Cog):
    def __init__(self, bot: TMWBot):
        self.bot = bot
        self._self_sticky_message_ids: set[int] = set()

    async def cog_load(self):
        await self.bot.RUN(CREATE_STICKY_MESSAGES_TABLE)

    async def _get_message(self, channel_id: int, message_id: int) -> discord.Message:
        channel = self.bot.get_channel(channel_id)
        if not channel:
            channel = await self.bot.fetch_channel(channel_id)
        message = discord.utils.get(self.bot.cached_messages, id=message_id)
        if not message:
            message = await channel.fetch_message(message_id)
        return message

    @staticmethod
    def _interaction_metadata(message: discord.Message):
        metadata = getattr(message, "interaction_metadata", None)
        if metadata is None:
            # ``interaction`` is retained for discord.py versions before 2.4.
            metadata = getattr(message, "interaction", None)
        return metadata

    def _is_sticky_command_message(
        self,
        message: discord.Message,
        interaction: discord.Interaction,
    ) -> bool:
        metadata = self._interaction_metadata(message)
        if metadata is None:
            return False
        if getattr(metadata, "id", None) == interaction.id:
            return True

        bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
        return (
            getattr(metadata, "name", None) == "sticky_last_message"
            and bot_user_id is not None
            and getattr(getattr(message, "author", None), "id", None) == bot_user_id
        )

    async def _send_sticky_message(
        self,
        channel: discord.abc.Messageable,
        original_message: discord.Message,
    ) -> discord.Message:
        sticky_message = await channel.send(
            f"📌 **Sticky Message:**\n\n{original_message.content}",
            embed=original_message.embeds[0] if original_message.embeds else None,
            files=[
                await attachment.to_file()
                for attachment in original_message.attachments
            ],
        )
        self._self_sticky_message_ids.add(sticky_message.id)
        return sticky_message

    @discord.app_commands.command(
        name="sticky_last_message",
        description="Make the last message sticky in this channel",
    )
    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(manage_messages=True)
    async def sticky_last_message(self, interaction: discord.Interaction):
        await interaction.response.defer()

        async with FETCH_LOCK:
            sticky_data = await self.bot.GET_ONE(
                GET_STICKY_MESSAGE,
                (interaction.guild_id, interaction.channel_id),
            )
            existing_sticky_id = sticky_data[1] if sticky_data else None

            last_message = None
            async for message in interaction.channel.history(limit=10):
                if (
                    message.id == existing_sticky_id
                    or message.id in self._self_sticky_message_ids
                    or self._is_sticky_command_message(message, interaction)
                ):
                    continue
                last_message = message
                break

            if last_message is None:
                await interaction.followup.send(
                    "No message found to make sticky.",
                    ephemeral=True,
                )
                return

            sticky_message = await self._send_sticky_message(
                interaction.channel,
                last_message,
            )

            await self.bot.RUN(
                UPDATE_STICKY_MESSAGE,
                (
                    interaction.guild_id,
                    interaction.channel_id,
                    last_message.id,
                    sticky_message.id,
                ),
            )

        await interaction.followup.send("Message has been made sticky!", ephemeral=True)

    @discord.app_commands.command(
        name="unsticky", description="Remove the sticky message from this channel"
    )
    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(manage_messages=True)
    async def unsticky(self, interaction: discord.Interaction):
        await interaction.response.defer()

        sticky_data = await self.bot.GET_ONE(
            GET_STICKY_MESSAGE, (interaction.guild_id, interaction.channel_id)
        )

        if not sticky_data:
            await interaction.followup.send(
                "No sticky message found in this channel!", ephemeral=True
            )
            return

        try:
            _, stickied_message_id = sticky_data
            message = await self._get_message(
                interaction.channel_id, stickied_message_id
            )
            await message.delete()
        except discord.NotFound:
            pass

        await self.bot.RUN(
            DELETE_STICKY_MESSAGE, (interaction.guild_id, interaction.channel_id)
        )

        await interaction.followup.send(
            "Sticky message has been removed!", ephemeral=True
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return
        if message.id in self._self_sticky_message_ids:
            self._self_sticky_message_ids.discard(message.id)
            return

        async with FETCH_LOCK:
            sticky_data = await self.bot.GET_ONE(
                GET_STICKY_MESSAGE,
                (message.guild.id, message.channel.id),
            )

            if not sticky_data:
                return

            original_message_id, old_sticky_id = sticky_data
            if message.id == old_sticky_id:
                return

            try:
                if old_sticky_id:
                    old_sticky = await self._get_message(
                        message.channel.id,
                        old_sticky_id,
                    )
                    await old_sticky.delete()
            except discord.NotFound:
                pass

            try:
                original_message = await self._get_message(
                    message.channel.id,
                    original_message_id,
                )

                new_sticky = await self._send_sticky_message(
                    message.channel,
                    original_message,
                )

                await self.bot.RUN(
                    UPDATE_STICKY_MESSAGE,
                    (
                        message.guild.id,
                        message.channel.id,
                        original_message_id,
                        new_sticky.id,
                    ),
                )

            except discord.NotFound:
                await self.bot.RUN(
                    DELETE_STICKY_MESSAGE,
                    (message.guild.id, message.channel.id),
                )


async def setup(bot):
    await bot.add_cog(StickyMessages(bot))
