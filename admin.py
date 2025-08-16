import discord
from discord.ext import commands
from discord import app_commands

class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="sync", description="Force sync slash commands to this server")
    @app_commands.checks.has_permissions(administrator=True)
    async def sync(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id  # You could hardcode one instead
        guild = discord.Object(id=guild_id)

        self.bot.tree.copy_global_to(guild=guild)  # Optional: copy global commands to this guild
        synced = await self.bot.tree.sync(guild=guild)

        await interaction.response.send_message(
            f"✅ Synced {len(synced)} commands to guild `{guild_id}`", ephemeral=True
        )

async def setup(bot):
    await bot.add_cog(Admin(bot))