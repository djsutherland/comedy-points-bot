#!/usr/bin/env python

import os

import discord
from dotenv import load_dotenv

from comedypoints import ComedyPointsBot


def main():
    load_dotenv()
    discord.utils.setup_logging(level=os.environ.get("LOG_LEVEL", "INFO"))

    bot = ComedyPointsBot()
    bot.run(os.environ["DISCORD_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
