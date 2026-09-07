"""Redact private feed URLs even in third-party logs and tracebacks."""

import logging
import re
import traceback


PRIVATE_FEED = re.compile(r"https?://(?:www\.)?patreon\.com/rss/[^\s<>\"']+", re.I)


class PrivateFeedRedaction(logging.Filter):
    def filter(self, record):
        record.msg = PRIVATE_FEED.sub("[private RSS feed]", record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_info = None
        if record.exc_text:
            record.exc_text = PRIVATE_FEED.sub("[private RSS feed]", record.exc_text)
        if record.stack_info:
            record.stack_info = PRIVATE_FEED.sub("[private RSS feed]", record.stack_info)
        return True


def install_log_redaction():
    # Root logger filters don't apply to propagated library records; attach to
    # the output handlers instead, including the Discord DM handler.
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, PrivateFeedRedaction) for f in handler.filters):
            handler.addFilter(PrivateFeedRedaction())
