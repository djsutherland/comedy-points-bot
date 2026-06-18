import asyncio
from collections import deque
import logging
import math
import os

try:
    import resource
except ImportError:
    resource = None

import discord
from discord.ext import commands, tasks


logger = logging.getLogger(__name__)
LOOP_LAG_INTERVAL = 1.0
LOOP_LAG_WARN_THRESHOLD = 1.0
LOOP_LAG_SAMPLE_SIZE = 60
GATEWAY_LATENCY_INTERVAL = 5.0
GATEWAY_LATENCY_WARN_THRESHOLD = 10.0


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
        self._loop_lags = deque(maxlen=LOOP_LAG_SAMPLE_SIZE)
        self._gateway_latency_was_high = False

    async def setup_hook(self):
        async with asyncio.TaskGroup() as tg:
            for ext in self.initial_extensions:
                tg.create_task(self.load_extension(ext))
        self.monitor_loop_lag.start()
        self.monitor_gateway_latency.start()
        logger.info(
            "Started loop lag and gateway latency monitors "
            "(lag warning at %.1fs, gateway warning at %.1fs)",
            LOOP_LAG_WARN_THRESHOLD,
            GATEWAY_LATENCY_WARN_THRESHOLD,
        )

    async def close(self):
        self.monitor_loop_lag.cancel()
        self.monitor_gateway_latency.cancel()
        await super().close()

    def recent_loop_lag(self):
        return max(self._loop_lags, default=0.0)

    def load_average(self):
        try:
            load1, load5, load15 = os.getloadavg()
        except (AttributeError, OSError):
            return "unavailable"
        return f"{load1:.2f}/{load5:.2f}/{load15:.2f}"

    def process_usage(self):
        if resource is None:
            return "unavailable"

        usage = resource.getrusage(resource.RUSAGE_SELF)
        return (
            f"{usage.ru_utime:.3f}s user, "
            f"{usage.ru_stime:.3f}s system, "
            f"{usage.ru_maxrss} maxrss"
        )

    @tasks.loop(seconds=LOOP_LAG_INTERVAL)
    async def monitor_loop_lag(self):
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._loop_lag_expected_at is not None:
            lag = now - self._loop_lag_expected_at
            self._loop_lags.append(max(lag, 0.0))
            if lag >= LOOP_LAG_WARN_THRESHOLD:
                logger.warning(
                    "Event loop lag detected: %.3fs late; gateway latency is %.3fs",
                    lag,
                    self.latency,
                )
        self._loop_lag_expected_at = now + LOOP_LAG_INTERVAL

    @tasks.loop(seconds=GATEWAY_LATENCY_INTERVAL)
    async def monitor_gateway_latency(self):
        latency = self.latency
        if not math.isfinite(latency):
            return

        if latency < GATEWAY_LATENCY_WARN_THRESHOLD:
            self._gateway_latency_was_high = False
            return

        if self._gateway_latency_was_high:
            return

        self._gateway_latency_was_high = True
        logger.warning(
            "High gateway latency detected: %.3fs; recent max loop lag %.3fs; "
            "load average %s; process usage %s",
            latency,
            self.recent_loop_lag(),
            self.load_average(),
            self.process_usage(),
        )

    async def on_ready(self):
        logging.info(f"Logged in as {self.user} ({self.user.id})")
