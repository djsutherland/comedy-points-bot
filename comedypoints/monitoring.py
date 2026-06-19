import asyncio
from collections import Counter, deque
import logging
import math
import os
import time
from urllib.request import Request, urlopen

try:
    import resource
except ImportError:
    resource = None

from discord.ext import commands, tasks

logger = logging.getLogger(__name__)
LOOP_LAG_INTERVAL = 1.0
LOOP_LAG_WARN_THRESHOLD = 1.0
LOOP_LAG_SAMPLE_SIZE = 60
GATEWAY_LATENCY_INTERVAL = 5.0
GATEWAY_LATENCY_WARN_THRESHOLD = 10.0
GATEWAY_EVENT_WINDOW = 60.0
GATEWAY_EVENT_TOP_N = 8
DISCORD_GATEWAY_PROBE_URL = "https://discord.com/api/v10/gateway"
DISCORD_GATEWAY_PROBE_TIMEOUT = 5.0


class Monitoring(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._loop_lag_expected_at = None
        self._loop_lags = deque(maxlen=LOOP_LAG_SAMPLE_SIZE)
        self._gateway_latency_was_high = False
        self._gateway_events = deque()
        self._gateway_event_counts = Counter()

    async def cog_load(self):
        self.monitor_loop_lag.start()
        self.monitor_gateway_latency.start()
        logger.info(
            "Started loop lag and gateway latency monitors "
            "(lag warning at %.1fs, gateway warning at %.1fs)",
            LOOP_LAG_WARN_THRESHOLD,
            GATEWAY_LATENCY_WARN_THRESHOLD,
        )

    def cog_unload(self):
        self.monitor_loop_lag.cancel()
        self.monitor_gateway_latency.cancel()

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

    def prune_gateway_events(self, now=None):
        if now is None:
            now = asyncio.get_running_loop().time()

        cutoff = now - GATEWAY_EVENT_WINDOW
        while self._gateway_events and self._gateway_events[0][0] < cutoff:
            _timestamp, event_type = self._gateway_events.popleft()
            self._gateway_event_counts[event_type] -= 1
            if self._gateway_event_counts[event_type] <= 0:
                del self._gateway_event_counts[event_type]

    def gateway_event_summary(self):
        self.prune_gateway_events()
        total = sum(self._gateway_event_counts.values())
        if total == 0:
            return "no dispatch events in last 60s"

        top_events = ", ".join(
            f"{event_type}={count}"
            for event_type, count in self._gateway_event_counts.most_common(
                GATEWAY_EVENT_TOP_N
            )
        )
        return f"{total} dispatch events in last 60s; top: {top_events}"

    def fetch_discord_gateway(self):
        request = Request(
            DISCORD_GATEWAY_PROBE_URL,
            headers={"User-Agent": "comedy-points-bot/1.0"},
        )
        with urlopen(request, timeout=DISCORD_GATEWAY_PROBE_TIMEOUT) as response:
            response.read(1)
            return response.status

    async def time_discord_gateway_probe(self):
        start = time.perf_counter()
        try:
            status = await asyncio.to_thread(self.fetch_discord_gateway)
        except Exception as error:
            elapsed = time.perf_counter() - start
            return f"failed after {elapsed:.3f}s ({type(error).__name__}: {error})"

        elapsed = time.perf_counter() - start
        return f"{elapsed:.3f}s HTTP {status}"

    @commands.Cog.listener()
    async def on_socket_event_type(self, event_type):
        now = asyncio.get_running_loop().time()
        self._gateway_events.append((now, event_type))
        self._gateway_event_counts[event_type] += 1
        self.prune_gateway_events(now)

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
                    self.bot.latency,
                )
        self._loop_lag_expected_at = now + LOOP_LAG_INTERVAL

    @tasks.loop(seconds=GATEWAY_LATENCY_INTERVAL)
    async def monitor_gateway_latency(self):
        latency = self.bot.latency
        if not math.isfinite(latency):
            return

        if latency < GATEWAY_LATENCY_WARN_THRESHOLD:
            self._gateway_latency_was_high = False
            return

        if self._gateway_latency_was_high:
            return

        self._gateway_latency_was_high = True
        https_probe = await self.time_discord_gateway_probe()
        logger.warning(
            "High gateway latency detected: %.3fs; recent max loop lag %.3fs; "
            "load average %s; process usage %s; gateway events: %s; "
            "Discord HTTPS probe: %s",
            latency,
            self.recent_loop_lag(),
            self.load_average(),
            self.process_usage(),
            self.gateway_event_summary(),
            https_probe,
        )


async def setup(bot):
    await bot.add_cog(Monitoring(bot))
