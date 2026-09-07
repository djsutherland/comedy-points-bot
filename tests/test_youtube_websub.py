import hashlib
import hmac
import unittest

from comedypoints.youtube_websub import (
    YOUTUBE_CHANNEL_ID,
    _parse_iso_duration,
    _video_from_api,
    parse_youtube_notification,
    verify_websub_signature,
)


SAMPLE_NOTIFICATION = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>yt:video:video-1</id>
    <yt:videoId>video-1</yt:videoId>
    <yt:channelId>{YOUTUBE_CHANNEL_ID}</yt:channelId>
    <title>A Public Episode</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=video-1"/>
    <author><name>Blank Check with Griffin &amp; David</name></author>
    <published>2026-09-06T04:00:03+00:00</published>
    <updated>2026-09-06T04:00:05+00:00</updated>
    <media:group>
      <media:thumbnail url="https://example.invalid/thumb.jpg"/>
      <media:description>An episode description.</media:description>
    </media:group>
  </entry>
</feed>
""".encode()


class YouTubeNotificationTests(unittest.TestCase):
    def test_parses_documented_and_media_fields(self):
        (video,) = parse_youtube_notification(SAMPLE_NOTIFICATION)
        self.assertEqual(video.video_id, "video-1")
        self.assertEqual(video.channel_id, YOUTUBE_CHANNEL_ID)
        self.assertEqual(video.title, "A Public Episode")
        self.assertEqual(video.description, "An episode description.")
        self.assertEqual(video.thumbnail_url, "https://example.invalid/thumb.jpg")
        self.assertEqual(video.published.isoformat(), "2026-09-06T04:00:03+00:00")

    def test_verifies_supported_hmac_signature(self):
        secret = "test-secret"
        digest = hmac.new(secret.encode(), SAMPLE_NOTIFICATION, hashlib.sha256).hexdigest()
        self.assertTrue(
            verify_websub_signature(
                SAMPLE_NOTIFICATION, f"sha256={digest}", secret
            )
        )
        self.assertFalse(
            verify_websub_signature(
                SAMPLE_NOTIFICATION + b" ", f"sha256={digest}", secret
            )
        )
        self.assertFalse(
            verify_websub_signature(SAMPLE_NOTIFICATION, None, secret)
        )

    def test_api_metadata_supplies_description_thumbnail_and_duration(self):
        video = _video_from_api(
            {
                "id": "video-1",
                "snippet": {
                    "channelId": YOUTUBE_CHANNEL_ID,
                    "channelTitle": "Blank Check with Griffin & David",
                    "title": "A Public Episode",
                    "description": "Description from the API.",
                    "publishedAt": "2026-09-06T04:00:03Z",
                    "thumbnails": {
                        "high": {"url": "https://example.invalid/high.jpg"}
                    },
                },
                "contentDetails": {"duration": "PT3H12M9S"},
                "status": {"privacyStatus": "public"},
            }
        )
        self.assertEqual(video.duration_seconds, 11529)
        self.assertEqual(video.privacy_status, "public")
        self.assertEqual(video.thumbnail_url, "https://example.invalid/high.jpg")

    def test_iso_duration_supports_days(self):
        self.assertEqual(_parse_iso_duration("P1DT2H3M4S"), 93784)
        self.assertIsNone(_parse_iso_duration("not-a-duration"))


if __name__ == "__main__":
    unittest.main()
