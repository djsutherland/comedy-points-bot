import asyncio
import datetime
from email.utils import format_datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web

from comedypoints.youtube_websub import (
    YouTubeWebSub, YOUTUBE_HTTP_TIMEOUT, YOUTUBE_TOPIC_URL,
    _log_callback_requests, _parse_retry_after, _subscription_retry_delay,
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

    def response(self, status=202, read=None, enter=None, headers=None, body=b""):
        response = SimpleNamespace(
            status=status, headers=headers or {},
            read=read or AsyncMock(return_value=body),
        )
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

    async def test_rejected_response_logs_headers_body_and_retry_after(self):
        self.response(
            status=503,
            headers={"Retry-After": "120", "Content-Type": "text/plain; charset=utf-8"},
            body=b"Transient error; please try again later",
        )
        with self.assertLogs("comedypoints.youtube_websub", level="INFO") as logs:
            result = await asyncio.wait_for(self.cog._subscribe_once(attempt=1), timeout=1)
        self.assertIsNone(result)
        self.assertEqual(self.cog._hub_retry_after, 120)
        start = next(line for line in logs.output if "request starting" in line)
        self.assertIn("hub=https://pubsubhubbub.appspot.com/subscribe", start)
        self.assertIn("topic=" + YOUTUBE_TOPIC_URL, start)
        headers = next(line for line in logs.output if "response headers" in line)
        self.assertIn("retry_after=120", headers)
        self.assertIn("'Retry-After': '120'", headers)
        rejected = next(line for line in logs.output if "request rejected" in line)
        self.assertTrue(rejected.startswith("WARNING:"))
        self.assertIn("status=503 retry_after=120", rejected)
        self.assertIn("body='Transient error; please try again later'", rejected)

    async def test_retry_schedule_honors_hub_retry_after(self):
        async def fail(*, attempt):
            self.cog._hub_retry_after = 120 if attempt == 1 else None
            return None
        self.cog._subscribe_once = fail
        with patch("comedypoints.youtube_websub.asyncio.sleep", new_callable=AsyncMock) as sleep:
            sleep.side_effect = [None, asyncio.CancelledError()]
            with self.assertLogs("comedypoints.youtube_websub", level="WARNING") as logs:
                with self.assertRaises(asyncio.CancelledError):
                    await self.cog._subscription_loop()
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [120, 30])
        self.assertIn("retry_seconds=120 hub_retry_after=120", logs.output[0])

    def test_retry_delay_honors_hub_retry_after_within_bounds(self):
        self.assertEqual(_subscription_retry_delay(1, 120), 120)
        self.assertEqual(_subscription_retry_delay(1, 5), 15)
        self.assertEqual(_subscription_retry_delay(4, 60), 120)
        self.assertEqual(_subscription_retry_delay(1, 100_000), 900)
        self.assertEqual(_subscription_retry_delay(6, None), 300)

    def test_parse_retry_after_handles_seconds_dates_and_garbage(self):
        self.assertEqual(_parse_retry_after("120"), 120)
        self.assertEqual(_parse_retry_after(" 7 "), 7)
        self.assertIsNone(_parse_retry_after(None))
        self.assertIsNone(_parse_retry_after(""))
        self.assertIsNone(_parse_retry_after("soon"))
        self.assertIsNone(_parse_retry_after("-5"))
        self.assertEqual(_parse_retry_after("Mon, 01 Jan 2001 00:00:00 GMT"), 0)
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=90)
        seconds = _parse_retry_after(format_datetime(future, usegmt=True))
        self.assertTrue(85 <= seconds <= 91, seconds)

    def test_retry_delay_is_bounded(self):
        self.assertEqual([_subscription_retry_delay(n) for n in range(1, 8)],
                         [15, 30, 60, 120, 240, 300, 300])


class CallbackRequestLoggingTests(unittest.IsolatedAsyncioTestCase):
    def request(self, method="GET", path="/callback", query="hub.mode=subscribe&hub.challenge=abc"):
        return SimpleNamespace(
            method=method, path=path, query_string=query, remote="127.0.0.1",
            headers={"X-Forwarded-For": "66.249.66.1", "User-Agent": "FeedFetcher-Google"},
        )

    async def test_logs_matched_requests_with_status(self):
        async def handler(request):
            return web.Response(status=204)
        with self.assertLogs("comedypoints.youtube_websub", level="INFO") as logs:
            response = await _log_callback_requests(self.request(), handler)
        self.assertEqual(response.status, 204)
        line = logs.output[0]
        self.assertIn("method=GET path=/callback query='hub.mode=subscribe&hub.challenge=abc'", line)
        self.assertIn("forwarded_for=66.249.66.1 user_agent='FeedFetcher-Google' status=204", line)

    async def test_logs_unmatched_paths_before_reraising(self):
        async def handler(request):
            raise web.HTTPNotFound()
        with self.assertLogs("comedypoints.youtube_websub", level="INFO") as logs:
            with self.assertRaises(web.HTTPNotFound):
                await _log_callback_requests(self.request(path="/other", query=""), handler)
        self.assertIn("path=/other query='' ", logs.output[0])
        self.assertIn("status=404", logs.output[0])
