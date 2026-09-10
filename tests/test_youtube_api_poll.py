import asyncio
import datetime
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from comedypoints.episode_dedupe import WebSubInbox
from comedypoints.youtube_websub import (
    OutageAlerter,
    YouTubeVideo,
    YouTubeWebSub,
    YOUTUBE_CHANNEL_ID,
    _api_poll_delay,
    _video_from_playlist_item,
)
import test_episode_dedupe as posting_tests


UTC = datetime.timezone.utc
NY = ZoneInfo("America/New_York")
# 2026-09-06 is a Sunday.
SUNDAY = datetime.date(2026, 9, 6)


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def playlist_item(video_id, published, title="A Public Episode", owner=YOUTUBE_CHANNEL_ID):
    return {
        "snippet": {
            "publishedAt": _iso(published),
            "title": title,
            "description": "Description",
            "thumbnails": {
                "default": {"url": "https://example.invalid/default.jpg"},
                "maxres": {"url": "https://example.invalid/maxres.jpg"},
            },
            "resourceId": {"videoId": video_id},
            "videoOwnerChannelId": owner,
            "channelTitle": "Blank Check with Griffin & David",
        },
        "contentDetails": {"videoId": video_id, "videoPublishedAt": _iso(published)},
        "status": {"privacyStatus": "public"},
    }


class ResponseContext:
    def __init__(self, status=200, payload=None, text=""):
        self.response = SimpleNamespace(
            status=status,
            json=AsyncMock(return_value=payload),
            text=AsyncMock(return_value=text),
        )

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return False


class PollScheduleTests(unittest.TestCase):
    def at(self, date, hour, minute, second):
        return datetime.datetime.combine(
            date, datetime.time(hour, minute, second), tzinfo=NY
        )

    def test_rapid_polling_only_around_the_sunday_window(self):
        saturday = SUNDAY - datetime.timedelta(days=1)
        self.assertEqual(_api_poll_delay(self.at(saturday, 23, 59, 45)), (5, "window"))
        self.assertEqual(_api_poll_delay(self.at(SUNDAY, 0, 0, 0)), (5, "window"))
        self.assertEqual(_api_poll_delay(self.at(SUNDAY, 0, 3, 0)), (5, "window"))
        self.assertEqual(_api_poll_delay(self.at(SUNDAY, 0, 10, 0)), (5, "window"))
        self.assertEqual(_api_poll_delay(self.at(SUNDAY, 0, 10, 1)), (60, "baseline"))
        self.assertEqual(_api_poll_delay(self.at(SUNDAY, 12, 0, 0)), (60, "baseline"))

    def test_other_days_stay_on_baseline_even_at_midnight(self):
        for offset in range(1, 7):
            day = SUNDAY + datetime.timedelta(days=offset)
            self.assertEqual(_api_poll_delay(self.at(day, 0, 3, 0)), (60, "baseline"), day)

    def test_baseline_shortens_to_enter_the_window_on_time(self):
        saturday = SUNDAY - datetime.timedelta(days=1)
        delay, mode = _api_poll_delay(self.at(saturday, 23, 58, 45))
        self.assertEqual(mode, "baseline")
        self.assertAlmostEqual(delay, 46, delta=0.01)


class PlaylistItemTests(unittest.TestCase):
    def test_builds_video_from_playlist_item(self):
        published = datetime.datetime(2026, 9, 6, 4, 0, 3, tzinfo=UTC)
        video = _video_from_playlist_item(playlist_item("video-1", published))
        self.assertEqual(video.video_id, "video-1")
        self.assertEqual(video.channel_id, YOUTUBE_CHANNEL_ID)
        self.assertEqual(video.published, published)
        self.assertEqual(video.link, "https://www.youtube.com/watch?v=video-1")
        self.assertEqual(video.thumbnail_url, "https://example.invalid/maxres.jpg")
        self.assertEqual(video.privacy_status, "public")
        self.assertIsNone(video.duration_seconds)

    def test_missing_video_id_is_rejected(self):
        with self.assertRaises(ValueError):
            _video_from_playlist_item({"snippet": {"publishedAt": "2026-09-06T04:00:00Z"}})


class PollOnceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.poster = SimpleNamespace(post_youtube_video=AsyncMock(return_value=True))
        self.cog = YouTubeWebSub(SimpleNamespace(
            wait_until_ready=AsyncMock(), get_cog=lambda name: self.poster,
        ))
        self.now = datetime.datetime.now(UTC)
        self.cog._activated_at = self.now - datetime.timedelta(days=1)
        self.cog._process_video = AsyncMock()
        self.key_patch = patch("comedypoints.youtube_websub.YOUTUBE_API_KEY", "key")
        self.key_patch.start()

    async def asyncTearDown(self):
        self.key_patch.stop()

    def respond(self, *contexts):
        self.cog._session = SimpleNamespace(get=Mock(side_effect=list(contexts)))

    def items(self, *items):
        return ResponseContext(200, {"items": list(items)})

    async def test_new_video_is_processed_once_with_api_trigger(self):
        fresh = playlist_item("video-1", self.now - datetime.timedelta(seconds=20))
        self.respond(self.items(fresh), self.items(fresh))
        with self.assertLogs("comedypoints.youtube_websub", level="INFO") as logs:
            await self.cog._api_poll_once(mode="window")
            await self.cog._api_poll_once(mode="window")
        self.cog._process_video.assert_awaited_once()
        kwargs = self.cog._process_video.await_args.kwargs
        self.assertEqual(kwargs["trigger"], "youtube:api")
        self.assertIsNotNone(kwargs["received_at"].tzinfo)
        self.assertEqual(self.cog._process_video.await_args.args[0].video_id, "video-1")
        self.assertEqual(self.cog._api_seen_video_ids, {"video-1"})
        self.assertTrue(any("attempted=1" in line for line in logs.output))

    async def test_old_and_pre_activation_videos_are_marked_seen_silently(self):
        old = playlist_item("old", self.now - datetime.timedelta(hours=7))
        before = playlist_item("before", self.cog._activated_at - datetime.timedelta(days=1))
        self.respond(self.items(old, before))
        await self.cog._api_poll_once(mode="baseline")
        self.cog._process_video.assert_not_awaited()
        self.assertEqual(self.cog._api_seen_video_ids, {"old", "before"})

    async def test_upcoming_video_waits_until_published(self):
        upcoming = playlist_item("premiere", self.now + datetime.timedelta(minutes=30))
        self.respond(self.items(upcoming))
        await self.cog._api_poll_once(mode="baseline")
        self.cog._process_video.assert_not_awaited()
        self.assertEqual(self.cog._api_seen_video_ids, set())

    async def test_deferred_video_is_retried_next_poll(self):
        fresh = playlist_item("video-1", self.now - datetime.timedelta(seconds=20))
        self.respond(self.items(fresh), self.items(fresh))
        self.cog._process_video = AsyncMock(
            side_effect=[RuntimeError("YouTube video not public yet; retry required"), None]
        )
        await self.cog._api_poll_once(mode="window")
        self.assertEqual(self.cog._api_seen_video_ids, set())
        await self.cog._api_poll_once(mode="window")
        self.assertEqual(self.cog._process_video.await_count, 2)
        self.assertEqual(self.cog._api_seen_video_ids, {"video-1"})

    async def test_end_to_end_posts_through_poster_with_api_metadata(self):
        del self.cog._process_video  # use the real implementation
        published = self.now - datetime.timedelta(seconds=20)
        self.respond(self.items(playlist_item("video-1", published)))
        api_video = YouTubeVideo(
            video_id="video-1", channel_id=YOUTUBE_CHANNEL_ID, title="A Public Episode",
            link="https://www.youtube.com/watch?v=video-1", author="Blank Check",
            description="Description", thumbnail_url=None, duration_seconds=3600,
            published=published, updated=None, privacy_status="public",
        )
        self.cog._fetch_video_metadata = AsyncMock(return_value=api_video)
        await self.cog._api_poll_once(mode="window")
        self.poster.post_youtube_video.assert_awaited_once()
        call = self.poster.post_youtube_video.await_args
        self.assertEqual(call.args[0].duration_seconds, 3600)
        self.assertEqual(call.kwargs["trigger"], "youtube:api")
        self.assertEqual(self.cog._api_seen_video_ids, {"video-1"})

    async def test_failures_alert_once_after_threshold_and_announce_recovery(self):
        self.cog._api_alerter = OutageAlerter(threshold=2, interval_seconds=3600)
        forbidden = ResponseContext(403, None, '{"error": {"message": "quotaExceeded"}}')
        self.respond(
            forbidden, ResponseContext(200, None), forbidden, self.items(),
        )
        self.cog._session.get.side_effect = [
            forbidden, RuntimeError("connection reset"), forbidden, self.items(),
        ]
        with self.assertLogs("comedypoints.youtube_websub", level="INFO") as logs:
            for _ in range(4):
                await self.cog._api_poll_once(mode="baseline")
        warnings = [line for line in logs.output if line.startswith("WARNING:")]
        self.assertEqual(len(warnings), 2, warnings)
        self.assertIn("polling outage consecutive_failures=2", warnings[0])
        self.assertIn("polling recovered failures=3", warnings[1])
        self.assertTrue(any("quotaExceeded" in line for line in logs.output))


class OutageAlerterTests(unittest.TestCase):
    def setUp(self):
        self.clock = 1000.0
        self.alerter = OutageAlerter(
            threshold=3, interval_seconds=86400, clock=lambda: self.clock
        )

    def test_alerts_at_threshold_then_daily(self):
        self.assertEqual(
            [self.alerter.record_failure() for _ in range(5)],
            [False, False, True, False, False],
        )
        self.clock += 86399
        self.assertFalse(self.alerter.record_failure())
        self.clock += 1
        self.assertTrue(self.alerter.record_failure())
        self.assertEqual(self.alerter.failures, 7)

    def test_recovery_reports_only_alerted_outages_and_resets(self):
        self.alerter.record_failure()
        self.assertEqual(self.alerter.record_success(), (False, 1, 0.0))
        for _ in range(3):
            self.alerter.record_failure()
        self.clock += 42
        alerted, failures, seconds = self.alerter.record_success()
        self.assertEqual((alerted, failures, seconds), (True, 3, 42.0))
        self.assertEqual(self.alerter.failures, 0)
        self.assertIsNone(self.alerter.started_at)
        self.assertEqual([self.alerter.record_failure() for _ in range(2)], [False, False])


class SubscriptionAlertTests(unittest.IsolatedAsyncioTestCase):
    async def test_outage_dms_once_and_recovery_once(self):
        cog = YouTubeWebSub(SimpleNamespace(wait_until_ready=AsyncMock()))
        cog._websub_alerter = OutageAlerter(threshold=3, interval_seconds=86400)
        cog._subscribe_once = AsyncMock(side_effect=[None, None, None, None, 120])
        with patch("comedypoints.youtube_websub.asyncio.sleep", new_callable=AsyncMock) as sleep:
            sleep.side_effect = [None, None, None, None, asyncio.CancelledError()]
            with self.assertLogs("comedypoints.youtube_websub", level="INFO") as logs:
                with self.assertRaises(asyncio.CancelledError):
                    await cog._subscription_loop()
        warnings = [line for line in logs.output if line.startswith("WARNING:")]
        self.assertEqual(len(warnings), 2, warnings)
        self.assertIn("subscription outage failures=3", warnings[0])
        self.assertIn("next_alert_hours=24", warnings[0])
        self.assertIn("subscription recovered failures=4", warnings[1])
        self.assertEqual(
            len([line for line in logs.output if "retry scheduled" in line]), 4
        )


class CogLoadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.cog = YouTubeWebSub(SimpleNamespace(wait_until_ready=AsyncMock()))
        self.cog._inbox = WebSubInbox(f"{self.tempdir.name}/state.sqlite")
        self.cog._api_poll_loop = AsyncMock()
        self.cog._inbox_loop = AsyncMock()
        self.cog._subscription_loop = AsyncMock()
        self.session = SimpleNamespace(close=AsyncMock())

    async def asyncTearDown(self):
        await self.cog.cog_unload()
        self.tempdir.cleanup()

    async def test_api_key_alone_polls_without_websub(self):
        with patch("comedypoints.youtube_websub.YOUTUBE_API_KEY", "key"), \
             patch("comedypoints.youtube_websub.YOUTUBE_CALLBACK_URL", None), \
             patch("comedypoints.youtube_websub.YOUTUBE_SECRET", None), \
             patch("comedypoints.youtube_websub.ClientSession", return_value=self.session), \
             patch("comedypoints.youtube_websub.web.AppRunner") as runner:
            with self.assertLogs("comedypoints.youtube_websub", level="INFO") as logs:
                await self.cog.cog_load()
        runner.assert_not_called()
        self.assertIsNotNone(self.cog._api_poll_task)
        self.assertIsNone(self.cog._subscription_task)
        self.assertIsNotNone(self.cog._activated_at)
        self.assertTrue(any("WebSub disabled" in line for line in logs.output))
        self.assertTrue(any("polling enabled" in line for line in logs.output))

    async def test_full_configuration_starts_websub_and_polling(self):
        runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock())
        site = SimpleNamespace(start=AsyncMock())
        with patch("comedypoints.youtube_websub.YOUTUBE_API_KEY", "key"), \
             patch("comedypoints.youtube_websub.YOUTUBE_CALLBACK_URL", "https://example.invalid/hook"), \
             patch("comedypoints.youtube_websub.YOUTUBE_SECRET", "secret"), \
             patch("comedypoints.youtube_websub.ClientSession", return_value=self.session), \
             patch("comedypoints.youtube_websub.web.AppRunner", return_value=runner), \
             patch("comedypoints.youtube_websub.web.TCPSite", return_value=site):
            await self.cog.cog_load()
        self.assertIsNotNone(self.cog._subscription_task)
        self.assertIsNotNone(self.cog._worker_task)
        self.assertIsNotNone(self.cog._api_poll_task)


class CrossSourceDedupeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = posting_tests.EpisodePostingIntegrationTests.asyncSetUp
    asyncTearDown = posting_tests.EpisodePostingIntegrationTests.asyncTearDown
    candidate = posting_tests.EpisodePostingIntegrationTests.candidate

    async def test_api_then_websub_then_rss_posts_once(self):
        video = YouTubeVideo(
            video_id="video-1", channel_id=YOUTUBE_CHANNEL_ID, title="A Public Episode",
            link="https://www.youtube.com/watch?v=video-1", author="Blank Check",
            description="Description", thumbnail_url=None, duration_seconds=3600,
            published=self.published, updated=None, privacy_status="public",
        )
        observed = self.published + datetime.timedelta(seconds=5)
        first = await self.poster.post_youtube_video(
            video, webhook_received_at=observed, trigger="youtube:api"
        )
        websub = await self.poster.post_youtube_video(
            video, webhook_received_at=observed, trigger="youtube:websub"
        )
        rss = await self.poster._post_candidate(
            self.candidate("rss", "rss-1", "A Public Episode (Ad-Free)"), trigger="test"
        )
        self.assertEqual((first, websub, rss), (True, False, False))
        card_sends = [kwargs for _, kwargs in self.bot.channel.sent if "view" in kwargs]
        self.assertEqual(len(card_sends), 1)
        self.assertEqual(sum(self.poster._episode_posts_by_date.values()), 1)
