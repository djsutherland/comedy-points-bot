"""Console and DM logging policy, including reader compatibility."""

import logging
import re

import discord

from .log_redaction import install_log_redaction


READER_LOGGERS = ("reader", "reader.parser.feedparser")
MISSING_CONTENT_TYPE = re.compile(
    r'exception="NonXMLContentType: no Content-type specified"'
    r"|NonXMLContentType\(['\"]no Content-type specified['\"]\)"
)


class ReaderFeedWarnings(logging.Filter):
    def filter(self, record):
        if record.name not in READER_LOGGERS or record.levelno != logging.WARNING:
            return True
        message = record.getMessage()
        if "bozo feed" not in message:
            return True

        if MISSING_CONTENT_TYPE.search(message):
            message = message.replace(
                "bozo feed",
                "RSS feed has no Content-Type header; parser continuing",
                1,
            )
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
        else:
            message = message.replace("bozo feed", "RSS feed metadata warning", 1)
        record.msg = message
        record.args = ()
        # Logger levels are checked before filters. Recheck after demotion so
        # a NOTSET handler cannot leak a DEBUG record into normal output.
        return record.levelno >= logging.getLogger(record.name).getEffectiveLevel()


def _is_gateway_diagnostic_record(record):
    message = record.getMessage()
    if record.name == "discord.gateway":
        return "Can't keep up" in message or "heartbeat blocked" in message
    if record.name == "comedypoints.monitoring":
        return message.startswith((
            "High gateway latency detected:", "Event loop lag detected:",
        ))
    return False


def _allow_dm_log_record(record):
    return not _is_gateway_diagnostic_record(record)


def configure_logging(console_level="INFO", *, dm_handler=None):
    console = logging.StreamHandler()
    console.setLevel(console_level)
    discord.utils.setup_logging(handler=console, level=console.level)
    root = logging.getLogger()
    if dm_handler is not None:
        dm_handler.setFormatter(logging.Formatter(
            "[{asctime}] [{levelname}] {name}: {message}",
            "%Y-%m-%d %H:%M:%S", style="{",
        ))
        dm_handler.addFilter(_allow_dm_log_record)
        root.addHandler(dm_handler)
    # The console and DM thresholds are independent.
    root.setLevel(min(handler.level for handler in root.handlers))

    # Apply at the emitting loggers, before handler thresholds are checked.
    # Filters on the parent `reader` logger do not see child logger records.
    for name in READER_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, ReaderFeedWarnings) for f in logger.filters):
            logger.addFilter(ReaderFeedWarnings())
    install_log_redaction()
