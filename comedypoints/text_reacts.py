import datetime
from logging import getLogger
import os

import discord
from discord.ext import commands

logger = getLogger(__name__)

RECENCY_THRESH = datetime.timedelta(seconds=10)
UTC = datetime.timezone.utc

if os.environ.get("DEV_MODE"):
    STICKER_MAP = {"careful": 1417923796257738835}
else:
    STICKER_MAP = {"good": 1026120666182844536, "good.": 1026120666182844536}

class TextReacts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return

        # not going to bother with storing which have been reacted to because of this
        if datetime.datetime.now(datetime.UTC) - message.created_at > RECENCY_THRESH:
            return # missed our chance

        content = message.content.strip()
        if (sticker_id := STICKER_MAP.get(content)):
            sticker = self.bot.get_sticker(sticker_id)
            if sticker is None:
                sticker = await self.bot.fetch_sticker(sticker_id)
            await message.channel.send(stickers=[sticker])


async def setup(bot):
    await bot.add_cog(TextReacts(bot))
