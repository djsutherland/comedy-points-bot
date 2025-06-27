import logging
from discord.ext import commands


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
            logging.error(f"Exception in loading {module}\n{info}")
            await ctx.message.add_reaction("\N{PISTOL}")
            await ctx.send(f"```{info}```")
        else:
            logging.info(f"Loaded {module}")
            await ctx.message.add_reaction("\N{WHITE HEAVY CHECK MARK}")

    @commands.command(hidden=True)
    @commands.is_owner()
    async def unload(self, ctx, *, module: str):
        """Unloads a module."""
        try:
            await self.bot.unload_extension(module)
        except Exception as e:
            info = f"{type(e).__name__}: {e}"
            logging.error(f"Exception in unloading {module}\n{info}")
            await ctx.message.add_reaction("\N{PISTOL}")
            await ctx.send(f"```{info}```")
        else:
            logging.info(f"Unloaded {module}")
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
            logging.error(f"Exception in reloading {module}\n{info}")
            await ctx.message.add_reaction("\N{PISTOL}")
            await ctx.send(f"```{info}```")
        else:
            logging.info(f"Reloaded {module}")
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
        await message.delete()

async def setup(bot):
    await bot.add_cog(Basics(bot))
