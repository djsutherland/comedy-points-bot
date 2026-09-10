import datetime
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from comedypoints.episode_dedupe import EpisodeClaimStore
from comedypoints.ep_poster import (
    EpisodeCandidate,
    EpPoster,
    FeedItemMetadata,
    FeedMetadata,
    _episode_date_for_post,
    _normalize_episode_title,
)


UTC = datetime.timezone.utc


class EpisodeClaimStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = EpisodeClaimStore(f"{self.tempdir.name}/claims.sqlite")
        self.store.initialize()
        self.now = datetime.datetime(2026, 9, 6, 4, 0, tzinfo=UTC)

    def tearDown(self):
        self.tempdir.cleanup()

    def claim(self, title, source, source_id, *, now=None):
        return self.store.claim(
            normalized_title=_normalize_episode_title(title),
            display_title=title,
            source=source,
            source_id=source_id,
            published_at=self.now,
            now=now or self.now,
        )

    def test_ad_free_rss_title_matches_youtube_title(self):
        youtube = self.claim("A Public Episode", "youtube", "video-1")
        self.assertTrue(youtube.claimed)
        self.store.complete(
            normalized_title=youtube.normalized_title,
            source=youtube.source,
            source_id=youtube.source_id,
            message_id=123,
            posted_at=self.now,
        )

        rss = self.claim("A Public Episode (Ad-Free)", "rss", "rss-1")
        self.assertFalse(rss.claimed)
        self.assertEqual(rss.source, "youtube")
        self.assertEqual(rss.source_id, "video-1")
        self.assertEqual(rss.message_id, 123)

    def test_patreon_only_episode_on_same_day_gets_a_separate_claim(self):
        public = self.claim("A Public Episode", "youtube", "video-1")
        patreon = self.claim("A Patreon Bonus (Ad-Free)", "rss", "rss-2")
        self.assertTrue(public.claimed)
        self.assertTrue(patreon.claimed)

    def test_repeat_source_id_is_deduplicated_even_if_title_changes(self):
        self.assertTrue(self.claim("Original Title", "youtube", "video-1").claimed)
        duplicate = self.claim("Corrected Title", "youtube", "video-1")
        self.assertFalse(duplicate.claimed)
        self.assertEqual(duplicate.display_title, "Original Title")

    def test_released_pending_claim_can_be_retried(self):
        claim = self.claim("Retry Me", "rss", "rss-1")
        self.store.release(
            normalized_title=claim.normalized_title,
            source=claim.source,
            source_id=claim.source_id,
        )
        self.assertTrue(self.claim("Retry Me", "rss", "rss-1").claimed)

    def test_pending_claim_is_not_expired_before_reconciliation(self):
        self.claim("Interrupted Episode", "youtube", "video-1")
        duplicate = self.claim(
            "Interrupted Episode", "rss", "rss-1",
            now=self.now + datetime.timedelta(days=31),
        )
        self.assertFalse(duplicate.claimed)
        self.assertIsNone(duplicate.posted_at)

    def test_source_identity_wins_over_title_collision(self):
        self.claim("Episode A", "rss", "rss-a")
        self.claim("Episode A", "youtube", "video-a")
        self.claim("Episode B", "rss", "rss-b")
        renamed = self.claim("Episode B", "youtube", "video-a")
        self.assertFalse(renamed.claimed)
        self.assertEqual(renamed.display_title, "Episode A")

    def test_old_titles_can_be_reused_after_retention_window(self):
        old = self.claim("A Reused Title", "youtube", "video-old")
        self.store.complete(
            normalized_title=old.normalized_title,
            source=old.source,
            source_id=old.source_id,
            message_id=123,
            posted_at=self.now,
        )
        future = self.now + datetime.timedelta(days=31)
        new = self.claim(
            "A Reused Title", "youtube", "video-new", now=future
        )
        self.assertTrue(new.claimed)


class TitleNormalizationTests(unittest.TestCase):
    def test_only_terminal_ad_free_suffix_is_removed(self):
        self.assertEqual(
            _normalize_episode_title("  Resident Evil: Extinction (Ad-Free) "),
            "resident evil extinction",
        )
        self.assertEqual(
            _normalize_episode_title("The (Ad-Free) Discussion"),
            "the ad free discussion",
        )

    def test_youtube_punctuation_differences_match_feed_titles(self):
        # Real pairs from the channel's videos tab and the public feed.
        pairs = [
            ("New York New York with Lin Manuel Miranda",
             "New York, New York\twith Lin-Manuel Miranda (Ad-Free)"),
            ("Alice Doesnt Live Here Anymore with Katey Rich",
             "Alice Doesn't Live Here Anymore with Katey Rich (Ad-Free)"),
            ("In The Blink of an Eye with Joey Sims",
             "In the Blink of an Eye with Joey Sims"),
            ("Who's That Knocking at My Door / Boxcar Bertha",
             "Who\u2019s That Knocking at My Door / Boxcar Bertha"),
            ("WALL-E with David Ehrlich", "WALL-E with David Ehrlich (AD-FREE)"),
        ]
        for youtube, rss in pairs:
            self.assertEqual(
                _normalize_episode_title(youtube), _normalize_episode_title(rss), youtube
            )

    def test_distinct_titles_stay_distinct(self):
        self.assertNotEqual(
            _normalize_episode_title("Toy Story 5"), _normalize_episode_title("Toy Story 4")
        )
        self.assertNotEqual(
            _normalize_episode_title("Finding Dory with Zach Cherry"),
            _normalize_episode_title("Finding Nemo with Rebecca Alter"),
        )


class PostDateAttributionTests(unittest.TestCase):
    def test_posts_just_before_midnight_count_for_the_next_release(self):
        ny = ZoneInfo("America/New_York")
        saturday, sunday = datetime.date(2026, 9, 5), datetime.date(2026, 9, 6)
        cases = [
            (datetime.datetime(2026, 9, 5, 23, 59, 50, tzinfo=ny), sunday),
            (datetime.datetime(2026, 9, 5, 23, 55, 1, tzinfo=ny), sunday),
            (datetime.datetime(2026, 9, 5, 23, 50, 0, tzinfo=ny), saturday),
            (datetime.datetime(2026, 9, 6, 0, 0, 30, tzinfo=ny), sunday),
            (datetime.datetime(2026, 9, 6, 3, 59, 59, tzinfo=UTC), sunday),
            (datetime.datetime(2026, 9, 6, 12, 0, 0, tzinfo=ny), sunday),
        ]
        for posted_at, expected in cases:
            self.assertEqual(_episode_date_for_post(posted_at), expected, posted_at)


class _FakeMessage:
    def __init__(self, message_id):
        self.id = message_id


class _FakeRole:
    mention = "<@&123>"


class _FakeGuild:
    def get_role(self, role_id):
        return _FakeRole()


class _FakeChannel:
    def __init__(self):
        self.guild = _FakeGuild()
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return _FakeMessage(len(self.sent))


class _FakeBot:
    def __init__(self):
        self.channel = _FakeChannel()

    def get_channel(self, channel_id):
        return self.channel


class EpisodePostingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.bot = _FakeBot()
        self.poster = EpPoster(self.bot)
        self.poster._claim_store = EpisodeClaimStore(
            f"{self.tempdir.name}/claims.sqlite"
        )
        self.poster._claim_store.initialize()
        self.published = datetime.datetime(2026, 9, 6, 4, 0, tzinfo=UTC)

    async def asyncTearDown(self):
        self.poster._reader_executor.shutdown(wait=False, cancel_futures=True)
        self.tempdir.cleanup()

    def candidate(self, source, source_id, title):
        return EpisodeCandidate(
            source=source,
            source_id=source_id,
            title=title,
            feed_title="Blank Check",
            summary="Description",
            link="https://example.invalid/episode",
            image_url=None,
            duration_seconds=3600,
            published=self.published,
            observed_at=self.published + datetime.timedelta(seconds=5),
        )

    async def test_public_race_dedupes_but_same_day_patreon_episode_posts(self):
        youtube = await self.poster._post_candidate(
            self.candidate("youtube", "video-1", "A Public Episode"),
            trigger="test",
        )
        rss_duplicate = await self.poster._post_candidate(
            self.candidate("rss", "rss-1", "A Public Episode (Ad-Free)"),
            trigger="test",
        )
        patreon = await self.poster._post_candidate(
            self.candidate("rss", "rss-2", "A Patreon Bonus (Ad-Free)"),
            trigger="test",
        )

        self.assertTrue(youtube)
        self.assertFalse(rss_duplicate)
        self.assertTrue(patreon)
        card_sends = [kwargs for _, kwargs in self.bot.channel.sent if "view" in kwargs]
        self.assertEqual(len(card_sends), 2)
        self.assertEqual(sum(self.poster._episode_posts_by_date.values()), 2)


class FeedMetadataCacheTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = EpisodePostingIntegrationTests.asyncSetUp
    asyncTearDown = EpisodePostingIntegrationTests.asyncTearDown
    FEED = "https://example.invalid/feed.xml"

    def entry(self, guid):
        return SimpleNamespace(feed_url=self.FEED, id=guid, link=None, title=None)

    def metadata(self, *guids):
        return FeedMetadata(
            title="Feed",
            items=tuple(FeedItemMetadata(id=g, duration_seconds=3600) for g in guids),
        )

    async def test_failed_fetch_is_retried_once_and_never_cached(self):
        fetch = Mock(side_effect=[OSError("timeout"), OSError("timeout"), self.metadata("a")])
        with patch("comedypoints.ep_poster._fetch_feed_metadata", fetch), \
             patch("comedypoints.ep_poster.FEED_FETCH_RETRY_SECONDS", 0):
            _, item = await self.poster._get_item_metadata(self.entry("a"))
            self.assertIsNone(item.duration_seconds)
            self.assertNotIn(self.FEED, self.poster._feed_cache)
            _, item = await self.poster._get_item_metadata(self.entry("a"))
        self.assertEqual(item.duration_seconds, 3600)
        self.assertEqual(fetch.call_count, 3)

    async def test_stale_cache_is_refreshed_for_a_new_entry(self):
        fetch = Mock(side_effect=[self.metadata("a"), self.metadata("a", "b")])
        with patch("comedypoints.ep_poster._fetch_feed_metadata", fetch):
            _, item_a = await self.poster._get_item_metadata(self.entry("a"))
            _, item_b = await self.poster._get_item_metadata(self.entry("b"))
            _, item_b_again = await self.poster._get_item_metadata(self.entry("b"))
        self.assertEqual(item_a.id, "a")
        self.assertEqual(item_b.id, "b")
        self.assertEqual(item_b_again.id, "b")
        self.assertEqual(fetch.call_count, 2)

    async def test_fresh_fetch_without_the_entry_is_not_refetched(self):
        fetch = Mock(return_value=self.metadata("a"))
        with patch("comedypoints.ep_poster._fetch_feed_metadata", fetch):
            _, item = await self.poster._get_item_metadata(self.entry("missing"))
        self.assertEqual(item.id, "[unknown]")
        self.assertEqual(fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
