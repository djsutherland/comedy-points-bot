from logging import getLogger
import re

from discord.ext import commands

logger = getLogger(__name__)

msg_format = re.compile(
    r"https?://discord\.com/channels/\d+/(?P<channel>\d+)/(?P<message>\d+)/?$"
)


class Basics(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(hidden=True)
    @commands.is_owner()
    async def sync(self, ctx):
        await self.bot.tree.sync()
        await ctx.send("Global command tree synced")

    @commands.command(hidden=True)
    @commands.is_owner()
    async def localsync(self, ctx):
        self.bot.tree.copy_global_to(guild=ctx.guild)
        await self.bot.tree.sync(guild=ctx.guild)
        await ctx.send("Command tree synced locally")

    # https://github.com/Rapptz/RoboDanny/blob/master/cogs/admin.py
    @commands.command(hidden=True)
    @commands.is_owner()
    async def load(self, ctx, *, module: str):
        """Loads a module."""
        try:
            await self.bot.load_extension(module)
        except Exception as e:
            info = f"{type(e).__name__}: {e}"
            logger.error(f"Exception in loading {module}\n{info}")
            await ctx.message.add_reaction("\N{PISTOL}")
            await ctx.send(f"```{info}```")
        else:
            logger.info(f"Loaded {module}")
            await ctx.message.add_reaction("\N{WHITE HEAVY CHECK MARK}")

    @commands.command(hidden=True)
    @commands.is_owner()
    async def unload(self, ctx, *, module: str):
        """Unloads a module."""
        try:
            await self.bot.unload_extension(module)
        except Exception as e:
            info = f"{type(e).__name__}: {e}"
            logger.error(f"Exception in unloading {module}\n{info}")
            await ctx.message.add_reaction("\N{PISTOL}")
            await ctx.send(f"```{info}```")
        else:
            logger.info(f"Unloaded {module}")
            await ctx.message.add_reaction("\N{WHITE HEAVY CHECK MARK}")

    @commands.command(hidden=True)
    @commands.is_owner()
    async def reload(self, ctx, *, module: str):
        """Reloads a module."""
        try:
            try:
                await self.bot.unload_extension(module)
            except commands.ExtensionNotLoaded:
                await ctx.send(f"(FYI, `{module}` wasn't actually loaded yet)")
            await self.bot.load_extension(module)
        except Exception as e:
            info = f"{type(e).__name__}: {e}"
            logger.error(f"Exception in reloading {module}\n{info}")
            await ctx.message.add_reaction("\N{PISTOL}")
            await ctx.send(f"```{info}```")
        else:
            logger.info(f"Reloaded {module}")
            await ctx.message.add_reaction("\N{WHITE HEAVY CHECK MARK}")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if (
            payload.event_type != "REACTION_ADD"
            or payload.emoji.name != "\N{CROSS MARK}"
            or payload.message_author_id != self.bot.user.id
            or not await self.bot.is_owner(payload.member)
        ):
            return

        channel = self.bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        logger.info(f"Removing {message.jump_url} because of X by {payload.member}")
        await message.delete()

    @commands.command(hidden=True)
    @commands.is_owner()
    async def delete(self, ctx, *, post: str):
        m = msg_format.match(post)
        if not m:
            return await ctx.reply("Incorrect message link, dummy")

        channel = self.bot.get_channel(int(m.group("channel")))
        try:
            message = await channel.fetch_message(int(m.group("message")))
        except discord.NotFound:
            return await ctx.reply(
                "Couldn't find this post; this doesn't work in threads yet"
            )

        if message.author.id != self.bot.user.id:
            return await ctx.reply(f"Not deleting a post by {message.author}")

        logger.info(f"Removing {message.jump_url} because of command by {ctx.author}")
        await message.delete()
        await ctx.message.add_reaction("\N{WHITE HEAVY CHECK MARK}")


async def setup(bot):
    await bot.add_cog(Basics(bot))
