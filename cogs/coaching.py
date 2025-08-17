import discord
from discord.ext import commands
from discord import app_commands
import json
import os
from datetime import datetime

CONFIG_FILE = "tickets_config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

class TicketView(discord.ui.View):
    def __init__(self, bot, opener: discord.Member, config: dict):
        super().__init__(timeout=None)
        self.bot = bot
        self.opener = opener
        self.config = config

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.green)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        claim_role = interaction.guild.get_role(self.config["claim_role"])
        if not (claim_role in interaction.user.roles or interaction.user.guild_permissions.administrator):
            return await interaction.response.send_message("You cannot claim this ticket.", ephemeral=True)

        await interaction.channel.set_permissions(interaction.user, read_messages=True, send_messages=True)
        await interaction.response.send_message(f"{interaction.user.mention} claimed this ticket.")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.red)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        claim_role = interaction.guild.get_role(self.config["claim_role"])
        if not (
            interaction.user == self.opener
            or claim_role in interaction.user.roles
            or interaction.user.guild_permissions.administrator
        ):
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)

        log_channel = interaction.guild.get_channel(self.config["log_channel"])
        transcript_file = await self.create_transcript(interaction.channel)

        await log_channel.send(
            content=f"Ticket closed: {interaction.channel.name} (by {interaction.user.mention})",
            file=transcript_file,
        )
        await interaction.response.send_message("Ticket closed. Channel will be locked.")

        # Lock channel
        await interaction.channel.edit(overwrites={})

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        await interaction.response.send_message("Deleting ticket...")
        await interaction.channel.delete()

    async def create_transcript(self, channel: discord.TextChannel):
            messages = [msg async for msg in channel.history(limit=None, oldest_first=True)]
            html = """<html><head><meta charset="utf-8">
            <style>
            body { font-family: sans-serif; background: #36393f; color: #dcddde; }
            .msg { margin: 10px 0; display: flex; }
            .avatar { margin-right: 10px; }
            .content { background: #2f3136; padding: 5px 10px; border-radius: 5px; max-width: 80%; }
            img.attachment { max-width: 400px; margin-top: 5px; border-radius: 3px; }
            </style></head><body>
            <h2>Transcript for #{channel.name}</h2><hr>"""

        for msg in messages:
            avatar = msg.author.display_avatar.url
            name = msg.author.display_name
            tag = str(msg.author)
            time = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")

            html += f"<div class='msg'>"
            html += f"<div class='avatar'><img src='{avatar}' width='40' height='40'></div>"
            html += f"<div class='content'><b>{name}</b> <span style='color:#72767d'>({tag})</span> <i>{time}</i><br>"
            html += msg.clean_content.replace('\\n', '<br>')

            for att in msg.attachments:
                html += f"<br><a href='{att.url}'>{att.filename}</a>"
                if att.content_type and att.content_type.startswith('image/'):
                    html += f"<br><img class='attachment' src='{att.url}'>"

            for embed in msg.embeds:
                if embed.image:
                    html += f"<br><img class='attachment' src='{embed.image.url}'>"

            html += "</div></div>"

        html += "</body></html>"

    filename = f"{channel.name}_transcript.html"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    return discord.File(filename)

class TicketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = load_config()

    @commands.hybrid_group(invoke_without_command=True)
    async def tickets(self, ctx):
        await ctx.send("Use `/tickets setup` or `/tickets panel`.")

    @tickets.command()
    @commands.has_permissions(administrator=True)
    async def setup(self, ctx, category: discord.CategoryChannel, claim_role: discord.Role, log_channel: discord.TextChannel):
        self.config = {
            "guild_id": ctx.guild.id,
            "category": category.id,
            "claim_role": claim_role.id,
            "log_channel": log_channel.id,
            "counter": 0,
        }
        save_config(self.config)
        await ctx.send("Ticket system configured.")

    @tickets.command()
    @commands.has_permissions(administrator=True)
    async def panel(self, ctx):
        embed = discord.Embed(title="Coaching Tickets", description="Click below to open a ticket.", color=discord.Color.blurple())
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open Ticket", style=discord.ButtonStyle.primary, custom_id="open_ticket"))
        await ctx.send(embed=embed, view=view)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.component and interaction.data.get("custom_id") == "open_ticket":
            if not self.config:
                return await interaction.response.send_message("Ticket system not configured.", ephemeral=True)

            category = interaction.guild.get_channel(self.config["category"])
            self.config["counter"] += 1
            save_config(self.config)

            name = f"ticket-{self.config['counter']:03d}-{interaction.user.name}"
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            }
            channel = await category.create_text_channel(name, overwrites=overwrites)

            view = TicketView(self.bot, interaction.user, self.config)
            await channel.send(f"{interaction.user.mention} created a ticket.", view=view)
            await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(TicketCog(bot))


