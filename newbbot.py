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
        await self.load_extension("cogs.ticketing")
        await self.load_extension("cogs.moderation")

        dev_guild = discord.Object(id=1304124705896136744)  # your server

        # Copy all global app commands (from your cogs) into the guild,
        # then sync that guild for instant availability.
        self.tree.copy_global_to(guild=dev_guild)
        await self.tree.sync(guild=dev_guild)

        # (Optional) Later, when you're ready to roll out globally, run:
        # await self.tree.sync()

        logging.info("App commands synced to dev guild.")


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
