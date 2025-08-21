import os
import logging
import asyncio
from dotenv import load_dotenv
import discord
from discord.ext import commands

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

# ---------- Intents ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

# ---------- Bot ----------
class ModBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
        )

    async def setup_hook(self):
        # Load cogs
        await self.load_extension("cogs.ticketing")
        await self.load_extension("cogs.moderation")

        # Do a global sync at startup (takes up to an hour to propagate)
        await self.tree.sync()
        logging.info("App commands synced globally.")

# Owner-only command to instantly sync with a specific guild
@commands.is_owner()
@bot.command(name="syncguild")
async def sync_guild(ctx, guild_id: int):
    guild = discord.Object(id=guild_id)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    await ctx.send(f"✅ Synced {len(synced)} commands to guild `{guild_id}`")

async def main():
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Missing DISCORD_TOKEN in .env")

    bot = ModBot()

    @bot.event
    async def on_ready():
        logging.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
        logging.info(f"Guilds: {[g.name for g in bot.guilds]}")

    await bot.start(token)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
