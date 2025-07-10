from logging import getLogger

import discord
from discord.ext import commands

logger = getLogger(__name__)


SETUPS = {  # post id: (emoji, channel id)
    # blankies server, #da-gendersh with :transbulba:
    1104505525527392358: (
        discord.PartialEmoji.from_str("transbulba:665751401477046302"),
        795433326802108456,
    ),
    # dani server, #secret-place with :peng:
    1392343366297059448: (
        discord.PartialEmoji.from_str("peng:1341251702015266926"),
        1392342773440712756,
    ),
}


class PrivatePerms(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        try:
            emoji, channel_id = SETUPS[payload.message_id]
        except KeyError:
            return
        if payload.user_id == self.bot.user.id:
            return

        post_channel = self.bot.get_channel(payload.channel_id)
        message = await post_channel.fetch_message(payload.message_id)

        for reaction in message.reactions:
            if reaction.emoji == emoji:
                break
        else:  # no reactions with the right emoji; i should add it
            logger.warn(
                f"There were no reactions with {emoji} on {message.jump_url}; adding it"
            )
            await message.add_reaction(emoji)
            return

        channel = message.guild.get_channel(channel_id)

        found_me = False
        async for user in reaction.users():
            if user == self.bot.user:
                found_me = True
                continue

            if not isinstance(user, discord.Member):
                member = await channel.guild.fetch_member(user.id)
                if member is None:
                    # apparently they've left the server, just delete the react
                    logging.warn(f"Couldn't find Member for {user}; removing their reaction")
                    await message.remove_reaction(emoji, user)
                else:
                    user = member

            try:
                logging.warn(f"Trying to add {user}")
                await channel.set_permissions(user, view_channel=True)
                await message.remove_reaction(emoji, user)
            except discord.errors.Forbidden:
                # this might happen if someone with a higher role than me reacts
                # but let's try to handle the other people anyway
                logging.exception(
                    f"Permissions error."
                    f"Was trying to add {user} to {channel} based on {message.jump_url}"
                )
                continue

        if not found_me:
            logger.warn(
                f"I hadn't reacted with {emoji} to {message.jump_url}; doing that"
            )
            await message.add_reaction(emoji)

    @commands.command(hidden=True)
    @commands.is_owner()
    async def debug_perms(self, ctx):
        for _, channel_id in SETUPS.values():
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                await ctx.reply(f"Can't load channel {channel_id}")
            else:
                for who, override in channel.overwrites.items():
                    await ctx.reply(f"{channel.jump_url}: {who} - {override.pair()}")

async def setup(bot):
    await bot.add_cog(PrivatePerms(bot))
