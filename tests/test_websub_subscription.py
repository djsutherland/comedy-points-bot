import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from comedypoints.youtube_websub import (
    YouTubeWebSub, YOUTUBE_HTTP_TIMEOUT, YOUTUBE_TOPIC_URL,
    _subscription_retry_delay,
)


class RequestContext:
    def __init__(self, response=None, enter=None):
        self.response = response
        self.enter = enter

    async def __aenter__(self):
        if self.enter:
            await self.enter()
        return self.response

    async def __aexit__(self, *args):
        return False


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = YouTubeWebSub(SimpleNamespace(wait_until_ready=AsyncMock()))

    def response(self, status=202, read=None, enter=None):
        response = SimpleNamespace(status=status, read=read or AsyncMock(return_value=b""))
        self.cog._session = SimpleNamespace(post=Mock(
            return_value=RequestContext(response, enter)
        ))

    async def verify(self):
        return await self.cog._handle_verification(SimpleNamespace(query={
            "hub.mode": "subscribe", "hub.topic": YOUTUBE_TOPIC_URL,
            "hub.challenge": "challenge", "hub.lease_seconds": "60",
        }))

    async def test_async_request_and_headers_logged_before_body_read(self):
        async def read():
            self.assertTrue(any("response headers" in line for line in logs.output))
            self.assertFalse(any("response complete" in line for line in logs.output))
            self.assertEqual((await self.verify()).status, 200)
            return b"accepted"
        self.response(read=read)
        with self.assertLogs("comedypoints.youtube_websub", level="INFO") as logs:
            result = await self.cog._subscribe_once(attempt=1)
        self.assertGreater(result, 0)
        self.assertLessEqual(result, 48)
        kwargs = self.cog._session.post.call_args.kwargs
        self.assertEqual(kwargs["data"]["hub.verify"], "async")
        self.assertEqual(kwargs["timeout"].total, 60)
        self.assertEqual(YOUTUBE_HTTP_TIMEOUT.total, 15)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertFalse(self.cog._pending_subscription)

    async def test_verification_after_request_timeout_is_accepted(self):
        request_failed = asyncio.Event()
        async def enter():
            request_failed.set()
            raise TimeoutError()
        self.response(enter=enter)
        with self.assertLogs("comedypoints.youtube_websub", level="INFO") as logs:
            task = asyncio.create_task(self.cog._subscribe_once(attempt=1))
            try:
                await asyncio.wait_for(request_failed.wait(), timeout=1)
                self.assertTrue(self.cog._pending_subscription)
                self.assertEqual((await self.verify()).status, 200)
                result = await asyncio.wait_for(task, timeout=1)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertGreater(result, 0)
        self.assertTrue(any("stage=await-response-headers" in line for line in logs.output))
        self.assertTrue(any("subscription active" in line for line in logs.output))

    async def test_verified_request_survives_body_timeout(self):
        async def read():
            await self.verify()
            raise TimeoutError()
        self.response(read=read)
        with self.assertLogs("comedypoints.youtube_websub", level="INFO") as logs:
            result = await self.cog._subscribe_once(attempt=1)
        self.assertGreater(result, 0)
        self.assertTrue(any("stage=read-response-body status=202" in line for line in logs.output))
        self.assertTrue(any("verification_received=True" in line for line in logs.output))

    async def test_failed_request_grace_expires_and_later_verification_is_rejected(self):
        self.response(enter=AsyncMock(side_effect=TimeoutError()))
        with patch("comedypoints.youtube_websub.YOUTUBE_VERIFICATION_WAIT_SECONDS", 0.001):
            with self.assertLogs("comedypoints.youtube_websub", level="INFO"):
                result = await self.cog._subscribe_once(attempt=1)
        self.assertIsNone(result)
        self.assertFalse(self.cog._pending_subscription)
        self.assertEqual((await self.verify()).status, 404)

    async def test_accepted_request_without_verification_is_not_active(self):
        self.response()
        with patch("comedypoints.youtube_websub.YOUTUBE_VERIFICATION_WAIT_SECONDS", 0.001):
            with self.assertLogs("comedypoints.youtube_websub", level="INFO"):
                result = await self.cog._subscribe_once(attempt=1)
        self.assertIsNone(result)
        self.assertFalse(self.cog._pending_subscription)

    async def test_rejected_request_does_not_wait_for_verification(self):
        self.response(status=400)
        with self.assertLogs("comedypoints.youtube_websub", level="INFO") as logs:
            result = await asyncio.wait_for(self.cog._subscribe_once(attempt=1), timeout=1)
        self.assertIsNone(result)
        self.assertFalse(any("awaiting verification" in line for line in logs.output))

    async def test_cancellation_clears_pending_state(self):
        self.response(enter=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await self.cog._subscribe_once(attempt=1)
        self.assertFalse(self.cog._pending_subscription)

    async def test_retry_backoff_resets_after_success(self):
        self.cog._subscribe_once = AsyncMock(side_effect=[None, None, 120, None])
        with patch("comedypoints.youtube_websub.asyncio.sleep", new_callable=AsyncMock) as sleep:
            sleep.side_effect = [None, None, None, asyncio.CancelledError()]
            with self.assertLogs("comedypoints.youtube_websub", level="WARNING"):
                with self.assertRaises(asyncio.CancelledError):
                    await self.cog._subscription_loop()
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [15, 30, 120, 15])
        self.assertEqual([call.kwargs["attempt"] for call in self.cog._subscribe_once.call_args_list],
                         [1, 2, 3, 1])

    def test_retry_delay_is_bounded(self):
        self.assertEqual([_subscription_retry_delay(n) for n in range(1, 8)],
                         [15, 30, 60, 120, 240, 300, 300])
