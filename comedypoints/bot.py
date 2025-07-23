import asyncio
import logging

import discord
from discord.ext import commands


class ComedyPointsBot(commands.Bot):

    def __init__(self, **kwargs):
        intents = discord.Intents.default()
        intents.reactions = True

        super().__init__(
            intents=intents,
            command_prefix=commands.when_mentioned,
            strip_after_prefix=True,
            **kwargs,
        )
        self.initial_extensions = [
            "comedypoints.basics",
            "comedypoints.fix_reacts",
            "comedypoints.points",
            "comedypoints.privateperms",
        ]

    async def setup_hook(self):
        async with asyncio.TaskGroup() as tg:
            for ext in self.initial_extensions:
                tg.create_task(self.load_extension(ext))

    async def on_ready(self):
        logging.info(f"Logged in as {self.user} ({self.user.id})")
