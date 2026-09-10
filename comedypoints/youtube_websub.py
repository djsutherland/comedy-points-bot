import asyncio
from dataclasses import dataclass, replace
import datetime
import email.utils
import hashlib
import hmac
from logging import getLogger
import math
import os
import re
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from aiohttp import ClientSession, ClientTimeout, web
from discord.ext import commands

from .episode_dedupe import WebSubInbox
from .ep_poster import (
    EPISODE_CLAIMS_DB_PATH,
    EXPECTED_EPISODE_START_TIME,
    EXPECTED_EPISODE_WINDOW,
    NY,
    PUBLIC_EPISODE_WEEKDAY,
)


logger = getLogger(__name__)

UTC = datetime.timezone.utc
YOUTUBE_CHANNEL_ID = os.environ.get(
    "YOUTUBE_CHANNEL_ID", "UCI8t9VKTB6uD91NvlC15oJA"
)
YOUTUBE_TOPIC_URL = (
    "https://www.youtube.com/feeds/videos.xml?channel_id=" + YOUTUBE_CHANNEL_ID
)
YOUTUBE_HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"
YOUTUBE_API_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_CALLBACK_URL = os.environ.get("YOUTUBE_WEBSUB_CALLBACK_URL")
YOUTUBE_SECRET = os.environ.get("YOUTUBE_WEBSUB_SECRET")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
YOUTUBE_BIND_HOST = os.environ.get("YOUTUBE_WEBSUB_BIND_HOST", "127.0.0.1")
YOUTUBE_BIND_PORT = int(os.environ.get("YOUTUBE_WEBSUB_BIND_PORT", "8080"))
YOUTUBE_NOTIFICATION_MAX_AGE = datetime.timedelta(hours=6)
YOUTUBE_NOTIFICATION_FUTURE_TOLERANCE = datetime.timedelta(minutes=5)
YOUTUBE_METADATA_ATTEMPTS = 3
YOUTUBE_HTTP_TIMEOUT = ClientTimeout(total=15)
YOUTUBE_SUBSCRIPTION_TIMEOUT = ClientTimeout(total=60)
YOUTUBE_VERIFICATION_WAIT_SECONDS = 60
YOUTUBE_RETRY_AFTER_MAX_SECONDS = 15 * 60

# Data API polling of the channel's uploads playlist. The uploads playlist ID
# is the channel ID with its "UC" prefix replaced by "UU".
YOUTUBE_PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
YOUTUBE_UPLOADS_PLAYLIST_ID = os.environ.get("YOUTUBE_UPLOADS_PLAYLIST_ID") or (
    "UU" + YOUTUBE_CHANNEL_ID[2:] if YOUTUBE_CHANNEL_ID.startswith("UC") else None
)
YOUTUBE_API_POLL_SECONDS = int(os.environ.get("YOUTUBE_API_POLL_SECONDS", "60"))
YOUTUBE_API_WINDOW_POLL_SECONDS = 5
YOUTUBE_API_WINDOW_LEAD = datetime.timedelta(seconds=30)
YOUTUBE_API_PAGE_SIZE = 10
YOUTUBE_API_HEARTBEAT_SECONDS = 60 * 60

# WARNING-level log records reach the Discord DM handler. During a sustained
# outage, alert once the failure looks real, then at most once per day.
YOUTUBE_WEBSUB_ALERT_AFTER_FAILURES = 3
YOUTUBE_API_ALERT_AFTER_FAILURES = 5
YOUTUBE_ALERT_INTERVAL_SECONDS = 24 * 60 * 60

ATOM_NS = "http://www.w3.org/2005/Atom"
MEDIA_NS = "http://search.yahoo.com/mrss/"
YT_NS = "http://www.youtube.com/xml/schemas/2015"


@dataclass(frozen=True)
class YouTubeVideo:
    video_id: str
    channel_id: str
    title: str
    link: str
    author: str | None
    description: str | None
    thumbnail_url: str | None
    duration_seconds: int | None
    published: datetime.datetime
    updated: datetime.datetime | None
    privacy_status: str | None = None


def parse_youtube_notification(payload: bytes) -> tuple[YouTubeVideo, ...]:
    root = ET.fromstring(payload)
    videos = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        video_id = _element_text(entry, f"{{{YT_NS}}}videoId")
        channel_id = _element_text(entry, f"{{{YT_NS}}}channelId")
        title = _element_text(entry, f"{{{ATOM_NS}}}title")
        published = _parse_datetime(
            _element_text(entry, f"{{{ATOM_NS}}}published")
        )
        if not video_id or not channel_id or not title or published is None:
            raise ValueError("YouTube notification entry is missing required fields")

        link_element = next(
            (
                element
                for element in entry.findall(f"{{{ATOM_NS}}}link")
                if element.attrib.get("rel") == "alternate"
            ),
            None,
        )
        link = (
            link_element.attrib.get("href") if link_element is not None else None
        ) or f"https://www.youtube.com/watch?v={video_id}"
        media_group = entry.find(f"{{{MEDIA_NS}}}group")
        thumbnail = (
            media_group.find(f"{{{MEDIA_NS}}}thumbnail")
            if media_group is not None
            else None
        )
        videos.append(
            YouTubeVideo(
                video_id=video_id,
                channel_id=channel_id,
                title=title,
                link=link,
                author=_element_text(
                    entry, f"{{{ATOM_NS}}}author/{{{ATOM_NS}}}name"
                ),
                description=_element_text(
                    media_group, f"{{{MEDIA_NS}}}description"
                ),
                thumbnail_url=(
                    thumbnail.attrib.get("url") if thumbnail is not None else None
                ),
                duration_seconds=None,
                published=published,
                updated=_parse_datetime(
                    _element_text(entry, f"{{{ATOM_NS}}}updated")
                ),
            )
        )
    return tuple(videos)


def verify_websub_signature(payload: bytes, header: str | None, secret: str) -> bool:
    if not header or "=" not in header:
        return False
    algorithm, supplied_digest = header.split("=", 1)
    algorithm = algorithm.lower()
    if algorithm not in {"sha1", "sha256", "sha384", "sha512"}:
        return False
    expected_digest = hmac.new(
        secret.encode(), payload, getattr(hashlib, algorithm)
    ).hexdigest()
    return supplied_digest.isascii() and hmac.compare_digest(
        expected_digest, supplied_digest.lower()
    )


def _renewal_delay(lease_seconds):
    if lease_seconds <= 0:
        raise ValueError("WebSub lease must be positive")
    return lease_seconds * 0.8


def _subscription_retry_delay(failures, hub_retry_after=None):
    backoff = min(300, 15 * 2 ** min(max(failures - 1, 0), 5))
    if hub_retry_after is None:
        return backoff
    return max(backoff, min(hub_retry_after, YOUTUBE_RETRY_AFTER_MAX_SECONDS))


def _parse_retry_after(value):
    """Parse an HTTP Retry-After header into whole seconds, or None if unusable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        retry_at = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0, math.ceil((retry_at - datetime.datetime.now(UTC)).total_seconds()))


class OutageAlerter:
    """Decide when a repeating failure deserves a WARNING (a Discord DM).

    Callers log every failure at INFO and ask ``record_failure`` whether to
    also emit a WARNING: yes after ``threshold`` consecutive failures, then at
    most once per ``interval_seconds``. ``record_success`` resets the state and
    reports whether a recovery notice is due (only if an alert was sent).
    """

    def __init__(self, *, threshold, interval_seconds, clock=time.monotonic):
        self._threshold = threshold
        self._interval = interval_seconds
        self._clock = clock
        self.failures = 0
        self.started_at = None
        self._started_monotonic = None
        self._last_alert_monotonic = None

    @property
    def outage_seconds(self):
        if self._started_monotonic is None:
            return 0.0
        return self._clock() - self._started_monotonic

    def record_failure(self) -> bool:
        now = self._clock()
        self.failures += 1
        if self._started_monotonic is None:
            self._started_monotonic = now
            self.started_at = datetime.datetime.now(UTC)
        if self.failures < self._threshold:
            return False
        if (
            self._last_alert_monotonic is not None
            and now - self._last_alert_monotonic < self._interval
        ):
            return False
        self._last_alert_monotonic = now
        return True

    def record_success(self) -> tuple[bool, int, float]:
        """Reset and return (alert_was_sent, failures, outage_seconds)."""
        result = (
            self._last_alert_monotonic is not None,
            self.failures,
            self.outage_seconds,
        )
        self.failures = 0
        self.started_at = None
        self._started_monotonic = None
        self._last_alert_monotonic = None
        return result


def _api_poll_delay(now_et):
    """Return (seconds until the next uploads poll, current mode).

    On public-episode days (Sundays, Eastern), rapid polling covers the
    expected-episode window, starting a little before
    EXPECTED_EPISODE_START_TIME; otherwise the baseline cadence applies,
    shortened so the next window is entered on time.
    """
    for day_offset in range(8):
        date = now_et.date() + datetime.timedelta(days=day_offset)
        if date.weekday() != PUBLIC_EPISODE_WEEKDAY:
            continue
        window_start = datetime.datetime.combine(date, EXPECTED_EPISODE_START_TIME)
        window_open = window_start - YOUTUBE_API_WINDOW_LEAD
        if window_open <= now_et < window_start + EXPECTED_EPISODE_WINDOW:
            return YOUTUBE_API_WINDOW_POLL_SECONDS, "window"
        if now_et < window_open:
            until_open = (
                window_open.astimezone(UTC) - now_et.astimezone(UTC)
            ).total_seconds()
            return max(0.0, min(YOUTUBE_API_POLL_SECONDS, until_open)), "baseline"
    return YOUTUBE_API_POLL_SECONDS, "baseline"


@web.middleware
async def _log_callback_requests(request, handler):
    # Log every request reaching the listener, including paths the router
    # rejects, so "did the hub ever call us?" is answerable from the journal.
    started = time.perf_counter()
    status = "unhandled-error"
    try:
        response = await handler(request)
        status = response.status
        return response
    except web.HTTPException as error:
        status = error.status
        raise
    finally:
        logger.info(
            "YouTube WebSub callback request method=%s path=%s query=%r "
            "remote=%s forwarded_for=%s user_agent=%r status=%s elapsed_ms=%.1f",
            request.method,
            _safe_text(request.path),
            _safe_text(request.query_string, 600),
            request.remote,
            _safe_text(request.headers.get("X-Forwarded-For", "none")),
            _safe_text(request.headers.get("User-Agent", "none")),
            status,
            (time.perf_counter() - started) * 1000,
        )


class YouTubeWebSub(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._runner = None
        self._session = None
        self._subscription_task = None
        self._worker_task = None
        self._inbox_event = asyncio.Event()
        self._inbox = WebSubInbox(EPISODE_CLAIMS_DB_PATH)
        self._activated_at = None
        self._pending_subscription = False
        self._verified_event = asyncio.Event()
        self._verified_at_monotonic = None
        self._lease_seconds = None
        self._hub_retry_after = None
        self._last_subscription_outcome = "none"
        self._websub_alerter = OutageAlerter(
            threshold=YOUTUBE_WEBSUB_ALERT_AFTER_FAILURES,
            interval_seconds=YOUTUBE_ALERT_INTERVAL_SECONDS,
        )
        self._api_poll_task = None
        self._api_poll_sequence = 0
        self._api_seen_video_ids = set()
        self._api_alerter = OutageAlerter(
            threshold=YOUTUBE_API_ALERT_AFTER_FAILURES,
            interval_seconds=YOUTUBE_ALERT_INTERVAL_SECONDS,
        )

    async def cog_load(self):
        if not YOUTUBE_API_KEY:
            logger.warning(
                "YouTube integration disabled missing_config=YOUTUBE_API_KEY "
                "channel_id=%s",
                YOUTUBE_CHANNEL_ID,
            )
            return
        websub_missing = [
            name
            for name, value in (
                ("YOUTUBE_WEBSUB_CALLBACK_URL", YOUTUBE_CALLBACK_URL),
                ("YOUTUBE_WEBSUB_SECRET", YOUTUBE_SECRET),
            )
            if not value
        ]
        callback_path = None
        if websub_missing:
            logger.warning(
                "YouTube WebSub disabled missing_config=%s channel_id=%s; "
                "Data API polling stays active",
                ",".join(websub_missing),
                YOUTUBE_CHANNEL_ID,
            )
        else:
            if len(YOUTUBE_SECRET.encode()) >= 200:
                raise ValueError("YOUTUBE_WEBSUB_SECRET must be under 200 bytes")
            callback = urlsplit(YOUTUBE_CALLBACK_URL)
            if callback.scheme != "https":
                logger.warning(
                    "YouTube WebSub callback is not HTTPS scheme=%s", callback.scheme
                )
            if callback.query or callback.fragment or not callback.netloc:
                raise ValueError(
                    "YOUTUBE_WEBSUB_CALLBACK_URL must have a host and no query/fragment"
                )
            callback_path = callback.path or "/"

        await asyncio.to_thread(self._inbox.initialize)
        self._activated_at = await asyncio.to_thread(self._inbox.activated_at)
        logger.info("YouTube activation cutoff=%s", self._activated_at.isoformat())

        self._session = ClientSession(timeout=YOUTUBE_HTTP_TIMEOUT)
        if callback_path is not None:
            app = web.Application(
                client_max_size=1024 * 1024, middlewares=[_log_callback_requests]
            )
            app.router.add_get(callback_path, self._handle_verification)
            app.router.add_post(callback_path, self._handle_notification)
            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            site = web.TCPSite(self._runner, YOUTUBE_BIND_HOST, YOUTUBE_BIND_PORT)
            await site.start()
            logger.info(
                "YouTube WebSub callback listening bind_host=%s bind_port=%d "
                "channel_id=%s",
                YOUTUBE_BIND_HOST,
                YOUTUBE_BIND_PORT,
                YOUTUBE_CHANNEL_ID,
            )
            self._worker_task = asyncio.create_task(
                self._inbox_loop(), name="youtube-websub-inbox"
            )
            self._subscription_task = asyncio.create_task(
                self._subscription_loop(), name="youtube-websub-subscription"
            )

        if YOUTUBE_UPLOADS_PLAYLIST_ID:
            logger.info(
                "YouTube Data API polling enabled playlist_id=%s "
                "baseline_seconds=%d window_seconds=%d window_start_et=%s "
                "window_minutes=%.0f lead_seconds=%.0f",
                YOUTUBE_UPLOADS_PLAYLIST_ID,
                YOUTUBE_API_POLL_SECONDS,
                YOUTUBE_API_WINDOW_POLL_SECONDS,
                EXPECTED_EPISODE_START_TIME.isoformat(),
                EXPECTED_EPISODE_WINDOW.total_seconds() / 60,
                YOUTUBE_API_WINDOW_LEAD.total_seconds(),
            )
            self._api_poll_task = asyncio.create_task(
                self._api_poll_loop(), name="youtube-api-poll"
            )
        else:
            logger.warning(
                "YouTube Data API polling disabled reason=no-uploads-playlist "
                "channel_id=%s",
                YOUTUBE_CHANNEL_ID,
            )

    async def cog_unload(self):
        # Stop accepting callbacks before draining/cancelling workers. Already
        # acknowledged work stays in SQLite and resumes on the next startup.
        if self._runner is not None:
            await self._runner.cleanup()
        tasks_to_stop = [
            task
            for task in (self._worker_task, self._subscription_task, self._api_poll_task)
            if task is not None
        ]
        for task in tasks_to_stop:
            task.cancel()
        if tasks_to_stop:
            await asyncio.gather(*tasks_to_stop, return_exceptions=True)
        if self._session is not None:
            await self._session.close()

    async def _handle_verification(self, request):
        mode = request.query.get("hub.mode")
        topic = request.query.get("hub.topic")
        challenge = request.query.get("hub.challenge")
        lease_text = request.query.get("hub.lease_seconds")
        if mode == "denied":
            logger.error(
                "YouTube WebSub subscription denied topic_matches=%s reason=%r",
                topic == YOUTUBE_TOPIC_URL,
                _safe_text(request.query.get("hub.reason")),
            )
            return web.Response(status=204)

        accepted = (
            mode == "subscribe"
            and topic == YOUTUBE_TOPIC_URL
            and challenge is not None
            and self._pending_subscription
        )
        logger.info(
            "YouTube WebSub verification mode=%s topic_matches=%s topic=%r "
            "challenge_present=%s pending=%s accepted=%s lease_seconds=%s",
            _safe_text(mode),
            topic == YOUTUBE_TOPIC_URL,
            _safe_text(topic),
            challenge is not None,
            self._pending_subscription,
            accepted,
            _safe_text(lease_text) if lease_text else "none",
        )
        if not accepted:
            return web.Response(status=404)

        try:
            self._lease_seconds = int(lease_text) if lease_text else None
            if self._lease_seconds is not None:
                _renewal_delay(self._lease_seconds)
        except ValueError:
            logger.warning(
                "YouTube WebSub verification has invalid lease_seconds=%r",
                _safe_text(lease_text),
            )
            return web.Response(status=404)
        self._pending_subscription = False
        self._verified_at_monotonic = time.monotonic()
        self._verified_event.set()
        return web.Response(
            body=challenge.encode(),
            content_type="application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    async def _handle_notification(self, request):
        received_at = datetime.datetime.now(UTC)
        started = time.perf_counter()
        payload = await request.read()
        payload_hash = hashlib.sha256(payload).hexdigest()
        signature_header = request.headers.get("X-Hub-Signature")
        signature_valid = verify_websub_signature(
            payload, signature_header, YOUTUBE_SECRET
        )
        logger.info(
            "YouTube WebSub delivery received at_utc=%s bytes=%d "
            "payload_sha256=%s signature_present=%s signature_valid=%s",
            received_at.isoformat(),
            len(payload),
            payload_hash,
            bool(signature_header),
            signature_valid,
        )
        if not signature_valid:
            logger.warning(
                "YouTube WebSub delivery ignored reason=invalid-signature "
                "payload_sha256=%s",
                payload_hash,
            )
            return web.Response(status=204)

        try:
            videos = parse_youtube_notification(payload)
        except (ET.ParseError, ValueError) as error:
            logger.warning(
                "YouTube WebSub delivery parse failed payload_sha256=%s "
                "error_type=%s error=%s",
                payload_hash,
                type(error).__name__,
                _safe_text(error),
            )
            return web.Response(status=400)

        try:
            await asyncio.to_thread(
                self._inbox.enqueue, payload_hash, payload, received_at
            )
        except Exception:
            logger.exception("YouTube WebSub inbox write failed; delivery not acknowledged")
            return web.Response(status=503)
        self._inbox_event.set()
        logger.info(
            "YouTube WebSub delivery accepted entries=%d payload_sha256=%s "
            "ack_elapsed_ms=%.1f",
            len(videos),
            payload_hash,
            (time.perf_counter() - started) * 1000,
        )
        return web.Response(status=204)

    async def _inbox_loop(self):
        await self.bot.wait_until_ready()
        while True:
            self._inbox_event.clear()
            try:
                worked = await self._process_next_delivery()
            except Exception:
                logger.exception("YouTube WebSub inbox worker failed; retrying")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self._inbox_event.wait(), timeout=5)
                except TimeoutError:
                    pass

    async def _process_next_delivery(self):
        delivery = await asyncio.to_thread(self._inbox.next_delivery)
        if delivery is None:
            return False
        digest, payload, received_text, attempts = delivery
        received_at = datetime.datetime.fromisoformat(received_text)
        try:
            for video in parse_youtube_notification(payload):
                await self._process_video(video, received_at=received_at)
            await asyncio.to_thread(self._inbox.finish_delivery, digest)
        except asyncio.CancelledError:
            raise
        except Exception:
            delay = await asyncio.to_thread(self._inbox.retry_delivery, digest, attempts)
            logger.exception(
                "YouTube WebSub delivery retry digest=%s attempt=%d delay_seconds=%d",
                digest, attempts + 1, delay,
            )
        return True

    async def _process_video(
        self,
        video: YouTubeVideo,
        *,
        received_at: datetime.datetime,
        trigger: str = "youtube:websub",
    ):
        started = time.perf_counter()
        published_lag = (received_at - video.published).total_seconds()
        updated_lag = (
            (received_at - video.updated).total_seconds()
            if video.updated is not None
            else None
        )
        logger.info(
            "YouTube entry processing trigger=%s video_id=%s channel_matches=%s "
            "title=%r published=%s updated=%s received_at=%s "
            "published_lag_seconds=%.3f updated_lag_seconds=%s",
            trigger,
            video.video_id,
            video.channel_id == YOUTUBE_CHANNEL_ID,
            _safe_text(video.title),
            video.published.isoformat(),
            video.updated.isoformat() if video.updated else "none",
            received_at.isoformat(),
            published_lag,
            f"{updated_lag:.3f}" if updated_lag is not None else "none",
        )
        if video.channel_id != YOUTUBE_CHANNEL_ID:
            logger.warning(
                "YouTube entry ignored video_id=%s reason=wrong-channel",
                video.video_id,
            )
            return
        # Persisted once, not reset on restart. Old title/description edits must
        # not repost episodes from before this deployment had a claim ledger.
        if video.published < self._activated_at:
            logger.info("YouTube entry ignored video_id=%s reason=before-activation",
                        video.video_id)
            return
        if published_lag > YOUTUBE_NOTIFICATION_MAX_AGE.total_seconds():
            logger.info(
                "YouTube entry ignored video_id=%s reason=old-update "
                "published_lag_seconds=%.3f",
                video.video_id,
                published_lag,
            )
            return
        if published_lag < -YOUTUBE_NOTIFICATION_FUTURE_TOLERANCE.total_seconds():
            logger.warning(
                "YouTube entry ignored video_id=%s reason=future-publication "
                "published_lag_seconds=%.3f",
                video.video_id,
                published_lag,
            )
            return

        if not YOUTUBE_API_KEY:
            raise RuntimeError("YouTube API key required for complete episode metadata")
        api_video = await self._fetch_video_metadata(video.video_id)
        if api_video is None:
            raise RuntimeError("YouTube API metadata unavailable; retry required")
        video = replace(api_video, updated=video.updated)
        if video.channel_id != YOUTUBE_CHANNEL_ID:
            logger.warning("YouTube entry ignored video_id=%s reason=api-channel-mismatch",
                           video.video_id)
            return
        if video.published < self._activated_at:
            logger.info("YouTube entry ignored video_id=%s reason=api-before-activation",
                        video.video_id)
            return
        if video.privacy_status != "public":
            raise RuntimeError("YouTube video not public yet; retry required")
        if not video.title or not video.description or not video.duration_seconds:
            raise RuntimeError("YouTube metadata incomplete; retry required")

        poster = self.bot.get_cog("EpPoster")
        if poster is None:
            logger.error(
                "YouTube entry ignored video_id=%s reason=poster-unavailable",
                video.video_id,
            )
            raise RuntimeError("Episode poster unavailable; retry required")
        try:
            posted = await poster.post_youtube_video(
                video, webhook_received_at=received_at, trigger=trigger
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "YouTube entry processing failed video_id=%s "
                "total_elapsed_ms=%.1f",
                video.video_id,
                (time.perf_counter() - started) * 1000,
            )
            raise
        logger.info(
            "YouTube entry processing finished video_id=%s posted=%s "
            "total_elapsed_ms=%.1f",
            video.video_id,
            posted,
            (time.perf_counter() - started) * 1000,
        )

    async def _fetch_video_metadata(self, video_id: str) -> YouTubeVideo | None:
        for attempt in range(1, YOUTUBE_METADATA_ATTEMPTS + 1):
            started = time.perf_counter()
            try:
                async with self._session.get(
                    YOUTUBE_API_URL,
                    params={
                        "part": "snippet,contentDetails,status",
                        "id": video_id,
                        "key": YOUTUBE_API_KEY,
                        "fields": (
                            "items(id,snippet(channelId,channelTitle,description,"
                            "publishedAt,thumbnails,title),contentDetails(duration),"
                            "status(privacyStatus))"
                        ),
                    },
                ) as response:
                    status = response.status
                    data = (
                        await response.json(content_type=None)
                        if status == 200
                        else None
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "YouTube API metadata failed video_id=%s attempt=%d "
                    "error_type=%s elapsed_ms=%.1f",
                    video_id,
                    attempt,
                    type(error).__name__,
                    (time.perf_counter() - started) * 1000,
                )
            else:
                items = data.get("items", []) if data else []
                logger.info(
                    "YouTube API metadata response video_id=%s attempt=%d "
                    "status=%d items=%d elapsed_ms=%.1f",
                    video_id,
                    attempt,
                    status,
                    len(items),
                    (time.perf_counter() - started) * 1000,
                )
                if items:
                    return _video_from_api(items[0])
            if attempt < YOUTUBE_METADATA_ATTEMPTS:
                await asyncio.sleep(2 ** (attempt - 1))
        return None

    async def _api_poll_loop(self):
        await self.bot.wait_until_ready()
        mode = None
        last_heartbeat = time.monotonic()
        while True:
            _, next_mode = _api_poll_delay(datetime.datetime.now(NY))
            if next_mode != mode:
                mode = next_mode
                logger.info(
                    "YouTube API poll mode=%s poll_seconds=%s playlist_id=%s",
                    mode,
                    YOUTUBE_API_WINDOW_POLL_SECONDS
                    if mode == "window"
                    else YOUTUBE_API_POLL_SECONDS,
                    YOUTUBE_UPLOADS_PLAYLIST_ID,
                )
            await self._api_poll_once(mode=mode)
            if time.monotonic() - last_heartbeat >= YOUTUBE_API_HEARTBEAT_SECONDS:
                # Uneventful baseline polls log at DEBUG; prove liveness hourly.
                last_heartbeat = time.monotonic()
                logger.info(
                    "YouTube API poll heartbeat polls=%d seen=%d "
                    "consecutive_failures=%d mode=%s",
                    self._api_poll_sequence,
                    len(self._api_seen_video_ids),
                    self._api_alerter.failures,
                    mode,
                )
            delay, _ = _api_poll_delay(datetime.datetime.now(NY))
            await asyncio.sleep(delay)

    async def _api_poll_once(self, *, mode):
        """Poll the uploads playlist once; never raises except on cancellation."""
        self._api_poll_sequence += 1
        poll_id = self._api_poll_sequence
        started = time.perf_counter()
        observed_at = datetime.datetime.now(UTC)
        status = None
        try:
            async with self._session.get(
                YOUTUBE_PLAYLIST_ITEMS_URL,
                params={
                    "part": "snippet,contentDetails,status",
                    "playlistId": YOUTUBE_UPLOADS_PLAYLIST_ID,
                    "maxResults": str(YOUTUBE_API_PAGE_SIZE),
                    "key": YOUTUBE_API_KEY,
                    "fields": (
                        "items(snippet(publishedAt,title,description,thumbnails,"
                        "resourceId/videoId,videoOwnerChannelId,channelId,"
                        "channelTitle),contentDetails(videoId,videoPublishedAt),"
                        "status/privacyStatus)"
                    ),
                },
            ) as response:
                status = response.status
                if status == 200:
                    data = await response.json(content_type=None)
                    detail = None
                else:
                    data = None
                    detail = _safe_text(await response.text(), 500)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._record_api_failure(
                poll_id,
                f"error_type={type(error).__name__} error={_safe_text(error)}",
                started,
            )
            return
        if data is None:
            self._record_api_failure(
                poll_id, f"status={status} body={detail!r}", started
            )
            return

        alerted, failures, outage_seconds = self._api_alerter.record_success()
        if alerted:
            logger.warning(
                "YouTube API polling recovered failures=%d outage_hours=%.1f",
                failures,
                outage_seconds / 3600,
            )
        elif failures:
            logger.info("YouTube API polling recovered failures=%d", failures)

        items = data.get("items") or []
        attempted = ignored = deferred = 0
        for item in items:
            try:
                video = _video_from_playlist_item(item)
            except (ValueError, TypeError, AttributeError) as error:
                logger.info(
                    "YouTube API poll item skipped poll=%d error_type=%s error=%s",
                    poll_id,
                    type(error).__name__,
                    _safe_text(error),
                )
                continue
            if video.video_id in self._api_seen_video_ids:
                continue
            if (
                video.published < self._activated_at
                or observed_at - video.published > YOUTUBE_NOTIFICATION_MAX_AGE
            ):
                # Backfill is intentionally off; RSS already covered these.
                self._api_seen_video_ids.add(video.video_id)
                ignored += 1
                continue
            if video.published > observed_at + YOUTUBE_NOTIFICATION_FUTURE_TOLERANCE:
                # Scheduled premieres can be listed before they go live.
                deferred += 1
                continue
            attempted += 1
            try:
                await self._process_video(
                    video, received_at=observed_at, trigger="youtube:api"
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                deferred += 1
                logger.info(
                    "YouTube API poll video deferred poll=%d video_id=%s "
                    "error_type=%s error=%s",
                    poll_id,
                    video.video_id,
                    type(error).__name__,
                    _safe_text(error),
                )
            else:
                self._api_seen_video_ids.add(video.video_id)
        log = (
            logger.info
            if attempted or ignored or deferred or mode == "window"
            else logger.debug
        )
        log(
            "YouTube API poll finished poll=%d mode=%s status=%d items=%d "
            "attempted=%d ignored=%d deferred=%d seen=%d elapsed_ms=%.1f",
            poll_id,
            mode,
            status,
            len(items),
            attempted,
            ignored,
            deferred,
            len(self._api_seen_video_ids),
            (time.perf_counter() - started) * 1000,
        )

    def _record_api_failure(self, poll_id, detail, started):
        alert = self._api_alerter.record_failure()
        logger.info(
            "YouTube API poll failed poll=%d %s consecutive_failures=%d elapsed_ms=%.1f",
            poll_id,
            detail,
            self._api_alerter.failures,
            (time.perf_counter() - started) * 1000,
        )
        if alert:
            logger.warning(
                "YouTube API polling outage consecutive_failures=%d since=%s "
                "last=%s next_alert_hours=%.0f",
                self._api_alerter.failures,
                self._api_alerter.started_at.isoformat(),
                detail,
                YOUTUBE_ALERT_INTERVAL_SECONDS / 3600,
            )

    async def _subscription_loop(self):
        await self.bot.wait_until_ready()
        failures = 0
        while True:
            renew_seconds = await self._subscribe_once(attempt=failures + 1)
            if renew_seconds is not None:
                alerted, failures_seen, outage_seconds = (
                    self._websub_alerter.record_success()
                )
                if alerted:
                    logger.warning(
                        "YouTube WebSub subscription recovered failures=%d "
                        "outage_hours=%.1f",
                        failures_seen,
                        outage_seconds / 3600,
                    )
                elif failures_seen:
                    logger.info(
                        "YouTube WebSub subscription recovered failures=%d",
                        failures_seen,
                    )
                failures = 0
                await asyncio.sleep(renew_seconds)
            else:
                failures += 1
                retry_seconds = _subscription_retry_delay(
                    failures, self._hub_retry_after
                )
                alert = self._websub_alerter.record_failure()
                logger.info(
                    "YouTube WebSub subscription retry scheduled failures=%d "
                    "retry_seconds=%d hub_retry_after=%s",
                    failures, retry_seconds, self._hub_retry_after,
                )
                if alert:
                    logger.warning(
                        "YouTube WebSub subscription outage failures=%d since=%s "
                        "last_outcome=%s retry_seconds=%d next_alert_hours=%.0f",
                        failures,
                        self._websub_alerter.started_at.isoformat(),
                        self._last_subscription_outcome,
                        retry_seconds,
                        YOUTUBE_ALERT_INTERVAL_SECONDS / 3600,
                    )
                await asyncio.sleep(retry_seconds)

    async def _subscribe_once(self, *, attempt):
        self._verified_event.clear()
        self._verified_at_monotonic = None
        self._lease_seconds = None
        self._hub_retry_after = None
        self._pending_subscription = True
        started = time.perf_counter()
        status = None
        body_text = "none"
        stage = "await-response-headers"
        logger.info(
            "YouTube WebSub subscription request starting attempt=%d hub=%s "
            "callback=%s topic=%s verify=async secret_bytes=%d timeout_seconds=%s",
            attempt,
            YOUTUBE_HUB_URL,
            YOUTUBE_CALLBACK_URL,
            YOUTUBE_TOPIC_URL,
            len(YOUTUBE_SECRET.encode()) if YOUTUBE_SECRET else 0,
            YOUTUBE_SUBSCRIPTION_TIMEOUT.total,
        )
        try:
            try:
                async with self._session.post(
                    YOUTUBE_HUB_URL,
                    data={
                        "hub.callback": YOUTUBE_CALLBACK_URL,
                        "hub.mode": "subscribe",
                        "hub.verify": "async",
                        "hub.topic": YOUTUBE_TOPIC_URL,
                        "hub.secret": YOUTUBE_SECRET,
                    },
                    allow_redirects=False,
                    timeout=YOUTUBE_SUBSCRIPTION_TIMEOUT,
                ) as response:
                    status = response.status
                    self._hub_retry_after = _parse_retry_after(
                        response.headers.get("Retry-After")
                    )
                    logger.info(
                        "YouTube WebSub subscription response headers attempt=%d "
                        "status=%d elapsed_ms=%.1f verification_received=%s "
                        "retry_after=%s headers=%s",
                        attempt, status, (time.perf_counter() - started) * 1000,
                        self._verified_event.is_set(), self._hub_retry_after,
                        _safe_text(dict(response.headers), 1500),
                    )
                    stage = "read-response-body"
                    body = await response.read()
                    body_text = _safe_text(body.decode("utf-8", "replace"), 1000)
                    logger.info(
                        "YouTube WebSub subscription response complete attempt=%d "
                        "status=%d body_bytes=%d elapsed_ms=%.1f body=%r",
                        attempt, status, len(body),
                        (time.perf_counter() - started) * 1000, body_text,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A timed-out request may still have been accepted by the hub.
                # Keep verification open for a bounded grace period, and don't
                # discard a challenge that arrived while the POST was in flight.
                self._last_subscription_outcome = (
                    f"request-failed stage={stage} status={status} "
                    f"error_type={type(error).__name__}"
                )
                logger.info(
                    "YouTube WebSub subscription request failed attempt=%d "
                    "stage=%s status=%s error_type=%s elapsed_ms=%.1f "
                    "verification_received=%s",
                    attempt, stage, status, type(error).__name__,
                    (time.perf_counter() - started) * 1000,
                    self._verified_event.is_set(),
                )
            if (
                status is not None
                and not 200 <= status < 300
                and not self._verified_event.is_set()
            ):
                self._last_subscription_outcome = (
                    f"rejected status={status} retry_after={self._hub_retry_after} "
                    f"body={body_text!r}"
                )
                logger.info(
                    "YouTube WebSub subscription request rejected attempt=%d "
                    "status=%d retry_after=%s body=%r",
                    attempt, status, self._hub_retry_after, body_text,
                )
                return None

            if not self._verified_event.is_set():
                logger.info(
                    "YouTube WebSub subscription awaiting verification attempt=%d "
                    "wait_seconds=%d request_status=%s",
                    attempt, YOUTUBE_VERIFICATION_WAIT_SECONDS, status,
                )
                try:
                    await asyncio.wait_for(
                        self._verified_event.wait(),
                        timeout=YOUTUBE_VERIFICATION_WAIT_SECONDS,
                    )
                except TimeoutError:
                    self._last_subscription_outcome = (
                        f"verification-timeout request_status={status}"
                    )
                    logger.info(
                        "YouTube WebSub subscription verification timed out attempt=%d",
                        attempt,
                    )
                    return None

            lease_seconds = self._lease_seconds or 5 * 24 * 60 * 60
            # The lease starts at verification, not when a slow POST finishes.
            verified_at = self._verified_at_monotonic
            elapsed_since_verification = (
                time.monotonic() - verified_at if verified_at is not None else 0
            )
            renew_seconds = max(
                0, _renewal_delay(lease_seconds) - elapsed_since_verification
            )
            self._last_subscription_outcome = "active"
            logger.info(
                "YouTube WebSub subscription active lease_seconds=%d "
                "renew_seconds=%.3f",
                lease_seconds,
                renew_seconds,
            )
            return renew_seconds
        finally:
            self._pending_subscription = False


def _video_from_api(item) -> YouTubeVideo:
    snippet = item["snippet"]
    content_details = item.get("contentDetails", {})
    status = item.get("status", {})
    thumbnail_url = _preferred_thumbnail(snippet.get("thumbnails"))
    published = _parse_datetime(snippet.get("publishedAt"))
    if published is None:
        raise ValueError("YouTube API response is missing snippet.publishedAt")
    return YouTubeVideo(
        video_id=item["id"],
        channel_id=snippet["channelId"],
        title=snippet["title"],
        link=f"https://www.youtube.com/watch?v={item['id']}",
        author=snippet.get("channelTitle"),
        description=snippet.get("description"),
        thumbnail_url=thumbnail_url,
        duration_seconds=_parse_iso_duration(content_details.get("duration")),
        published=published,
        updated=None,
        privacy_status=status.get("privacyStatus"),
    )


def _video_from_playlist_item(item) -> YouTubeVideo:
    """Build a YouTubeVideo from a playlistItems.list item (no duration yet)."""
    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}
    video_id = content_details.get("videoId") or (
        snippet.get("resourceId") or {}
    ).get("videoId")
    published = _parse_datetime(
        content_details.get("videoPublishedAt") or snippet.get("publishedAt")
    )
    if not video_id or published is None:
        raise ValueError("YouTube playlist item is missing videoId or publish time")
    return YouTubeVideo(
        video_id=video_id,
        channel_id=(
            snippet.get("videoOwnerChannelId")
            or snippet.get("channelId")
            or YOUTUBE_CHANNEL_ID
        ),
        title=snippet.get("title") or "",
        link=f"https://www.youtube.com/watch?v={video_id}",
        author=snippet.get("channelTitle"),
        description=snippet.get("description"),
        thumbnail_url=_preferred_thumbnail(snippet.get("thumbnails")),
        duration_seconds=None,
        published=published,
        updated=None,
        privacy_status=(item.get("status") or {}).get("privacyStatus"),
    )


def _preferred_thumbnail(thumbnails) -> str | None:
    thumbnails = thumbnails or {}
    return next(
        (
            thumbnails[name].get("url")
            for name in ("maxres", "standard", "high", "medium", "default")
            if (thumbnails.get(name) or {}).get("url")
        ),
        None,
    )


def _element_text(parent: ET.Element | None, path: str) -> str | None:
    if parent is None:
        return None
    element = parent.find(path)
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def _parse_datetime(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("YouTube timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def _parse_iso_duration(value: str | None) -> int | None:
    if not value or not (match := ISO_DURATION_RE.fullmatch(value)):
        return None
    parts = {name: int(number or 0) for name, number in match.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def _safe_text(value, limit: int = 300) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def setup(bot):
    await bot.add_cog(YouTubeWebSub(bot))
