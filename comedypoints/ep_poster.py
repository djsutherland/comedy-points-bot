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
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from logging import getLogger
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

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
    TARGET_ROLE = 1484422590885007430
else:
    TARGET_CHANNEL = 755516308355022970  # blankies #bot-testing-ground
    TARGET_ROLE = 795408027883929601

START_OF_TIME = datetime.datetime(2026, 3, 16, tzinfo=datetime.timezone.utc)
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
SUMMARY_LIMIT = 900
SUMMARY_PARAGRAPH_LIMIT = 2
FEED_FETCH_TIMEOUT = 15
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


async def run_in_thread(func, *args, default=None, **kwargs):
    """Runs a blocking reader task in a separate thread."""
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except ReaderError as error:
        logger.error("Error executing task: %s", error)
        return default


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
        if value and value.strip():
            return value.strip()
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

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

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

    sentence_cut = max(text.rfind(". ", 0, limit), text.rfind("! ", 0, limit), text.rfind("? ", 0, limit))
    if sentence_cut >= limit // 2:
        return text[: sentence_cut + 1].rstrip() + "..."

    word_cut = text.rfind(" ", 0, limit)
    if word_cut >= limit // 2:
        return text[:word_cut].rstrip() + "..."

    return text[: limit - 3].rstrip() + "..."


def _trim_summary_boilerplate(text: str) -> str:
    cut_points = [
        text.find(marker)
        for marker in SUMMARY_TRIM_MARKERS
        if text.find(marker) >= 120
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
    entry_values = {
        value
        for value in (
            _normalize_whitespace(entry.id),
            _normalize_whitespace(entry.link),
            _normalize_whitespace(entry.title),
        )
        if value
    }
    item_values = {
        value
        for value in (
            _normalize_whitespace(item.id),
            _normalize_whitespace(item.link),
            _normalize_whitespace(item.title),
        )
        if value
    }
    return bool(entry_values & item_values)


def _preferred_summary_source(entry, item_metadata: FeedItemMetadata | None) -> str | None:
    if item_metadata and item_metadata.content_html:
        return item_metadata.content_html

    entry_html = next((content.value for content in entry.content if content.is_html), None)
    if entry_html:
        return entry_html

    if item_metadata and item_metadata.summary:
        return item_metadata.summary

    if entry.summary:
        return entry.summary

    entry_text = next((content.value for content in entry.content if content.value), None)
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


class EpPoster(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._feed_cache = {}
        self._feed_cache_locks = {}

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
        self._feed_cache.clear()

        async with asyncio.TaskGroup() as tg:
            posted = 0
            for entry in self.reader.get_entries(read=False):
                if (not entry.published or entry.published > START_OF_TIME) and posted <= 3:
                    tg.create_task(self.post_entry(entry))
                    posted += 1
                else:
                    tg.create_task(run_in_thread(self.reader.mark_entry_as_read, entry))

    async def _get_feed_metadata(self, feed_url: str) -> FeedMetadata:
        if cached := self._feed_cache.get(feed_url):
            return cached

        lock = self._feed_cache_locks.setdefault(feed_url, asyncio.Lock())
        async with lock:
            if cached := self._feed_cache.get(feed_url):
                return cached

            try:
                metadata = await asyncio.to_thread(_fetch_feed_metadata, feed_url)
            except Exception:
                logger.warning("Failed to fetch feed metadata for %s", feed_url, exc_info=True)
                metadata = FeedMetadata()

            self._feed_cache[feed_url] = metadata
            return metadata

    async def _get_item_metadata(self, entry) -> tuple[FeedMetadata, FeedItemMetadata | None]:
        feed_metadata = await self._get_feed_metadata(entry.feed_url)
        item_metadata = next(
            (item for item in feed_metadata.items if _feed_item_matches_entry(item, entry)),
            None,
        )
        return feed_metadata, item_metadata

    async def _build_episode_card(self, entry) -> discord.ui.LayoutView:
        feed_metadata, item_metadata = await self._get_item_metadata(entry)
        view = discord.ui.LayoutView(timeout=None)

        feed_title = _escape_display_text(entry.feed_resolved_title or feed_metadata.title)
        title = _escape_display_text(_truncate_text(entry.title or "New episode", 300))
        summary = _escape_display_text(_build_summary(_preferred_summary_source(entry, item_metadata)))

        metadata_bits = []
        if item_metadata and item_metadata.episode_type and item_metadata.episode_type.title() != "Full":
            metadata_bits.append(item_metadata.episode_type.title())
        if duration := _format_duration(item_metadata.duration_seconds if item_metadata else None):
            metadata_bits.append(duration)
        if published := _format_timestamp(entry.published):
            metadata_bits.append(published)

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

        primary_url = entry.link or (item_metadata.link if item_metadata else None)
        audio_url = next((enclosure.href for enclosure in entry.enclosures if enclosure.href), None)
        image_url = (
            item_metadata.image_url if item_metadata else None
        ) or feed_metadata.image_url

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

    async def post_entry(self, entry):
        channel = self.bot.get_channel(TARGET_CHANNEL) or (
            await self.bot.fetch_channel(TARGET_CHANNEL)
        )  # should only need to fetch at most once
        guild = channel.guild
        role = guild.get_role(TARGET_ROLE) or (await guild.fetch_role(TARGET_ROLE))

        view = await self._build_episode_card(entry)
        msg = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        await msg.reply(role.mention)
        await run_in_thread(self.reader.mark_entry_as_read, entry)


async def setup(bot):
    await bot.add_cog(EpPoster(bot))
