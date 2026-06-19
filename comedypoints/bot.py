import asyncio
import logging

import discord
from discord.ext import commands


class ComedyPointsBot(commands.Bot):

    def __init__(self, **kwargs):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.guild_reactions = True
        intents.emojis_and_stickers = True
        intents.message_content = True

        super().__init__(
            intents=intents,
            command_prefix=commands.when_mentioned,
            strip_after_prefix=True,
            **kwargs,
        )
        self.initial_extensions = [
            "comedypoints.monitoring",
            "comedypoints.basics",
            "comedypoints.fix_reacts",
            "comedypoints.points",
            "comedypoints.privateperms",
            "comedypoints.ep_poster",
            "comedypoints.text_reacts",
        ]

    async def setup_hook(self):
        async with asyncio.TaskGroup() as tg:
            for ext in self.initial_extensions:
                tg.create_task(self.load_extension(ext))

    async def on_ready(self):
        logging.info(f"Logged in as {self.user} ({self.user.id})")
