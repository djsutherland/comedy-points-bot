import asyncio
from dataclasses import dataclass
from logging import getLogger
from typing import Set

import discord
from discord.ext import commands

logger = getLogger(__name__)

PANEL_TEXT = (
    "You’re welcome to join our private trans & non-binary channel by clicking join below. "
    "<:transbulba:665751401477046302> "
    "It's a private space for trans, enby, gender non-conforming, and questioning people to chat. "
    "Joining is fully confidential; other members won’t be able to see that you’re there."
)


@dataclass
class PrivatePermsConfig:
    panel_channel_id: int
    target_channel_id: int

    @property
    def join_custom_id(self):
        return f"privateperms:join:{self.target_channel_id}"

    @property
    def leave_custom_id(self):
        return f"privateperms:leave:{self.target_channel_id}"


SETUPS = (
    PrivatePermsConfig(  # blankies #come-iiiin and #da-gendersh
        panel_channel_id=647968771494903818,
        target_channel_id=795433326802108456,
    ),
    # PrivatePermsConfig(  # dani #bot-testing and #secret-place
    #     panel_channel_id=1198483653941006428,
    #     target_channel_id=1392342773440712756,
    # ),
)


class PrivatePermsJoinButton(discord.ui.Button):
    def __init__(self, custom_id):
        super().__init__(
            label="Join",
            style=discord.ButtonStyle.success,
            custom_id=custom_id,
        )

    async def callback(self, interaction):
        if self.view is None:
            return
        await self.view.cog.handle_join(interaction, self.view.target_channel_id)


class PrivatePermsLeaveButton(discord.ui.Button):
    def __init__(self, custom_id):
        super().__init__(
            label="Leave",
            style=discord.ButtonStyle.secondary,
            custom_id=custom_id,
        )

    async def callback(self, interaction):
        if self.view is None:
            return
        await self.view.cog.handle_leave(interaction, self.view.target_channel_id)


class PrivatePermsView(discord.ui.View):
    def __init__(self, cog, config):
        super().__init__(timeout=None)
        self.cog = cog
        self.target_channel_id = config.target_channel_id
        self.add_item(PrivatePermsJoinButton(config.join_custom_id))
        self.add_item(PrivatePermsLeaveButton(config.leave_custom_id))


class PrivatePerms(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.registered_targets: Set[int] = set()
        self.ensure_lock = asyncio.Lock()
        self.startup_panels_synced = False

    async def cog_load(self):
        for config in SETUPS:
            self.register_view(config)
        if self.bot.is_ready():
            async with self.ensure_lock:
                await self.ensure_panels()
                self.startup_panels_synced = True

    def register_view(self, config):
        if config.target_channel_id in self.registered_targets:
            return

        self.bot.add_view(PrivatePermsView(self, config))
        self.registered_targets.add(config.target_channel_id)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.startup_panels_synced:
            return

        async with self.ensure_lock:
            if self.startup_panels_synced:
                return

            await self.ensure_panels()
            self.startup_panels_synced = True

    async def send_ephemeral(self, interaction, message):
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def get_text_channel(self, channel_id):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                logger.exception(f"Couldn't fetch configured channel {channel_id}")
                return None

        if not isinstance(channel, discord.TextChannel):
            logger.warning(f"Configured channel {channel_id} is not a text channel")
            return None
        return channel

    async def get_bot_member(self, guild):
        member = guild.me or guild.get_member(self.bot.user.id)
        if member is not None:
            return member
        return await guild.fetch_member(self.bot.user.id)

    async def get_interaction_member(self, interaction):
        if interaction.guild is None:
            return None
        if isinstance(interaction.user, discord.Member):
            return interaction.user
        try:
            return await interaction.guild.fetch_member(interaction.user.id)
        except discord.NotFound:
            return None

    def get_target_channel(self, guild, target_channel_id):
        channel = guild.get_channel(target_channel_id)
        if channel is None:
            logger.warning(
                f"Couldn't find target channel {target_channel_id} in guild {guild.id}"
            )
        return channel

    def configured_guild_channels(self, guild):
        configs = []
        for config in SETUPS:
            panel_channel = guild.get_channel(config.panel_channel_id)
            target_channel = guild.get_channel(config.target_channel_id)
            if panel_channel is None or target_channel is None:
                continue
            configs.append((config, panel_channel, target_channel))
        return configs

    def panel_message_matches(self, message, config):
        if message.author.id != self.bot.user.id:
            return False

        custom_ids = set()
        for row in message.components:
            for component in getattr(row, "children", ()):
                custom_id = getattr(component, "custom_id", None)
                if custom_id is not None:
                    custom_ids.add(custom_id)
        return (
            config.join_custom_id in custom_ids and config.leave_custom_id in custom_ids
        )

    async def find_panel_message(self, panel_channel, config):
        try:
            async for message in panel_channel.history(limit=None):
                if self.panel_message_matches(message, config):
                    return message
        except discord.Forbidden:
            logger.exception(f"Couldn't read history in {panel_channel}")
        except discord.HTTPException:
            logger.exception(f"Discord rejected history lookup in {panel_channel}")
        return None

    async def ensure_panel(self, config, panel_channel, target_channel):
        bot_member = await self.get_bot_member(panel_channel.guild)
        panel_permissions = panel_channel.permissions_for(bot_member)
        if (
            not panel_permissions.view_channel
            or not panel_permissions.send_messages
            or not panel_permissions.read_message_history
        ):
            logger.warning(
                f"Missing panel channel permissions in {panel_channel} for private perms"
            )
            return (
                "error",
                f"{panel_channel.mention}: missing `View Channel`, `Send Messages`, "
                "or `Read Message History`.",
            )

        target_permissions = target_channel.permissions_for(bot_member)
        if not target_permissions.manage_channels:
            logger.warning(
                f"Missing manage channel permissions in {target_channel} for private perms"
            )
            return (
                "error",
                f"{target_channel.mention}: missing `Manage Channels`.",
            )

        existing_message = await self.find_panel_message(panel_channel, config)
        if existing_message is not None:
            logger.info(
                f"Found existing private perms panel for {target_channel} at "
                f"{existing_message.jump_url}"
            )
            return (
                "existing",
                f"{panel_channel.mention} -> {target_channel.mention}: existing panel "
                f"{existing_message.jump_url}",
            )

        try:
            message = await panel_channel.send(
                PANEL_TEXT,
                view=PrivatePermsView(self, config),
            )
        except discord.Forbidden:
            logger.exception(f"Couldn't post a private perms panel in {panel_channel}")
            return (
                "error",
                f"{panel_channel.mention}: I couldn't post the panel there.",
            )
        except discord.HTTPException:
            logger.exception(f"Discord rejected a panel post in {panel_channel}")
            return (
                "error",
                f"{panel_channel.mention}: Discord rejected the panel post.",
            )

        logger.info(
            f"Posted a new private perms panel in {panel_channel} for {target_channel}"
        )
        return (
            "created",
            f"{panel_channel.mention} -> {target_channel.mention}: posted new panel "
            f"{message.jump_url}",
        )

    async def ensure_panels(self, guild=None):
        results = []
        if guild is None:
            configured = []
            for config in SETUPS:
                panel_channel = await self.get_text_channel(config.panel_channel_id)
                target_channel = await self.get_text_channel(config.target_channel_id)
                if panel_channel is None or target_channel is None:
                    results.append(
                        (
                            "error",
                            f"Couldn't resolve channels for {config.target_channel_id}.",
                        )
                    )
                    continue
                configured.append((config, panel_channel, target_channel))
        else:
            configured = self.configured_guild_channels(guild)

        for config, panel_channel, target_channel in configured:
            results.append(
                await self.ensure_panel(config, panel_channel, target_channel)
            )
        return results

    async def abort(self, interaction):
        await self.send_ephemeral(
            interaction, "Sorry, something's broken here. DM a mod to fix your access!"
        )

    async def handle_join(self, interaction, target_channel_id):
        member = await self.get_interaction_member(interaction)
        if member is None:
            logger.warning(f"Couldn't get member for {interaction}")
            return await self.abort(interaction)

        channel = self.get_target_channel(interaction.guild, target_channel_id)
        if channel is None:
            logger.error(f"Couldn't get channel {target_channel_id}")
            return await self.abort(interaction)

        if channel.permissions_for(member).view_channel:
            return await self.send_ephemeral(
                interaction, f"You already have access to {channel.jump_url}"
            )

        overwrite = channel.overwrites_for(member)
        overwrite.view_channel = True
        try:
            await channel.set_permissions(member, overwrite=overwrite)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(f"Couldn't grant {member} access to {channel}")
            return await self.abort(interaction)

        logger.info(f"Granted {member} access to {channel} via button panel")
        await self.send_ephemeral(
            interaction, f"You now have access to {channel.jump_url} – welcome!"
        )

    async def handle_leave(self, interaction, target_channel_id):
        member = await self.get_interaction_member(interaction)
        if member is None:
            logger.warning(f"Couldn't get member for {interaction}")
            return await self.abort(interaction)

        channel = self.get_target_channel(interaction.guild, target_channel_id)
        if channel is None:
            logger.error(f"Couldn't get channel {target_channel_id}")
            return await self.abort(interaction)

        overwrite = channel.overwrites_for(member)
        if overwrite.view_channel is not True:
            if channel.permissions_for(member).view_channel:
                return await self.send_ephemeral(
                    interaction,
                    f"You have access to {channel.jump_url} through a role, not through "
                    "personal permission. I can't delete that.",
                )
            else:
                return await self.send_ephemeral(
                    interaction, "You can't leave a channel you're not in...."
                )

        overwrite.view_channel = None
        try:
            if overwrite.is_empty():
                await channel.set_permissions(member, overwrite=None)
            else:
                await channel.set_permissions(member, overwrite=overwrite)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                f"Couldn't remove {member}'s access to {channel} from a button click"
            )
            return await self.abort(interaction)

        logger.info(f"Removed {member}'s personal access override for {channel}")
        if channel.permissions_for(member).view_channel:
            return await self.send_ephemeral(
                interaction,
                "I removed your personal override, but you may still be able to see "
                f"{channel.jump_url} through a role.",
            )
        else:
            return await self.send_ephemeral(
                interaction, f"Your access to {channel.jump_url} has been removed."
            )

    @commands.group(hidden=True, invoke_without_command=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_channels=True)
    async def privateperms(self, ctx):
        await ctx.send("Available subcommands: `post`")

    @privateperms.command(name="post", hidden=True)
    async def privateperms_post(self, ctx):
        async with self.ensure_lock:
            results = await self.ensure_panels(guild=ctx.guild)

        if not results:
            await ctx.send("This server has no hardcoded private perms panels.")
            return

        await ctx.send("\n".join(message for _status, message in results))


async def setup(bot):
    await bot.add_cog(PrivatePerms(bot))
