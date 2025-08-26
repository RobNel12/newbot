import os
import logging
import asyncio
from dotenv import load_dotenv
import discord
from discord.ext import commands

load_dotenv()

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
        # Load cogs
        await self.load_extension("cogs.ticketing")
        await self.load_extension("cogs.applications")

        # Global sync (slower rollout, ~1h but necessary for all guilds)
        await self.tree.sync()
        logging.info("App commands synced globally.")

bot = ModBot()

# ---------------- Utility Slash Command ----------------
@bot.tree.command(name="syncguild", description="Owner only: instantly sync app commands globally.")
async def syncguild(interaction: discord.Interaction):
    # Verify owner
    app_info = await bot.application_info()
    if interaction.user.id != app_info.owner.id:
        return await interaction.response.send_message("❌ You are not the bot owner.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)  # acknowledge immediately

    try:
        synced = await bot.tree.sync()  # Global sync
        await interaction.followup.send(
            f"✅ Globally synced **{len(synced)}** commands.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"⚠️ Sync failed: `{e}`", ephemeral=True)


# -------------------------------------------------------

async def main():
    async with bot:
        await bot.start(os.environ["DISCORD_TOKEN"])

if __name__ == "__main__":
    asyncio.run(main())
