import asyncio
import logging

import discord
from discord.ext import commands, tasks


logger = logging.getLogger(__name__)
LOOP_LAG_INTERVAL = 1.0
LOOP_LAG_WARN_THRESHOLD = 1.0


class ComedyPointsBot(commands.Bot):

    def __init__(self, **kwargs):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.guild_reactions = True
        intents.emojis_and_stickers = True
        intents.message_content = True

        super().__init__(
            intents=intents,
            command_prefix=commands.when_mentioned,
            strip_after_prefix=True,
            **kwargs,
        )
        self.initial_extensions = [
            "comedypoints.basics",
            "comedypoints.fix_reacts",
            "comedypoints.points",
            "comedypoints.privateperms",
            "comedypoints.ep_poster",
            "comedypoints.text_reacts",
        ]
        self._loop_lag_expected_at = None

    async def setup_hook(self):
        async with asyncio.TaskGroup() as tg:
            for ext in self.initial_extensions:
                tg.create_task(self.load_extension(ext))
        self.monitor_loop_lag.start()

    async def close(self):
        self.monitor_loop_lag.cancel()
        await super().close()

    @tasks.loop(seconds=LOOP_LAG_INTERVAL)
    async def monitor_loop_lag(self):
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._loop_lag_expected_at is not None:
            lag = now - self._loop_lag_expected_at
            if lag >= LOOP_LAG_WARN_THRESHOLD:
                logger.warning(
                    "Event loop lag detected: %.3fs late; gateway latency is %.3fs",
                    lag,
                    self.latency,
                )
        self._loop_lag_expected_at = now + LOOP_LAG_INTERVAL

    async def on_ready(self):
        logging.info(f"Logged in as {self.user} ({self.user.id})")
