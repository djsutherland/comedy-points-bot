from logging import getLogger

from discord.ext import commands

logger = getLogger(__name__)

# NOTE: probably doesn't work if you're replacing *with* a standard emoji (untested)
to_replace = {
    392450533052514305: {  # blankies
        755187566810234891: 750201290633773086,  # OofForPeopleWithoutNitro => oof
    },
    1100658430663995432: {  # dani
        1250189062510350509: 1341251702015266926,  # tux => peng
        "🐴": 1168645229541339216,
        "🧠": 1392392472532881553,
    },
}
been_replaced = {guild: set(reps.values()) for guild, reps in to_replace.items()}


class FixReacts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.guild_id not in to_replace or payload.user_id == self.bot.user.id:
            return

        emoji = payload.emoji
        emoji_id = emoji.id if emoji.is_custom_emoji() else emoji.name

        if emoji_id in (to_rep := to_replace[payload.guild_id]):
            channel = self.bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)

            replaced_emoji = self.bot.get_emoji(to_rep[emoji_id])
            logger.info(
                f"Replacing {emoji} with {replaced_emoji} on {message.jump_url}"
            )
            for reaction in message.reactions:
                if reaction.emoji == replaced_emoji:
                    if not reaction.normal_count:  # only has super reacts
                        await message.add_reaction(replaced_emoji)
                    break
            else:  # no reactions
                await message.add_reaction(replaced_emoji)
            await message.clear_reaction(emoji)

        elif emoji_id in been_replaced[payload.guild_id]:
            # someone else is reacting with the replaced emoji
            # if i had that reaction, time to kill it
            # note: this does *not* error if i didn't have that reaction yet
            channel = self.bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)

            logger.info(f"Removing my {payload.emoji} on {message.jump_url}")
            await message.remove_reaction(payload.emoji, self.bot.user)


async def setup(bot):
    await bot.add_cog(FixReacts(bot))
