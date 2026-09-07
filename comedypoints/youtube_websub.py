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
from .ep_poster import EPISODE_CLAIMS_DB_PATH


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

    async def cog_load(self):
        missing = [
            name
            for name, value in (
                ("YOUTUBE_WEBSUB_CALLBACK_URL", YOUTUBE_CALLBACK_URL),
                ("YOUTUBE_WEBSUB_SECRET", YOUTUBE_SECRET),
                ("YOUTUBE_API_KEY", YOUTUBE_API_KEY),
            )
            if not value
        ]
        if missing:
            logger.warning(
                "YouTube WebSub disabled missing_config=%s channel_id=%s",
                ",".join(missing),
                YOUTUBE_CHANNEL_ID,
            )
            return
        if len(YOUTUBE_SECRET.encode()) >= 200:
            raise ValueError("YOUTUBE_WEBSUB_SECRET must be under 200 bytes")

        callback = urlsplit(YOUTUBE_CALLBACK_URL)
        if callback.scheme != "https":
            logger.warning(
                "YouTube WebSub callback is not HTTPS scheme=%s", callback.scheme
            )
        if callback.query or callback.fragment or not callback.netloc:
            raise ValueError("YOUTUBE_WEBSUB_CALLBACK_URL must have a host and no query/fragment")
        callback_path = callback.path or "/"

        await asyncio.to_thread(self._inbox.initialize)
        self._activated_at = await asyncio.to_thread(self._inbox.activated_at)
        logger.info("YouTube WebSub activation cutoff=%s", self._activated_at.isoformat())

        self._session = ClientSession(timeout=YOUTUBE_HTTP_TIMEOUT)
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
            "channel_id=%s api_metadata=%s",
            YOUTUBE_BIND_HOST,
            YOUTUBE_BIND_PORT,
            YOUTUBE_CHANNEL_ID,
            "enabled" if YOUTUBE_API_KEY else "atom-fallback",
        )
        self._worker_task = asyncio.create_task(
            self._inbox_loop(), name="youtube-websub-inbox"
        )
        self._subscription_task = asyncio.create_task(
            self._subscription_loop(), name="youtube-websub-subscription"
        )

    async def cog_unload(self):
        # Stop accepting callbacks before draining/cancelling workers. Already
        # acknowledged work stays in SQLite and resumes on the next startup.
        if self._runner is not None:
            await self._runner.cleanup()
        tasks_to_stop = [self._worker_task] if self._worker_task else []
        if self._subscription_task is not None:
            tasks_to_stop.append(self._subscription_task)
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
        self, video: YouTubeVideo, *, received_at: datetime.datetime
    ):
        started = time.perf_counter()
        published_lag = (received_at - video.published).total_seconds()
        updated_lag = (
            (received_at - video.updated).total_seconds()
            if video.updated is not None
            else None
        )
        logger.info(
            "YouTube WebSub entry processing video_id=%s channel_matches=%s "
            "title=%r published=%s updated=%s received_at=%s "
            "published_lag_seconds=%.3f updated_lag_seconds=%s",
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
                "YouTube WebSub entry ignored video_id=%s reason=wrong-channel",
                video.video_id,
            )
            return
        # Persisted once, not reset on restart. Old title/description edits must
        # not repost episodes from before this deployment had a claim ledger.
        if video.published < self._activated_at:
            logger.info("YouTube WebSub entry ignored video_id=%s reason=before-activation",
                        video.video_id)
            return
        if published_lag > YOUTUBE_NOTIFICATION_MAX_AGE.total_seconds():
            logger.info(
                "YouTube WebSub entry ignored video_id=%s reason=old-update "
                "published_lag_seconds=%.3f",
                video.video_id,
                published_lag,
            )
            return
        if published_lag < -YOUTUBE_NOTIFICATION_FUTURE_TOLERANCE.total_seconds():
            logger.warning(
                "YouTube WebSub entry ignored video_id=%s reason=future-publication "
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
            logger.warning("YouTube WebSub entry ignored video_id=%s reason=api-channel-mismatch",
                           video.video_id)
            return
        if video.published < self._activated_at:
            logger.info("YouTube WebSub entry ignored video_id=%s reason=api-before-activation",
                        video.video_id)
            return
        if video.privacy_status != "public":
            raise RuntimeError("YouTube video not public yet; retry required")
        if not video.title or not video.description or not video.duration_seconds:
            raise RuntimeError("YouTube metadata incomplete; retry required")

        poster = self.bot.get_cog("EpPoster")
        if poster is None:
            logger.error(
                "YouTube WebSub entry ignored video_id=%s reason=poster-unavailable",
                video.video_id,
            )
            raise RuntimeError("Episode poster unavailable; retry required")
        try:
            posted = await poster.post_youtube_video(
                video, webhook_received_at=received_at
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "YouTube WebSub entry processing failed video_id=%s "
                "total_elapsed_ms=%.1f",
                video.video_id,
                (time.perf_counter() - started) * 1000,
            )
            raise
        logger.info(
            "YouTube WebSub entry processing finished video_id=%s posted=%s "
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

    async def _subscription_loop(self):
        await self.bot.wait_until_ready()
        failures = 0
        while True:
            renew_seconds = await self._subscribe_once(attempt=failures + 1)
            if renew_seconds is not None:
                failures = 0
                await asyncio.sleep(renew_seconds)
            else:
                failures += 1
                retry_seconds = _subscription_retry_delay(
                    failures, self._hub_retry_after
                )
                logger.warning(
                    "YouTube WebSub subscription retry scheduled failures=%d "
                    "retry_seconds=%d hub_retry_after=%s",
                    failures, retry_seconds, self._hub_retry_after,
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
                logger.warning(
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
                logger.warning(
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
                    logger.warning(
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
    thumbnails = snippet.get("thumbnails", {})
    thumbnail_url = next(
        (
            thumbnails[name].get("url")
            for name in ("maxres", "standard", "high", "medium", "default")
            if thumbnails.get(name, {}).get("url")
        ),
        None,
    )
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
