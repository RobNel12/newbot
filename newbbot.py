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

class ModBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
        )

    async def setup_hook(self):
        # Load your cogs
        await self.load_extension("cogs.ticketing")
        await self.load_extension("cogs.moderation")

        # Global sync at startup (slower, ~1h to propagate but avoids duplicates)
        await self.tree.sync()
        logging.info("App commands synced globally.")

# Instantiate bot
bot = ModBot()

# ---------------- Utility Slash Command ----------------
@bot.tree.command(name="syncguild", description="Owner only: instantly sync app commands to a guild ID.")
async def syncguild(interaction: discord.Interaction, guild_id: str):
    # Verify owner
    app_info = await bot.application_info()
    if interaction.user.id != app_info.owner.id:
        return await interaction.response.send_message("❌ You are not the bot owner.", ephemeral=True)

    try:
        gid = int(guild_id)
        guild = discord.Object(id=gid)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        await interaction.response.send_message(
            f"✅ Synced **{len(synced)}** commands to guild `{gid}`.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"⚠️ Sync failed: `{e}`", ephemeral=True)

# -------------------------------------------------------

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
