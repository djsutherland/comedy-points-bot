#!/usr/bin/env -S uv run --locked

import os

import discord
from discord_lumberjack.handlers import DiscordDMHandler
from dotenv import load_dotenv

from comedypoints import ComedyPointsBot
from comedypoints.logging_config import configure_logging


def main():
    load_dotenv()
    discord.VoiceClient.warn_nacl = False
    discord.VoiceClient.warn_dave = False

    dm_handler = None
    if "DM_LOG_TARGET" in os.environ:
        dm_handler = DiscordDMHandler(
            bot_token=os.environ["DISCORD_TOKEN"],
            user_id=os.environ["DM_LOG_TARGET"],
            level=os.environ.get("DM_LOG_LEVEL", "WARNING"),
        )
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"), dm_handler=dm_handler)
    bot = ComedyPointsBot()
    bot.run(os.environ["DISCORD_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
