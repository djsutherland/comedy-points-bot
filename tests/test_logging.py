from contextlib import redirect_stderr
import io
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from reader import ParseError
from reader._parser.feedparser import FeedparserParser

from comedypoints.ep_poster import EpPoster
from comedypoints.logging_config import configure_logging


PRIVATE_URL = "https://www.patreon.com/rss/test?auth=private-test-token"
WARNING_FORMATS = (
    ("reader", "bozo feed: %r: %s", (PRIVATE_URL,
     "NonXMLContentType('no Content-type specified')")),
    ("reader.parser.feedparser", 'bozo feed  feed="%s" exception="%s"',
     (PRIVATE_URL, "NonXMLContentType: no Content-type specified")),
)


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.console = io.StringIO()
        self.dm = io.StringIO()
        self.states = []
        for name in ("", "reader", "reader.parser.feedparser",
                     "comedypoints.monitoring", "discord.gateway"):
            logger = logging.getLogger(name)
            self.states.append((logger, logger.level, logger.handlers[:],
                                logger.filters[:], logger.propagate, logger.disabled))
            logger.handlers = []
            logger.filters = []
            logger.setLevel(logging.NOTSET)
            logger.propagate = True
            logger.disabled = False

    def tearDown(self):
        for logger, level, handlers, filters, propagate, disabled in self.states:
            for handler in logger.handlers:
                handler.close()
            logger.handlers = handlers
            logger.filters = filters
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled

    def configure(self, console_level="INFO", dm_level="WARNING"):
        handler = logging.StreamHandler(self.dm)
        handler.setLevel(dm_level)
        with redirect_stderr(self.console):
            configure_logging(console_level, dm_handler=handler)

    def emit_missing_header_warnings(self):
        for name, message, args in WARNING_FORMATS:
            logging.getLogger(name).warning(message, *args)

    def test_missing_header_is_quiet_at_default_levels_for_both_reader_formats(self):
        self.configure()
        self.emit_missing_header_warnings()
        self.emit_missing_header_warnings()
        self.assertEqual(self.console.getvalue(), "")
        self.assertEqual(self.dm.getvalue(), "")

    def test_debug_preserves_explanation_and_redaction_without_warning_dms(self):
        self.configure("DEBUG")
        self.emit_missing_header_warnings()
        output = self.console.getvalue()
        self.assertEqual(output.count("RSS feed has no Content-Type header"), 2)
        self.assertEqual(output.count("[private RSS feed]"), 2)
        self.assertIn("DEBUG", output)
        self.assertNotIn("bozo", output)
        self.assertNotIn("private-test-token", output)
        self.assertEqual(self.dm.getvalue(), "")

    def test_dm_level_is_independent_of_console_level(self):
        self.configure("ERROR", "WARNING")
        logging.getLogger("reader").warning("Feed retrieval failed")
        self.assertEqual(self.console.getvalue(), "")
        self.assertIn("[WARNING] reader: Feed retrieval failed", self.dm.getvalue())

    def test_debug_dms_honor_reclassified_level(self):
        self.configure("INFO", "DEBUG")
        self.emit_missing_header_warnings()
        self.assertEqual(self.console.getvalue(), "")
        self.assertIn("[DEBUG] reader.parser.feedparser:", self.dm.getvalue())
        self.assertNotIn("private-test-token", self.dm.getvalue())

    def test_other_feed_warnings_and_actual_errors_still_alert(self):
        self.configure()
        logger = logging.getLogger("reader.parser.feedparser")
        for detail in ("NonXMLContentType: text/html is not an XML media type",
                       "CharacterEncodingOverride: document declared as ascii"):
            logger.warning('bozo feed  feed="%s" exception="%s"', PRIVATE_URL, detail)
        logger.error('bozo feed  exception="NonXMLContentType: no Content-type specified"')
        logger.warning("An unrelated warning")
        output = self.dm.getvalue()
        self.assertEqual(output.count("[WARNING]"), 3)
        self.assertEqual(output.count("RSS feed metadata warning"), 2)
        self.assertIn("[ERROR]", output)
        self.assertIn("An unrelated warning", output)
        self.assertNotIn("private-test-token", output)

    def test_gateway_diagnostics_stay_in_console_and_other_warnings_reach_dm(self):
        self.configure()
        logging.getLogger("discord.gateway").warning("Shard heartbeat blocked for 10s")
        logger = logging.getLogger("comedypoints.monitoring")
        logger.warning("Event loop lag detected: 1.5s late")
        logger.warning("A different monitoring failure")
        self.assertIn("heartbeat blocked", self.console.getvalue())
        self.assertIn("Event loop lag", self.console.getvalue())
        self.assertNotIn("heartbeat blocked", self.dm.getvalue())
        self.assertNotIn("Event loop lag", self.dm.getvalue())
        self.assertIn("A different monitoring failure", self.dm.getvalue())

    def test_tracebacks_are_useful_and_private_in_both_outputs(self):
        self.configure()
        try:
            raise ValueError(PRIVATE_URL)
        except ValueError:
            logging.getLogger("reader").exception("Fetch failed for %s", PRIVATE_URL)
        for output in (self.console.getvalue(), self.dm.getvalue()):
            self.assertIn("Traceback", output)
            self.assertIn("ValueError", output)
            self.assertIn("[private RSS feed]", output)
            self.assertNotIn("private-test-token", output)

    def test_installed_reader_parses_missing_header_feed_and_rejects_bad_xml(self):
        self.configure("DEBUG")
        parser = FeedparserParser()
        xml = b'''<rss version="2.0"><channel><title>Test feed</title>
        <item><guid>entry-1</guid><title>Test entry</title></item></channel></rss>'''
        feed, entries = parser(PRIVATE_URL, io.BytesIO(xml), {"server": "test"})
        self.assertEqual(feed.title, "Test feed")
        self.assertEqual([entry.id for entry in entries], ["entry-1"])
        self.assertIn("RSS feed has no Content-Type header", self.console.getvalue())
        self.assertEqual(self.dm.getvalue(), "")
        with self.assertRaises(ParseError):
            parser(PRIVATE_URL, io.BytesIO(b"<rss><channel>"), {"server": "test"})


class RssPollLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.poster = EpPoster(SimpleNamespace())
        self.poster._feed_diagnostics = AsyncMock(return_value=[])
        self.poster.post_entry = AsyncMock(return_value=False)

    async def asyncTearDown(self):
        self.poster._reader_executor.shutdown(wait=True)

    async def poll(self, updates, entries=(), trigger="fallback", level="INFO"):
        self.poster._run_reader = AsyncMock(side_effect=[updates, list(entries)])
        with self.assertLogs("comedypoints.ep_poster", level=level) as logs:
            await self.poster._check_feeds(trigger=trigger)
        return logs.output

    async def test_idle_fallback_has_one_summary_and_skips_diagnostic_queries(self):
        output = await self.poll([(PRIVATE_URL, None)])
        self.assertEqual(len(output), 1, output)
        self.assertIn("status=ok new=0 modified=0 errors=0", output[0])
        self.poster._feed_diagnostics.assert_not_awaited()

    async def test_rapid_idle_poll_is_debug_and_keeps_detailed_diagnostics(self):
        output = await self.poll([(PRIVATE_URL, None)], trigger="expected:test:attempt-1",
                                 level="DEBUG")
        self.assertTrue(all(line.startswith("DEBUG:") for line in output), output)
        self.assertEqual(self.poster._feed_diagnostics.await_count, 2)

    async def test_summary_counts_actual_posts_not_deduplicated_attempts(self):
        entry = SimpleNamespace(id="entry-1", title="Test entry", published=None)
        result = SimpleNamespace(new=1, modified=2, unmodified=3)
        output = await self.poll([(PRIVATE_URL, result)], [entry])
        self.assertIn("new=1 modified=2 errors=0", output[-1])
        self.assertIn("unread=1 post_attempts=1 posted=0", output[-1])
        self.poster.post_entry.assert_awaited_once()

    async def test_real_update_failures_still_log_errors_and_failed_summary(self):
        output = await self.poll([(PRIVATE_URL, ParseError(PRIVATE_URL))])
        self.assertTrue(output[0].startswith("ERROR:"), output)
        self.assertIn("status=error", output[-1])
        self.assertIn("errors=1", output[-1])

    async def test_reader_operation_failure_is_not_summarized_as_success(self):
        output = await self.poll(None)
        self.assertIn("status=error", output[-1])


if __name__ == "__main__":
    unittest.main()
