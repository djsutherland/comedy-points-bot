# This file is based in part on
#    https://github.com/VioletCranberry/discord-rss-bot/blob/v0.2.1/discord_rss_bot/
# which is under the following license:
#
# MIT License
#
# Copyright (c) 2025 Fedor Zhdanov
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import asyncio
import datetime
from logging import getLogger
import os
from pathlib import Path

import discord
from discord.ext import commands, tasks
from reader import make_reader, ReaderError

logger = getLogger(__name__)

FEEDS = {
    os.environ.get("PATREON_RSS", "https://feeds.megaphone.fm/blank-check"),
    "https://feeds.megaphone.fm/THI7214278819",  # critical darlings
}
READER_DB_PATH = os.environ.get("READER_DB", str(Path(__file__).parent.parent / "rss-db.sqlite"))

if os.environ.get("DEV_MODE"):
    TARGET_CHANNEL = 1198483653941006428  # dani #bot-testing
else:
    TARGET_CHANNEL = 755516308355022970  # blankies #bot-testing-ground

START_OF_TIME = datetime.datetime(2026, 3, 12, tzinfo=datetime.timezone.utc)


async def run_in_thread(func, *args, default=None, **kwargs):
    """Runs a blocking reader task in a separate thread."""
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except ReaderError as error:
        logger.error("Error executing task: %s", error)
        return default


class EpPoster(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        logger.info("Initializing RSS reader")
        self.reader = make_reader(READER_DB_PATH)
        self.reader.set_tag((), ".reader.update", {"interval": 6, "jitter": 0.8})

        # make sure the reader lazy-init is done
        parser = getattr(self.reader, "_parser", None)
        if hasattr(parser, "_lazy_init"):
            parser._lazy_init()

    async def cog_load(self):
        await asyncio.gather(
            *[run_in_thread(self.reader.add_feed, url, exist_ok=True) for url in FEEDS]
        )
        curr = await run_in_thread(
            lambda: {feed.url for feed in self.reader.get_feeds()}
        )
        if to_del := curr - FEEDS:
            await asyncio.gather(
                *[run_in_thread(self.reader.delete_feed, url) for url in to_del]
            )
        self.check_feeds.start()

    async def cog_unload(self):
        self.check_feeds.cancel()

    @tasks.loop(minutes=2)
    async def check_feeds(self):
        logger.info("Running RSS updates")
        await run_in_thread(self.reader.update_feeds, scheduled=True)

        async with asyncio.TaskGroup() as tg:
            for i, entry in enumerate(
                (
                    entry
                    for entry in self.reader.get_entries(read=False)
                    if not entry.published or entry.published > START_OF_TIME
                )
            ):
                if i >= 5:
                    if i == 5:
                        logger.warning(
                            f"Got too many rss updates at once...bailing on rest"
                        )
                    tg.create_task(run_in_thread(self.reader.mark_entry_as_read, entry))
                else:
                    tg.create_task(self.post_entry(entry))

    async def post_entry(self, entry):
        channel = self.bot.get_channel(TARGET_CHANNEL) or (
            await self.bot.fetch_channel(TARGET_CHANNEL)
        )  # should only need to fetch at most once

        await channel.send(f"{entry.title}")
        await run_in_thread(self.reader.mark_entry_as_read, entry)


async def setup(bot):
    await bot.add_cog(EpPoster(bot))
