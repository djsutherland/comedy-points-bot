#!/usr/bin/env python

import logging
import os

import discord
from discord_lumberjack.handlers import DiscordDMHandler
from dotenv import load_dotenv

from comedypoints import ComedyPointsBot


def _is_gateway_diagnostic_record(record: logging.LogRecord) -> bool:
    message = record.getMessage()
    if record.name == "discord.gateway":
        return "Can't keep up" in message or "heartbeat blocked" in message

    if record.name == "comedypoints.monitoring":
        return message.startswith(
            "High gateway latency detected:"
        ) or message.startswith("Event loop lag detected:")

    return False


def _allow_dm_log_record(record: logging.LogRecord) -> bool:
    if (
        record.name == "reader"
        and "NonXMLContentType('no Content-type specified')" in record.getMessage()
    ):
        return False

    if _is_gateway_diagnostic_record(record):
        return False

    return True


def main():
    load_dotenv()
    discord.utils.setup_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    discord.VoiceClient.warn_nacl = False
    discord.VoiceClient.warn_dave = False

    if "DM_LOG_TARGET" in os.environ:
        logger = logging.getLogger()
        handler = DiscordDMHandler(
            bot_token=os.environ["DISCORD_TOKEN"],
            user_id=os.environ["DM_LOG_TARGET"],
            level=os.environ.get("DM_LOG_LEVEL", "WARNING"),
        )
        handler.addFilter(_allow_dm_log_record)
        logger.addHandler(handler)

    bot = ComedyPointsBot()
    bot.run(os.environ["DISCORD_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
