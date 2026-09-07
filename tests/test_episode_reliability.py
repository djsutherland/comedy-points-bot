import asyncio
from dataclasses import replace
import datetime
import hashlib
import hmac
import io
import logging
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from comedypoints.episode_dedupe import EpisodeClaimStore, WebSubInbox
from comedypoints.log_redaction import PrivateFeedRedaction
from comedypoints.youtube_websub import (
    YouTubeWebSub, YOUTUBE_TOPIC_URL, _renewal_delay, parse_youtube_notification,
)
import test_episode_dedupe as posting_tests
from test_youtube_websub import SAMPLE_NOTIFICATION


UTC = datetime.timezone.utc


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = posting_tests.EpisodePostingIntegrationTests.asyncSetUp
    asyncTearDown = posting_tests.EpisodePostingIntegrationTests.asyncTearDown
    candidate = posting_tests.EpisodePostingIntegrationTests.candidate

    def pending(self, title="Public Episode"):
        return self.poster._claim_store.claim(
            normalized_title=title.casefold(), display_title=title,
            source="youtube", source_id="video-1", published_at=self.published,
            now=datetime.datetime.now(UTC) - datetime.timedelta(minutes=1),
        )

    def history(self, messages=(), error=None):
        async def history(**kwargs):
            if error:
                raise error
            for message in messages:
                yield message
        self.bot.channel.history = history
        self.bot.user = SimpleNamespace(id=42)

    async def test_crash_before_send_retries_from_rss(self):
        self.pending()
        self.history()
        posted = await self.poster._post_candidate(
            self.candidate("rss", "rss-1", "Public Episode (Ad-Free)"), trigger="test"
        )
        self.assertTrue(posted)
        self.assertEqual(len(self.bot.channel.sent), 2)

    async def test_crash_after_send_recovers_without_duplicate(self):
        self.pending()
        component = SimpleNamespace(to_dict=lambda: {
            "components": [{"content": "## Public Episode"}]
        })
        self.history([SimpleNamespace(
            id=77, author=SimpleNamespace(id=42), components=[component],
            created_at=datetime.datetime.now(UTC),
        )])
        posted = await self.poster._post_candidate(
            self.candidate("rss", "rss-1", "Public Episode (Ad-Free)"), trigger="test"
        )
        self.assertFalse(posted)
        self.assertEqual(self.bot.channel.sent, [])
        claim = self.poster._claim_store.claim(
            normalized_title="renamed", display_title="renamed", source="rss",
            source_id="rss-1", published_at=self.published,
        )
        self.assertEqual(claim.message_id, 77)

    async def test_unavailable_history_leaves_pending_for_retry(self):
        self.pending()
        self.history(error=PermissionError("history unavailable"))
        with self.assertRaises(PermissionError):
            await self.poster._post_candidate(
                self.candidate("rss", "rss-1", "Public Episode"), trigger="test"
            )
        self.assertEqual(self.bot.channel.sent, [])

    async def test_ambiguous_send_is_not_released(self):
        self.bot.channel.send = AsyncMock(side_effect=TimeoutError("send uncertain"))
        with self.assertRaises(TimeoutError):
            await self.poster._post_candidate(
                self.candidate("youtube", "video-1", "Public Episode"), trigger="test"
            )
        claim = self.poster._claim_store.claim(
            normalized_title="public episode", display_title="Public Episode",
            source="rss", source_id="rss-1", published_at=self.published,
        )
        self.assertFalse(claim.claimed)
        self.assertIsNone(claim.posted_at)

    async def test_real_concurrent_race_preserves_bonus_episode(self):
        results = await asyncio.gather(*[
            self.poster._post_candidate(candidate, trigger="test") for candidate in (
                self.candidate("youtube", "video-1", "Public Episode"),
                self.candidate("rss", "rss-1", "Public Episode (Ad-Free)"),
                self.candidate("rss", "rss-2", "Patreon Bonus"),
            )
        ])
        self.assertEqual(sum(results), 2)
        self.assertEqual(len(self.bot.channel.sent), 4)

    async def test_losing_source_title_edit_still_dedupes_after_restart(self):
        await self.poster._post_candidate(
            self.candidate("rss", "rss-1", "Public Episode (Ad-Free)"), trigger="test"
        )
        await self.poster._post_candidate(
            self.candidate("youtube", "video-1", "Public Episode"), trigger="test"
        )
        self.poster._claim_store = EpisodeClaimStore(self.poster._claim_store.path)
        self.poster._claim_store.initialize()
        posted = await self.poster._post_candidate(
            self.candidate("youtube", "video-1", "Corrected Public Episode"), trigger="test"
        )
        self.assertFalse(posted)
        self.assertEqual(len(self.bot.channel.sent), 2)


class InboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.inbox = WebSubInbox(f"{self.tempdir.name}/state.sqlite")
        self.inbox.initialize()
        self.poster = SimpleNamespace(post_youtube_video=AsyncMock(return_value=True))
        self.cog = YouTubeWebSub(SimpleNamespace(get_cog=lambda name: self.poster))
        self.cog._inbox = self.inbox
        self.video = parse_youtube_notification(SAMPLE_NOTIFICATION)[0]
        self.cog._activated_at = self.video.published - datetime.timedelta(seconds=1)
        self.received = self.video.published + datetime.timedelta(seconds=2)
        self.api_video = replace(self.video, duration_seconds=3600, privacy_status="public")
        self.cog._fetch_video_metadata = AsyncMock(return_value=self.api_video)

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    def enqueue(self):
        self.inbox.enqueue("digest", SAMPLE_NOTIFICATION, self.received)

    async def test_ack_only_after_durable_write_and_restart(self):
        signature = hmac.new(b"secret", SAMPLE_NOTIFICATION, hashlib.sha256).hexdigest()
        request = SimpleNamespace(
            read=AsyncMock(return_value=SAMPLE_NOTIFICATION),
            headers={"X-Hub-Signature": "sha256=" + signature},
        )
        with patch("comedypoints.youtube_websub.YOUTUBE_SECRET", "secret"):
            result = await self.cog._handle_notification(request)
        self.assertEqual(result.status, 204)
        reopened = WebSubInbox(self.inbox.path)
        reopened.initialize()
        self.assertEqual(reopened.next_delivery()[1], SAMPLE_NOTIFICATION)
        with patch.object(self.inbox, "enqueue", side_effect=OSError("disk full")):
            with patch("comedypoints.youtube_websub.YOUTUBE_SECRET", "secret"):
                result = await self.cog._handle_notification(request)
        self.assertEqual(result.status, 503)

    async def test_failure_retries_and_replay_does_not_requeue(self):
        self.enqueue()
        self.cog._process_video = AsyncMock(side_effect=RuntimeError("temporary failure"))
        await self.cog._process_next_delivery()
        self.assertIsNone(self.inbox.next_delivery())
        due = self.inbox.next_delivery(datetime.datetime.now(UTC) + datetime.timedelta(hours=1))
        self.assertEqual(due[3], 1)
        # Simulate the scheduled retry becoming due, without sleeping.
        with self.inbox._connect() as db:
            db.execute("UPDATE websub_inbox SET retry_at = received_at")
        self.cog._process_video = AsyncMock()
        await self.cog._process_next_delivery()
        self.enqueue()
        self.assertIsNone(self.inbox.next_delivery())

    async def test_cancelled_worker_keeps_delivery(self):
        self.enqueue()
        self.cog._process_video = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.cog._process_next_delivery()
        self.assertIsNotNone(WebSubInbox(self.inbox.path).next_delivery())

    async def test_metadata_failure_does_not_post_or_complete_delivery(self):
        self.enqueue()
        self.cog._fetch_video_metadata.return_value = None
        with patch("comedypoints.youtube_websub.YOUTUBE_API_KEY", "key"):
            await self.cog._process_next_delivery()
        self.poster.post_youtube_video.assert_not_awaited()
        due = self.inbox.next_delivery(datetime.datetime.now(UTC) + datetime.timedelta(hours=1))
        self.assertIsNotNone(due)

    async def test_incomplete_or_private_metadata_never_posts(self):
        with patch("comedypoints.youtube_websub.YOUTUBE_API_KEY", "key"):
            for change in ({"duration_seconds": None}, {"description": None},
                           {"privacy_status": "private"}):
                self.cog._fetch_video_metadata.return_value = replace(self.api_video, **change)
                with self.assertRaises(RuntimeError):
                    await self.cog._process_video(self.video, received_at=self.received)
        self.poster.post_youtube_video.assert_not_awaited()

    async def test_complete_metadata_posts(self):
        with patch("comedypoints.youtube_websub.YOUTUBE_API_KEY", "key"):
            await self.cog._process_video(self.video, received_at=self.received)
        self.poster.post_youtube_video.assert_awaited_once()

    async def test_missing_key_disables_listener(self):
        with patch("comedypoints.youtube_websub.YOUTUBE_API_KEY", None), \
             patch("comedypoints.youtube_websub.YOUTUBE_CALLBACK_URL", "https://example.invalid/hook"), \
             patch("comedypoints.youtube_websub.YOUTUBE_SECRET", "secret"), \
             patch("comedypoints.youtube_websub.ClientSession") as session:
            await self.cog.cog_load()
        session.assert_not_called()

    async def test_verification_accepts_short_lease_rejects_nonpositive(self):
        for lease, expected in (("60", 200), ("0", 404), ("-1", 404)):
            self.cog._pending_subscription = True
            self.cog._verified_event.clear()
            response = await self.cog._handle_verification(SimpleNamespace(query={
                "hub.mode": "subscribe", "hub.topic": YOUTUBE_TOPIC_URL,
                "hub.challenge": "test-challenge", "hub.lease_seconds": lease,
            }))
            self.assertEqual(response.status, expected)
            self.assertEqual(self.cog._verified_event.is_set(), expected == 200)

    async def test_pre_activation_episode_is_ignored_and_cutoff_persists(self):
        cutoff = self.inbox.activated_at()
        self.inbox.initialize()
        self.assertEqual(self.inbox.activated_at(), cutoff)
        self.cog._activated_at = self.video.published + datetime.timedelta(seconds=1)
        await self.cog._process_video(self.video, received_at=self.received)
        self.cog._fetch_video_metadata.assert_not_awaited()
        self.poster.post_youtube_video.assert_not_awaited()


class SmallReliabilityTests(unittest.TestCase):
    def test_short_leases_renew_before_expiration(self):
        for lease in (1, 60, 300, 3600, 432000):
            self.assertGreater(_renewal_delay(lease), 0)
            self.assertLess(_renewal_delay(lease), lease)
        for lease in (0, -1):
            with self.assertRaises(ValueError):
                _renewal_delay(lease)

    def test_private_feed_redacted_in_library_message_and_traceback(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.addFilter(PrivateFeedRedaction())
        logger = logging.Logger("reader")
        logger.addHandler(handler)
        url = "https://www.patreon.com/rss/test?auth=private-test-token"
        try:
            raise ValueError(url)
        except ValueError:
            logger.exception("parse %r failed", url)
        text = output.getvalue()
        self.assertNotIn("private-test-token", text)
        self.assertNotIn("patreon.com/rss", text)
        self.assertIn("ValueError", text)
        self.assertIn("[private RSS feed]", text)
