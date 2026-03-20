#!/usr/bin/env python

import logging
import os

import discord
from discord_lumberjack.handlers import DiscordDMHandler
from dotenv import load_dotenv

from comedypoints import ComedyPointsBot


def _allow_dm_log_record(record: logging.LogRecord) -> bool:
    return not (
        record.name == "reader"
        and "NonXMLContentType('no Content-type specified')" in record.getMessage()
    )


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
