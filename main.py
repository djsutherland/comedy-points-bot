#!/usr/bin/env python

import logging
import os

import discord
from discord_lumberjack.handlers import DiscordDMHandler
from dotenv import load_dotenv

from comedypoints import ComedyPointsBot


def main():
    load_dotenv()
    discord.utils.setup_logging(level=os.environ.get("LOG_LEVEL", "INFO"))

    if "DM_LOG_TARGET" in os.environ:
        logger = logging.getLogger()
        handler = DiscordDMHandler(
            bot_token=os.environ["DISCORD_TOKEN"],
            user_id=os.environ["DM_LOG_TARGET"],
            level=os.environ.get("DM_LOG_LEVEL", "WARNING"),
        )
        logger.addHandler(handler)

    bot = ComedyPointsBot()
    bot.run(os.environ["DISCORD_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
