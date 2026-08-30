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
from concurrent.futures import ThreadPoolExecutor
import datetime
from dataclasses import dataclass, field
import functools
from html import unescape
from html.parser import HTMLParser
from logging import getLogger
import os
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from reader import make_reader, ReaderError

logger = getLogger(__name__)

FEEDS = {
    os.environ.get("PATREON_RSS", "https://feeds.megaphone.fm/blank-check"),
    # "https://feeds.megaphone.fm/THI7214278819",  # critical darlings
}
READER_DB_PATH = os.environ.get(
    "READER_DB", str(Path(__file__).parent.parent / "rss-db.sqlite")
)

if os.environ.get("DEV_MODE"):
    TARGET_CHANNEL = 1198483653941006428  # dani #bot-testing
    TARGET_ROLE = 1484422590885007430
else:
    # TARGET_CHANNEL = 755516308355022970  # blankies #bot-testing-ground
    TARGET_CHANNEL = 829052560085352458  # blankies #blank-check-podcast
    TARGET_ROLE = 795408027883929601

START_OF_TIME = datetime.datetime(2026, 3, 16, tzinfo=datetime.timezone.utc)
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
FEED_FETCH_TIMEOUT = 15

NY = ZoneInfo("America/New_York")
SURPRISE_DROP_POLL_MINUTES = 15
EXPECTED_EPISODE_DAYS_OF_MONTH = {1, 11, 21}
EXPECTED_EPISODE_START_TIME = datetime.time(hour=0, second=1, tzinfo=NY)
EXPECTED_EPISODE_WINDOW = datetime.timedelta(minutes=10)
EXPECTED_EPISODE_POLL_SECONDS = 12


def _format_log_datetime(value) -> str:
    if value is None:
        return "none"
    return value.isoformat()


def _entry_first_seen(entry):
    return getattr(entry, "first_updated", None) or getattr(entry, "added", None)


def _safe_log_text(value, limit: int = 240) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    for feed_url in FEEDS:
        text = text.replace(feed_url, "<feed-url-redacted>")
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


@dataclass(frozen=True)
class FeedItemMetadata:
    id: str
    title: str | None = None
    link: str | None = None
    author: str | None = None
    summary: str | None = None
    content_html: str | None = None
    image_url: str | None = None
    duration_seconds: int | None = None
    episode_type: str | None = None


@dataclass(frozen=True)
class FeedMetadata:
    title: str | None = None
    link: str | None = None
    image_url: str | None = None
    items: tuple[FeedItemMetadata, ...] = field(default_factory=tuple)


async def run_blocking(func, *args, executor=None, **kwargs):
    call = functools.partial(func, *args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, call)


async def run_in_thread(func, *args, default=None, executor=None, **kwargs):
    """Runs a blocking reader task in a separate thread."""
    try:
        return await run_blocking(func, *args, executor=executor, **kwargs)
    except ReaderError as error:
        logger.error(
            "Reader operation failed error_type=%s error=%s",
            type(error).__name__,
            _safe_log_text(error),
        )
        return default


def _make_initialized_reader(db_path: str):
    reader = make_reader(db_path)
    parser = getattr(reader, "_parser", None)
    getattr(parser, "_lazy_init", lambda: None)()
    return reader


class EpPoster(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._feed_cache = {}
        self._feed_cache_locks = {}
        self._update_lock = asyncio.Lock()
        self._reader_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="rss-reader"
        )
        self.reader = None
        self._poll_sequence = 0
        self._expecting_episode = False
        self._episode_posted_event = asyncio.Event()
        self._last_episode_posted_at = None
        self._episode_posts_by_date = {}
        self._startup_expectation_task = None
        self._feed_labels = {
            feed_url: f"feed-{index}"
            for index, feed_url in enumerate(sorted(FEEDS), start=1)
        }

    async def _run_reader(self, operation, *, default=None):
        reader = self.reader
        if reader is None:
            raise RuntimeError("RSS reader is not initialized")
        return await run_in_thread(
            operation, reader, default=default, executor=self._reader_executor
        )

    async def _run_reader_method(self, method_name, *args, default=None, **kwargs):
        def operation(reader):
            return getattr(reader, method_name)(*args, **kwargs)

        return await self._run_reader(operation, default=default)

    async def cog_load(self):
        logger.info("Initializing RSS reader")
        self.reader = await run_blocking(
            _make_initialized_reader, READER_DB_PATH, executor=self._reader_executor
        )
        # self.reader.set_tag((), ".reader.update", {"interval": 6, "jitter": 0.8})

        await asyncio.gather(
            *[self._run_reader_method("add_feed", url, exist_ok=True) for url in FEEDS]
        )
        feeds = await self._run_reader(lambda reader: list(reader.get_feeds()))
        curr = {feed.url for feed in feeds}
        if to_del := curr - FEEDS:
            await asyncio.gather(
                *[self._run_reader_method("delete_feed", url) for url in to_del]
            )
        logger.info(
            "RSS reader initialized feeds=%d fallback_minutes=%d "
            "expected_start_et=%s expected_window_minutes=%.0f "
            "expected_poll_seconds=%d",
            len(curr),
            SURPRISE_DROP_POLL_MINUTES,
            EXPECTED_EPISODE_START_TIME.isoformat(),
            EXPECTED_EPISODE_WINDOW.total_seconds() / 60,
            EXPECTED_EPISODE_POLL_SECONDS,
        )
        self.check_feeds.start()
        self.expect_episode.start()
        self.clear_feed_caches.start()
        self._startup_expectation_task = asyncio.create_task(
            self._run_startup_expectation(),
            name="rss-startup-episode-expectation",
        )

    async def cog_unload(self):
        self.check_feeds.cancel()
        self.expect_episode.cancel()
        self.clear_feed_caches.cancel()
        if self._startup_expectation_task is not None:
            self._startup_expectation_task.cancel()
        self._reader_executor.shutdown(wait=False, cancel_futures=True)

    @tasks.loop(hours=24)
    async def clear_feed_caches(self):
        logger.info(
            "Clearing RSS metadata cache entries=%d at_utc=%s",
            len(self._feed_cache),
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        self._feed_cache.clear()

    @commands.command("rss", hidden=True)
    async def _do_rss(self, ctx):
        await self._check_feeds(trigger="manual")
        await ctx.message.add_reaction("\N{WHITE HEAVY CHECK MARK}")

    @tasks.loop(minutes=SURPRISE_DROP_POLL_MINUTES)
    async def check_feeds(self):
        if self._expecting_episode:
            logger.info("RSS fallback poll skipped reason=episode-watcher-active")
            return
        try:
            await self._check_feeds(trigger="fallback")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "RSS fallback poll failed error_type=%s error=%s",
                type(error).__name__,
                _safe_log_text(error, limit=1000),
            )

    @check_feeds.before_loop
    async def before_check_feeds(self):
        await self.bot.wait_until_ready()

    @check_feeds.error
    async def check_feeds_error(self, error):
        logger.error(
            "RSS fallback polling loop stopped error_type=%s error=%s",
            type(error).__name__,
            _safe_log_text(error, limit=1000),
        )

    @tasks.loop(time=EXPECTED_EPISODE_START_TIME)
    async def expect_episode(self):
        await self._expect_episode(trigger="scheduled")

    @expect_episode.before_loop
    async def before_expect_episode(self):
        await self.bot.wait_until_ready()

    @expect_episode.error
    async def expect_episode_error(self, error):
        logger.error(
            "RSS expected-episode loop stopped error_type=%s error=%s",
            type(error).__name__,
            _safe_log_text(error, limit=1000),
        )

    async def _run_startup_expectation(self):
        await self.bot.wait_until_ready()
        await self._expect_episode(trigger="startup")

    def _expected_episode_reasons(self, date: datetime.date) -> tuple[str, ...]:
        reasons = []
        if date.weekday() == 6:
            reasons.append("sunday-public")
        if date.day in EXPECTED_EPISODE_DAYS_OF_MONTH:
            reasons.append(f"patreon-day-{date.day}")
        return tuple(reasons)

    async def _expect_episode(self, *, trigger: str):
        now_et = datetime.datetime.now(NY)
        episode_date = now_et.date()
        reasons = self._expected_episode_reasons(episode_date)
        if not reasons:
            logger.info(
                "RSS episode watcher not needed trigger=%s date_et=%s",
                trigger,
                episode_date.isoformat(),
            )
            return
        reason = ",".join(reasons)
        expected_posts = len(reasons)

        window_start = datetime.datetime.combine(
            episode_date, EXPECTED_EPISODE_START_TIME
        )
        window_end = window_start + EXPECTED_EPISODE_WINDOW
        if now_et >= window_end:
            logger.info(
                "RSS episode watcher outside window trigger=%s reason=%s "
                "at_et=%s window_end_et=%s",
                trigger,
                reason,
                now_et.isoformat(),
                window_end.isoformat(),
            )
            return

        if self._expecting_episode:
            logger.info(
                "RSS episode watcher already active trigger=%s reason=%s",
                trigger,
                reason,
            )
            return

        self._expecting_episode = True
        try:
            if now_et < window_start:
                wait_seconds = (window_start - now_et).total_seconds()
                logger.info(
                    "RSS episode watcher waiting for window trigger=%s reason=%s "
                    "wait_seconds=%.1f",
                    trigger,
                    reason,
                    wait_seconds,
                )
                await asyncio.sleep(wait_seconds)

            already_posted = self._episode_posts_by_date.get(episode_date, 0)
            if already_posted >= expected_posts:
                logger.info(
                    "RSS episode watcher found expected posts already sent "
                    "trigger=%s reason=%s posted=%d expected=%d",
                    trigger,
                    reason,
                    already_posted,
                    expected_posts,
                )
                return

            self._episode_posted_event.clear()
            logger.info(
                "RSS episode watcher starting trigger=%s reason=%s "
                "window_end_et=%s poll_seconds=%d posted=%d expected=%d",
                trigger,
                reason,
                window_end.isoformat(),
                EXPECTED_EPISODE_POLL_SECONDS,
                already_posted,
                expected_posts,
            )

            attempt = 0
            while datetime.datetime.now(NY) < window_end:
                attempt += 1
                try:
                    await self._check_feeds(
                        trigger=f"expected:{reason}:attempt-{attempt}"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.error(
                        "RSS episode watcher poll failed trigger=%s reason=%s "
                        "attempt=%d error_type=%s error=%s",
                        trigger,
                        reason,
                        attempt,
                        type(error).__name__,
                        _safe_log_text(error, limit=1000),
                    )

                posted_count = self._episode_posts_by_date.get(episode_date, 0)
                if posted_count >= expected_posts:
                    logger.info(
                        "RSS episode watcher succeeded trigger=%s reason=%s "
                        "attempt=%d posted=%d expected=%d posted_at=%s",
                        trigger,
                        reason,
                        attempt,
                        posted_count,
                        expected_posts,
                        _format_log_datetime(self._last_episode_posted_at),
                    )
                    return

                remaining = (window_end - datetime.datetime.now(NY)).total_seconds()
                if remaining <= 0:
                    break
                self._episode_posted_event.clear()
                try:
                    await asyncio.wait_for(
                        self._episode_posted_event.wait(),
                        timeout=min(EXPECTED_EPISODE_POLL_SECONDS, remaining),
                    )
                except TimeoutError:
                    pass
                else:
                    posted_count = self._episode_posts_by_date.get(episode_date, 0)
                    logger.info(
                        "RSS episode watcher observed post between polls "
                        "trigger=%s reason=%s attempt=%d posted=%d expected=%d",
                        trigger,
                        reason,
                        attempt,
                        posted_count,
                        expected_posts,
                    )

            logger.warning(
                "RSS episode watcher timed out trigger=%s reason=%s attempts=%d "
                "posted=%d expected=%d window_end_et=%s",
                trigger,
                reason,
                attempt,
                self._episode_posts_by_date.get(episode_date, 0),
                expected_posts,
                window_end.isoformat(),
            )
        finally:
            self._expecting_episode = False

    async def _check_feeds(self, *, trigger: str):
        self._poll_sequence += 1
        poll_id = self._poll_sequence
        started = time.perf_counter()
        lock_started = started
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_et = now_utc.astimezone(NY)
        logger.info(
            "RSS poll starting poll=%d trigger=%s at_utc=%s at_et=%s",
            poll_id,
            trigger,
            now_utc.isoformat(),
            now_et.isoformat(),
        )

        async with self._update_lock:
            lock_acquired = time.perf_counter()
            logger.info(
                "RSS poll lock acquired poll=%d wait_ms=%.1f",
                poll_id,
                (lock_acquired - lock_started) * 1000,
            )

            before_feeds = await self._feed_diagnostics()
            self._log_feed_diagnostics(poll_id, "before", before_feeds)

            update_started = time.perf_counter()
            # Use the iterator so per-feed parse and HTTP failures are observable.
            update_results = await self._run_reader(
                lambda reader: list(reader.update_feeds_iter(scheduled=False)),
                default=None,
            )
            update_finished = time.perf_counter()
            logger.info(
                "RSS update finished poll=%d elapsed_ms=%.1f result_count=%s",
                poll_id,
                (update_finished - update_started) * 1000,
                "failed" if update_results is None else len(update_results),
            )
            if update_results is not None:
                self._log_update_results(poll_id, update_results)

            after_feeds = await self._feed_diagnostics()
            self._log_feed_diagnostics(poll_id, "after", after_feeds)

            async with asyncio.TaskGroup() as tg:
                posted = 0
                entries = await self._run_reader(
                    lambda reader: list(reader.get_entries(read=False)), default=[]
                )
                logger.info(
                    "RSS unread entries poll=%d count=%d",
                    poll_id,
                    len(entries),
                )
                for entry in entries:
                    logger.info(
                        "RSS unread entry poll=%d title=%r published=%s "
                        "first_seen=%s last_updated=%s",
                        poll_id,
                        _safe_log_text(entry.title),
                        _format_log_datetime(entry.published),
                        _format_log_datetime(_entry_first_seen(entry)),
                        _format_log_datetime(getattr(entry, "last_updated", None)),
                    )
                    if (
                        not entry.published or entry.published > START_OF_TIME
                    ) and posted <= 3:
                        tg.create_task(self.post_entry(entry, poll_id=poll_id))
                        posted += 1
                    else:
                        logger.info(
                            "RSS entry skipped poll=%d title=%r reason=%s",
                            poll_id,
                            _safe_log_text(entry.title),
                            "post-limit" if posted > 3 else "before-start-of-time",
                        )
                        tg.create_task(
                            self._run_reader_method("mark_entry_as_read", entry)
                        )

            logger.info(
                "RSS poll finished poll=%d posted=%d total_elapsed_ms=%.1f",
                poll_id,
                posted,
                (time.perf_counter() - started) * 1000,
            )

    async def _feed_diagnostics(self):
        def operation(reader):
            diagnostics = []
            for feed in reader.get_feeds():
                latest_entry = next(
                    reader.get_entries(feed=feed, limit=1),
                    None,
                )
                diagnostics.append(
                    {
                        "url": feed.url,
                        "title": feed.title,
                        "last_retrieved": feed.last_retrieved,
                        "last_updated": feed.last_updated,
                        "update_after": feed.update_after,
                        "last_exception": feed.last_exception,
                        "latest_entry_title": (
                            latest_entry.title if latest_entry is not None else None
                        ),
                        "latest_entry_published": (
                            latest_entry.published if latest_entry is not None else None
                        ),
                        "latest_entry_first_seen": (
                            _entry_first_seen(latest_entry)
                            if latest_entry is not None
                            else None
                        ),
                    }
                )
            return diagnostics

        return await self._run_reader(operation, default=[])

    def _feed_label(self, feed_url: str) -> str:
        return self._feed_labels.get(feed_url, "unknown-feed")

    def _log_feed_diagnostics(self, poll_id: int, phase: str, feeds):
        for feed in feeds:
            last_exception = feed["last_exception"]
            exception_text = "none"
            if last_exception is not None:
                exception_text = _safe_log_text(
                    f"{last_exception.type_name}: {last_exception.value_str}"
                )
            logger.info(
                "RSS feed state poll=%d phase=%s feed=%s title=%r "
                "last_retrieved=%s last_updated=%s update_after=%s "
                "last_exception=%s latest_entry=%r latest_published=%s "
                "latest_first_seen=%s",
                poll_id,
                phase,
                self._feed_label(feed["url"]),
                _safe_log_text(feed["title"]),
                _format_log_datetime(feed["last_retrieved"]),
                _format_log_datetime(feed["last_updated"]),
                _format_log_datetime(feed["update_after"]),
                exception_text,
                _safe_log_text(feed["latest_entry_title"]),
                _format_log_datetime(feed["latest_entry_published"]),
                _format_log_datetime(feed["latest_entry_first_seen"]),
            )

    def _log_update_results(self, poll_id: int, results):
        for result in results:
            feed_url, value = result
            feed_label = self._feed_label(feed_url)
            if isinstance(value, Exception):
                logger.error(
                    "RSS update result poll=%d feed=%s status=error "
                    "error_type=%s error=%s",
                    poll_id,
                    feed_label,
                    type(value).__name__,
                    _safe_log_text(value),
                )
            elif value is None:
                logger.info(
                    "RSS update result poll=%d feed=%s status=server-unchanged",
                    poll_id,
                    feed_label,
                )
            else:
                logger.info(
                    "RSS update result poll=%d feed=%s status=fetched new=%d "
                    "modified=%d unmodified=%d",
                    poll_id,
                    feed_label,
                    value.new,
                    value.modified,
                    value.unmodified,
                )

    async def _get_feed_metadata(self, feed_url: str) -> FeedMetadata:
        if cached := self._feed_cache.get(feed_url):
            logger.info(
                "RSS metadata cache hit feed=%s items=%d",
                self._feed_label(feed_url),
                len(cached.items),
            )
            return cached

        lock = self._feed_cache_locks.setdefault(feed_url, asyncio.Lock())
        async with lock:
            if cached := self._feed_cache.get(feed_url):
                logger.info(
                    "RSS metadata cache hit after lock feed=%s items=%d",
                    self._feed_label(feed_url),
                    len(cached.items),
                )
                return cached

            started = time.perf_counter()
            logger.info(
                "RSS metadata fetch starting feed=%s timeout_seconds=%d",
                self._feed_label(feed_url),
                FEED_FETCH_TIMEOUT,
            )
            try:
                metadata = await run_blocking(_fetch_feed_metadata, feed_url)
            except Exception as error:
                logger.warning(
                    "RSS metadata fetch failed feed=%s elapsed_ms=%.1f "
                    "error_type=%s error=%s",
                    self._feed_label(feed_url),
                    (time.perf_counter() - started) * 1000,
                    type(error).__name__,
                    _safe_log_text(error),
                )
                metadata = FeedMetadata()
            else:
                logger.info(
                    "RSS metadata fetch finished feed=%s elapsed_ms=%.1f items=%d",
                    self._feed_label(feed_url),
                    (time.perf_counter() - started) * 1000,
                    len(metadata.items),
                )

            self._feed_cache[feed_url] = metadata
            return metadata

    async def post_entry(self, entry, *, poll_id: int):
        started = time.perf_counter()
        stage = "resolve-channel"
        title = _safe_log_text(entry.title)
        logger.info("RSS post starting poll=%d title=%r", poll_id, title)
        try:
            channel = self.bot.get_channel(TARGET_CHANNEL) or (
                await self.bot.fetch_channel(TARGET_CHANNEL)
            )  # should only need to fetch at most once
            stage = "resolve-role"
            guild = channel.guild
            role = guild.get_role(TARGET_ROLE) or (await guild.fetch_role(TARGET_ROLE))

            stage = "build-card"
            stage_started = time.perf_counter()
            view = await self._build_episode_card(entry)
            logger.info(
                "RSS post card built poll=%d title=%r elapsed_ms=%.1f",
                poll_id,
                title,
                (time.perf_counter() - stage_started) * 1000,
            )

            stage = "send-card"
            stage_started = time.perf_counter()
            msg = await channel.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
            logger.info(
                "RSS post card sent poll=%d title=%r message_id=%d "
                "elapsed_ms=%.1f total_elapsed_ms=%.1f",
                poll_id,
                title,
                msg.id,
                (time.perf_counter() - stage_started) * 1000,
                (time.perf_counter() - started) * 1000,
            )

            stage = "send-mention"
            stage_started = time.perf_counter()
            mention = await channel.send(
                role.mention, allowed_mentions=discord.AllowedMentions.all()
            )
            logger.info(
                "RSS post mention sent poll=%d title=%r message_id=%d elapsed_ms=%.1f",
                poll_id,
                title,
                mention.id,
                (time.perf_counter() - stage_started) * 1000,
            )

            stage = "mark-read"
            stage_started = time.perf_counter()
            await self._run_reader_method("mark_entry_as_read", entry)
            self._last_episode_posted_at = datetime.datetime.now(datetime.timezone.utc)
            posted_date_et = self._last_episode_posted_at.astimezone(NY).date()
            self._episode_posts_by_date[posted_date_et] = (
                self._episode_posts_by_date.get(posted_date_et, 0) + 1
            )
            self._episode_posted_event.set()
            logger.info(
                "RSS post finished poll=%d title=%r mark_read_ms=%.1f "
                "total_elapsed_ms=%.1f posted_at=%s",
                poll_id,
                title,
                (time.perf_counter() - stage_started) * 1000,
                (time.perf_counter() - started) * 1000,
                self._last_episode_posted_at.isoformat(),
            )
        except Exception:
            logger.exception(
                "RSS post failed poll=%d title=%r stage=%s total_elapsed_ms=%.1f",
                poll_id,
                title,
                stage,
                (time.perf_counter() - started) * 1000,
            )
            raise

    async def _get_item_metadata(self, entry) -> tuple[FeedMetadata, FeedItemMetadata]:
        feed_metadata = await self._get_feed_metadata(entry.feed_url)
        item_metadata = next(
            (
                item
                for item in feed_metadata.items
                if _feed_item_matches_entry(item, entry)
            ),
            FeedItemMetadata("[unknown]"),
        )
        return feed_metadata, item_metadata

    async def _build_episode_card(self, entry) -> discord.ui.LayoutView:
        feed_metadata, item_metadata = await self._get_item_metadata(entry)
        view = discord.ui.LayoutView(timeout=None)

        feed_title = _escape_display_text(
            entry.feed_resolved_title or feed_metadata.title
        )
        title = _escape_display_text(_truncate_text(entry.title or "New episode", 300))
        summary = _escape_display_text(
            _build_summary(_preferred_summary_source(entry, item_metadata))
        )

        metadata_bits = []
        if item_metadata.episode_type and item_metadata.episode_type.title() != "Full":
            metadata_bits.append(item_metadata.episode_type.title())
        if duration := _format_duration(item_metadata.duration_seconds):
            metadata_bits.append(duration)
        if published := _format_timestamp(entry.published):
            metadata_bits.append(published)
        metadata_bits.append("Posted by: Andy")

        metadata_lines = []
        if metadata_bits:
            metadata_lines.append(f"-# {' • '.join(metadata_bits)}")
        metadata_line = "\n".join(metadata_lines) if metadata_lines else None

        card = discord.ui.Container(accent_color=discord.Color.blurple())

        if feed_title:
            card.add_item(discord.ui.TextDisplay(f"-# {feed_title}"))

        body_children = [f"## {title}"]
        if summary:
            body_children.append(summary)
        if metadata_line:
            body_children.append(metadata_line)

        primary_url = entry.link or item_metadata.link
        audio_url = next(
            (enclosure.href for enclosure in entry.enclosures if enclosure.href), None
        )
        image_url = item_metadata.image_url or feed_metadata.image_url

        if image_url:
            body_section = discord.ui.Section(
                *body_children,
                accessory=discord.ui.Thumbnail(
                    image_url,
                    description=entry.title or "Episode art",
                ),
            )
            card.add_item(body_section)
        else:
            for child in body_children:
                card.add_item(discord.ui.TextDisplay(child))

        view.add_item(card)
        return view


async def setup(bot):
    await bot.add_cog(EpPoster(bot))


################################################################################
# Formatting helpers

SUMMARY_LIMIT = 900
SUMMARY_PARAGRAPH_LIMIT = 2
SUMMARY_TRIM_MARKERS = (
    "Learn more about your ad choices.",
    "Apple Podcasts:",
    "Sign up for Check Book",
    "Join our Patreon",
    "Follow us @",
    "Buy some real nerdy merch",
    "Connect with other Blankies",
    "Subscribe to ",
    "Read ",
)


class _HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {"article", "blockquote", "div", "li", "p", "section", "tr"}
    SKIP_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if self._skip_depth or not data:
            return
        self.parts.append(data)

    def get_text(self) -> str:
        return "".join(self.parts)


def _normalize_whitespace(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"\s+", " ", unescape(text)).strip()
    return text or None


def _element_text(parent: ET.Element | None, path: str) -> str | None:
    if parent is None:
        return None
    element = parent.find(path)
    if element is None or element.text is None:
        return None
    text = element.text.strip()
    return text or None


def _element_markup(parent: ET.Element | None, path: str) -> str | None:
    if parent is None:
        return None

    element = parent.find(path)
    if element is None:
        return None

    parts = [element.text or ""]
    parts.extend(ET.tostring(child, encoding="unicode") for child in element)
    markup = "".join(parts).strip()
    return markup or None


def _first_nonempty(*values: str | None) -> str | None:
    for value in values:
        if value and (s := value.strip()):
            return s
    return None


def _extract_image_url(parent: ET.Element | None) -> str | None:
    if parent is None:
        return None

    itunes_image = parent.find(f"{{{ITUNES_NS}}}image")
    if itunes_image is not None:
        href = itunes_image.attrib.get("href", "").strip()
        if href:
            return href

    return _element_text(parent, "image/url")


def _parse_duration_seconds(value: str | None) -> int | None:
    if not value:
        return None

    value = value.strip()
    if not value:
        return None

    if value.isdigit():
        return int(value)

    try:
        parts = [int(part) for part in value.split(":")]
    except ValueError:
        return None

    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    return None


def _format_duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None

    hours, minsecs = divmod(seconds, 3600)
    minutes, seconds = divmod(minsecs, 60)

    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    elif not hours and seconds:
        parts.append(f"{seconds}s")

    return " ".join(parts) or "0s"


def _fully_unescape(text: str) -> str:
    while True:
        unescaped = unescape(text)
        if unescaped == text:
            return text
        text = unescaped


def _html_to_text(raw_text: str | None) -> str | None:
    if not raw_text:
        return None

    text = _fully_unescape(raw_text)
    if "<" in text and ">" in text:
        parser = _HTMLTextExtractor()
        parser.feed(text)
        parser.close()
        text = parser.get_text()

    text = (
        text.replace("\r", "\n")
        .replace("\xa0", " ")
        .replace("\u2060", "")
        .replace("\ufeff", "")
    )
    paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n+", text)
        if paragraph.strip()
    ]
    if not paragraphs:
        return None
    return "\n\n".join(paragraphs)


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text

    sentence_cut = max(
        text.rfind(". ", 0, limit),
        text.rfind("! ", 0, limit),
        text.rfind("? ", 0, limit),
    )
    if sentence_cut >= limit // 2:
        return text[: sentence_cut + 1].rstrip() + "..."

    word_cut = text.rfind(" ", 0, limit)
    if word_cut >= limit // 2:
        return text[:word_cut].rstrip() + "..."

    return text[: limit - 3].rstrip() + "..."


def _trim_summary_boilerplate(text: str) -> str:
    cut_points = [
        text.find(marker) for marker in SUMMARY_TRIM_MARKERS if text.find(marker) >= 120
    ]
    if cut_points:
        return text[: min(cut_points)].rstrip()
    return text


def _build_summary(raw_text: str | None) -> str | None:
    text = _html_to_text(raw_text)
    if not text:
        return None

    text = _trim_summary_boilerplate(text)
    paragraphs = [paragraph for paragraph in text.split("\n\n") if paragraph]
    if not paragraphs:
        return None

    selected = []
    for paragraph in paragraphs:
        if selected and len(paragraph) < 80 and len(paragraph.split()) < 12:
            continue
        candidate = "\n\n".join([*selected, paragraph])
        if selected and len(candidate) > SUMMARY_LIMIT:
            break
        selected.append(paragraph)
        if len(selected) >= SUMMARY_PARAGRAPH_LIMIT:
            break

    summary = "\n\n".join(selected) if selected else paragraphs[0]
    return _truncate_text(summary, SUMMARY_LIMIT)


def _escape_display_text(text: str | None) -> str | None:
    if not text:
        return None
    text = discord.utils.escape_mentions(text)
    text = discord.utils.escape_markdown(text, as_needed=True)
    return text


def _format_timestamp(timestamp: datetime.datetime | None) -> str | None:
    if timestamp is None:
        return None
    return f"<t:{int(timestamp.timestamp())}:f>"


def _feed_item_matches_entry(item: FeedItemMetadata, entry) -> bool:
    def values(x):
        return {
            _normalize_whitespace(getattr(x, n)) or None
            for n in ["id", "link", "title"]
        } - {None}

    return bool(values(item) & values(entry))


def _preferred_summary_source(
    entry, item_metadata: FeedItemMetadata | None
) -> str | None:
    if item_metadata and item_metadata.content_html:
        return item_metadata.content_html

    entry_html = next(
        (content.value for content in entry.content if content.is_html), None
    )
    if entry_html:
        return entry_html

    if item_metadata and item_metadata.summary:
        return item_metadata.summary

    if entry.summary:
        return entry.summary

    entry_text = next(
        (content.value for content in entry.content if content.value), None
    )
    return entry_text


def _fetch_feed_metadata(feed_url: str) -> FeedMetadata:
    request = Request(feed_url, headers={"User-Agent": "comedy-points-bot/1.0"})
    with urlopen(request, timeout=FEED_FETCH_TIMEOUT) as response:
        root = ET.fromstring(response.read())

    channel = root.find("channel")
    if channel is None:
        raise ValueError(f"RSS feed at {feed_url} did not contain a <channel>")

    feed_image = _extract_image_url(channel)
    items = []
    for item in channel.findall("item"):
        title = _element_text(item, "title")
        link = _element_text(item, "link")
        guid = _element_text(item, "guid")
        item_id = _first_nonempty(guid, link, title)
        if not item_id:
            continue

        content_html = _element_markup(item, f"{{{CONTENT_NS}}}encoded")
        items.append(
            FeedItemMetadata(
                id=item_id,
                title=title,
                link=link,
                author=_first_nonempty(
                    _element_text(item, f"{{{ITUNES_NS}}}author"),
                    _element_text(item, "author"),
                ),
                summary=_first_nonempty(
                    _element_markup(item, f"{{{ITUNES_NS}}}summary"),
                    content_html,
                    _element_markup(item, "description"),
                ),
                content_html=content_html,
                image_url=_extract_image_url(item) or feed_image,
                duration_seconds=_parse_duration_seconds(
                    _element_text(item, f"{{{ITUNES_NS}}}duration")
                ),
                episode_type=_element_text(item, f"{{{ITUNES_NS}}}episodeType"),
            )
        )

    return FeedMetadata(
        title=_element_text(channel, "title"),
        link=_element_text(channel, "link"),
        image_url=feed_image,
        items=tuple(items),
    )
