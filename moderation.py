# moderation.py
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- TIMEOUT ---
    @app_commands.command(name="newb_timeout", description="Timeout a member for a certain duration in minutes.")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def newb_timeout(self, interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason provided"):
        await member.timeout(discord.utils.utcnow() + discord.utils.timedelta(minutes=minutes), reason=reason)
        await interaction.response.send_message(f"⏳ {member.mention} has been timed out for {minutes} minutes. Reason: {reason}")

    # --- MUTE ---
    @app_commands.command(name="newb_mute", description="Mute a member for a certain duration in minutes.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def newb_mute(self, interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason provided"):
        muted_role = discord.utils.get(interaction.guild.roles, name="Muted")
        if not muted_role:
            muted_role = await interaction.guild.create_role(name="Muted", reason="Mute role for muting members")
            for channel in interaction.guild.channels:
                await channel.set_permissions(muted_role, send_messages=False, speak=False)

        await member.add_roles(muted_role, reason=reason)
        await interaction.response.send_message(f"🔇 {member.mention} has been muted for {minutes} minutes. Reason: {reason}")

        await asyncio.sleep(minutes * 60)
        await member.remove_roles(muted_role, reason="Mute duration expired")

    # --- KICK ---
    @app_commands.command(name="newb_kick", description="Kick a member from the server.")
    @app_commands.checks.has_permissions(kick_members=True)
    async def newb_kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 {member.mention} has been kicked. Reason: {reason}")

    # --- BAN ---
    @app_commands.command(name="newb_ban", description="Ban a member for a certain duration in minutes (0 for permanent).")
    @app_commands.checks.has_permissions(ban_members=True)
    async def newb_ban(self, interaction: discord.Interaction, member: discord.Member, minutes: int = 0, reason: str = "No reason provided"):
        await interaction.guild.ban(member, reason=reason)
        await interaction.response.send_message(f"⛔ {member.mention} has been banned. Reason: {reason}")

        if minutes > 0:
            await asyncio.sleep(minutes * 60)
            await interaction.guild.unban(member, reason="Temporary ban expired")


async def setup(bot):
    await bot.add_cog(Moderation(bot))