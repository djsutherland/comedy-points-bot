import logging

import datetime
import itertools
import random

from discord.ext import commands

from .utils import LRUCache


START_OF_TIME = datetime.datetime(2025, 7, 1, tzinfo=datetime.timezone.utc)
VOTING_EMOJI_ID = 748975694540832848

HALLS_OF_FAME = {
    # guild id: channel/thread id
    1100658430663995432: 1388010970739376178,  # dani server
    392450533052514305: 762058324115587154,  # blankies
}
VOTES_THRESH = {
    1100658430663995432: 1,  # dani server
    392450533052514305: 8,  # blankies
}

PTS_PAIRS = {
    "no comedy points": 0.01,
    "half a comedy point": 0.1,
    "one comedy point": 5,
    "two comedy points": 3,
    "three comedy points": 2,
    "four comedy points": 2,
    "five comedy points": 2,
    "six comedy points": 2,
    "seven comedy points": 2,
    "eight comedy points": 2,
    "nine comedy points": 2,
    "ten comedy points": 2,
    "11 comedy points": 1,
    "12 comedy points": 1,
    "13 comedy points": 1,
    "14 comedy points": 1,
    "15 comedy points": 1,
    "16 comedy points": 1,
    "17 comedy points": 1,
    "18 comedy points": 1,
    "19 comedy points": 1,
    "20 comedy points": 1,
    "21 comedy points": 1,
    "22 comedy points": 1,
    "23 comedy points": 1,
    "24 comedy points": 1,
    "25 comedy points": 1,
    "26 comedy points": 1,
    "27 comedy points": 1,
    "28 comedy points": 1,
    "29 comedy points": 1,
    "30 comedy points": 1,
    "50 comedy points": 1,
    "one hundred comedy points": 1,
    "one thousand comedy points": 0.5,
    "ten thousand comedy points": 0.1,
    "one million comedy points": 0.01,
    "one billion comedy points": 0.001,
    "one trillion comedy points": 0.0001,
}
PTS_VALUES = list(PTS_PAIRS.keys())
PTS_CUM_WTS = list(itertools.accumulate(PTS_PAIRS.values()))


class Points(commands.Cog):
    def __init__(self, bot, cache_size=1024):
        self.bot = bot
        self._inducted_cache = LRUCache(cache_size)
        self._channel_is_private = LRUCache(cache_size)

    def channel_is_private(self, channel):
        if channel in self._channel_is_private:
            return self._channel_is_private[channel]

        everyone = channel.guild.default_role
        result = not channel.permissions_for(everyone).read_messages
        self._channel_is_private[channel] = result
        return result

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if getattr(payload.emoji, "id", 0) != VOTING_EMOJI_ID:
            return
        if payload.user_id == self.bot.user.id:
            return
        if payload.message_id in self._inducted_cache:
            return

        channel = self.bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        guild = message.guild

        if message.created_at < START_OF_TIME:
            return

        for reaction in message.reactions:
            if getattr(reaction.emoji, "id", 0) == VOTING_EMOJI_ID:
                break
        else:
            logging.warning(
                f"couldn't find voting reaction on {message.jump_url} "
                f"even though {payload.user_id} reacted"
            )
            return  # maybe quickly un-reacted...

        count = reaction.count
        voted_for_self = False
        async for user in reaction.users():
            if user == self.bot.user:
                logging.info(f"{message.jump_url} already inducted")
                # already inducted
                self._inducted_cache[payload.message_id] = True
                return

            if user == message.author:
                voted_for_self = True
                count -= 1

        if voted_for_self or (count >= VOTES_THRESH[guild.id]):
            (points,) = random.choices(PTS_VALUES, cum_weights=PTS_CUM_WTS)

            if voted_for_self:
                logging.info(f"{message.jump_url} getting demerited")
                await message.reply(
                    f"{message.author.mention}, "
                    f"you have been fined {points} for voting for yourself."
                )
            else:
                logging.info(f"inducting {message.jump_url}")
                hall = guild.get_channel_or_thread(HALLS_OF_FAME[guild.id])

                base = f"{message.author.mention} was awarded {points} for"
                if self.channel_is_private(channel):
                    induction = await hall.send(f"{base} {message.jump_url}.")
                else:
                    induction = await hall.send(f"{base}:")
                    await message.forward(hall)

                await message.reply(
                    f"{message.author.mention}, you have been awarded {points} "
                    f"({induction.jump_url})."
                )

            await message.add_reaction(payload.emoji)
            self._inducted_cache[payload.message_id] = True


async def setup(bot):
    await bot.add_cog(Points(bot))
