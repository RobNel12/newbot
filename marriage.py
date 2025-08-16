import discord
from discord.ext import commands
from discord import app_commands
from io import BytesIO

class ProposeView(discord.ui.View):
    def __init__(self, proposer: discord.Member, proposee: discord.Member):
        super().__init__(timeout=60)  # 1 minute timeout
        self.proposer = proposer
        self.proposee = proposee
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.proposee.id:
            await interaction.response.send_message("You're not the one being proposed to!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.edit_message(content=f"💍 {self.proposee.mention} accepted {self.proposer.mention}'s proposal! 💍", view=None)
        await self.send_married_graphic(interaction.channel)
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.edit_message(content=f"💔 {self.proposee.mention} declined {self.proposer.mention}'s proposal.", view=None)
        self.stop()

    async def send_married_graphic(self, channel: discord.TextChannel):
        # For now we just send an image from a file; replace with your own graphic
        with open("married.png", "rb") as f:
            file = discord.File(f, filename="married.png")
            await channel.send(content=f"🎉 Congratulations {self.proposer.mention} and {self.proposee.mention}! 🎉", file=file)


class Marriage(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="propose", description="Propose to another user")
    async def propose(self, interaction: discord.Interaction, user: discord.Member):
        if user.id == interaction.user.id:
            await interaction.response.send_message("You can't propose to yourself!", ephemeral=True)
            return

        view = ProposeView(proposer=interaction.user, proposee=user)
        await interaction.response.send_message(
            f"{user.mention}, {interaction.user.mention} is proposing to you! Do you accept?",
            view=view
        )

async def setup(bot):
    await bot.add_cog(Marriage(bot))