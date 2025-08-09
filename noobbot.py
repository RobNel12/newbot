# combined_bot.py
# Python 3.10+ | discord.py 2.3+
# Combines:
#  1) Ticket Bot (multi-panel, claim, transcript)
#  2) Coach Roster Bot (with admin approval flow)
#
# NOTE: Enable "Message Content Intent" in the Discord Developer Portal for transcripts and reading coach submissions.

import os
import io
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

CONFIG_PATH = "combined_config.json"

# ---------- Persistent Config Helpers ----------
def load_all() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_all(data: Dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def ensure_guild(data: Dict[str, Any], gid: int) -> Dict[str, Any]:
    g = data.setdefault(str(gid), {})
    # Ticket system
    g.setdefault("tickets", {"category_id": None, "log_channel_id": None, "panels": {}, "next_ticket_id": 1})
    # Coach system
    g.setdefault("coach", {
        "category_id": None,
        "roster_channel_id": None,
        "log_channel_id": None,
        "next_entry_id": 1,
        "template_text": None
    })
    return g
# ---------- Bot Class ----------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True  # Needed for transcripts & reading coach submissions

class CombinedBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.store = load_all()

    async def setup_hook(self):
        # Views for Ticket Panels and Coach Panels will be added later
        self.add_view(TicketPanelView(self))
        self.add_view(CoachControlsView(self))
        self.add_view(OpenCoachTicketView(self))

        gid = os.getenv("GUILD_ID")
        if gid:
            guild = discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    def gcfg(self, gid: int) -> Dict[str, Any]:
        return ensure_guild(self.store, gid)

    def save(self):
        save_all(self.store)

bot = CombinedBot()
# ---------- Ticket Panel System ----------
class TicketPanelView(discord.ui.View):
    def __init__(self, bot: CombinedBot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="🎫 Open Ticket", style=discord.ButtonStyle.primary, custom_id="ticket_open_btn")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.bot.gcfg(interaction.guild_id)
        tickets_cfg = g["tickets"]
        category_id = tickets_cfg.get("category_id")
        if not category_id:
            return await interaction.response.send_message("No ticket category set.", ephemeral=True)

        category = interaction.guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("Configured category not found.", ephemeral=True)

        ticket_id = tickets_cfg["next_ticket_id"]
        tickets_cfg["next_ticket_id"] += 1
        self.bot.save()

        name = f"{ticket_id:03d}-{interaction.user.name.lower()}"
        topic = f"ticket|opener={interaction.user.id}|claimed=none"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
        }

        ch = await interaction.guild.create_text_channel(name=name, category=category, overwrites=overwrites, topic=topic)
        await ch.send(f"Ticket opened by {interaction.user.mention}", view=TicketControlsView(self.bot))
        await interaction.response.send_message(f"Ticket created: {ch.mention}", ephemeral=True)

class TicketControlsView(discord.ui.View):
    def __init__(self, bot: CombinedBot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="📌 Claim", style=discord.ButtonStyle.success, custom_id="ticket_claim_btn")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_channels:
            return await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)

        topic = interaction.channel.topic or ""
        parts = topic.split("|")
        meta = dict(p.split("=") for p in parts if "=" in p)
        meta["claimed"] = str(interaction.user.id)
        new_topic = "|".join([f"{k}={v}" for k, v in meta.items()])
        await interaction.channel.edit(name=f"{interaction.channel.name}-{interaction.user.name.lower()}", topic=new_topic)
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}")

    @discord.ui.button(label="🧾 Close & Transcript", style=discord.ButtonStyle.danger, custom_id="ticket_close_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_channels:
            return await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)

        transcript_lines = []
        async for m in interaction.channel.history(limit=None, oldest_first=True):
            ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
            transcript_lines.append(f"[{ts}] {m.author}: {m.content}")

        transcript_data = "\n".join(transcript_lines).encode()
        file = discord.File(io.BytesIO(transcript_data), filename="transcript.txt")

        g = self.bot.gcfg(interaction.guild_id)
        log_id = g["tickets"].get("log_channel_id")
        log_ch = interaction.guild.get_channel(log_id) if log_id else None
        if log_ch:
            await log_ch.send(f"Ticket {interaction.channel.name} closed by {interaction.user}", file=file)
        else:
            await interaction.channel.send(file=file)

        await interaction.channel.delete()
# ---------- Coach Roster System ----------
DEFAULT_TEMPLATE = (
"""
```
IGN (In-Game Name):
[Your in-game name here]
    
Region / Server Preference:
[e.g., NA East, EU Central, etc.]
    
Experience & Background:
(Hours played, notable ranks, tournament experience, past teams, etc.)
    
Specialties
    [ ] 1v1 Duels
    
    [ ] Team Fights
    
    [ ] Frontline / Objective Play
    
    [ ] Loadout Optimization
    
    [ ] Movement & Positioning
    
    [ ] Reading Opponents / Feints
    
    [ ] Other: __________
    
    
Coaching Style:
    [ ] Live matches with real-time commentary
    
    [ ] Private duels and drills
    
    [ ] Recorded match review with feedback
    
    [ ] Written guides/tips
    
    [ ] Other: __________
    
    
Session Length & Structure:
(Example: “Typically 1-hour sessions with warmup, targeted drills, and live practice.”)
    
Availability (with Time Zone):
[e.g., Weeknights 6–9 PM EST, Weekends flexible]
    
    Voice Chat Options:
    
Past Student Feedback:
(Optional — quotes, success stories, or notable improvements)
    
Other Notes:
(Anything else players should know—ping requirements, competitive focus, preferred communication style, etc.)
```
"""
)

class OpenCoachTicketView(discord.ui.View):
    def __init__(self, bot: CombinedBot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="🎫 Open Application", style=discord.ButtonStyle.primary, custom_id="coach_open_btn")
    async def open_application(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.bot.gcfg(interaction.guild_id)["coach"]
        cat_id = g.get("category_id")
        if not cat_id:
            return await interaction.response.send_message("No coach application category set.", ephemeral=True)
        category = interaction.guild.get_channel(cat_id)
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("Category not found.", ephemeral=True)

        name = f"coach-{interaction.user.name.lower()}"
        topic = f"coach|opener={interaction.user.id}|submitted=none|approved=none"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
        }
        ch = await interaction.guild.create_text_channel(name=name, category=category, overwrites=overwrites, topic=topic)
        await ch.send(g.get("template_text") or DEFAULT_TEMPLATE, view=CoachControlsView(self.bot))
        await interaction.response.send_message(f"Application ticket created: {ch.mention}", ephemeral=True)

class CoachControlsView(discord.ui.View):
    def __init__(self, bot: CombinedBot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="📥 Submit", style=discord.ButtonStyle.success, custom_id="coach_submit_btn")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        topic = interaction.channel.topic or ""
        if f"opener={interaction.user.id}" not in topic:
            return await interaction.response.send_message("Only the opener can submit.", ephemeral=True)

        async for m in interaction.channel.history(limit=50, oldest_first=False):
            if m.author == interaction.user and not m.author.bot and m.content:
                g = self.bot.gcfg(interaction.guild_id)["coach"]
                new_topic = topic.replace("submitted=none", f"submitted={m.id}")
                await interaction.channel.edit(topic=new_topic)
                return await interaction.response.send_message("✅ Submitted for review.", ephemeral=True)
        await interaction.response.send_message("Couldn't find your template message.", ephemeral=True)

    @discord.ui.button(label="✅ Approve (Admin)", style=discord.ButtonStyle.primary, custom_id="coach_approve_btn")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_channels:
            return await interaction.response.send_message("Admins only.", ephemeral=True)

        topic = interaction.channel.topic or ""
        parts = dict(p.split("=") for p in topic.split("|") if "=" in p)
        msg_id = parts.get("submitted")
        if not msg_id or msg_id == "none":
            return await interaction.response.send_message("No submission to approve.", ephemeral=True)

        try:
            msg = await interaction.channel.fetch_message(int(msg_id))
        except:
            return await interaction.response.send_message("Submission message not found.", ephemeral=True)

        g = self.bot.gcfg(interaction.guild_id)["coach"]
        roster_ch = interaction.guild.get_channel(g.get("roster_channel_id"))
        if not roster_ch:
            return await interaction.response.send_message("Roster channel not set.", ephemeral=True)

        entry_id = g["next_entry_id"]
        g["next_entry_id"] += 1
        self.bot.save()

        embed = discord.Embed(title=f"Coach #{entry_id:03d}", description=msg.content, color=discord.Color.green())
        embed.add_field(name="User", value=msg.author.mention)
        await roster_ch.send(embed=embed)

        new_topic = topic.replace("approved=none", f"approved={entry_id}")
        await interaction.channel.edit(topic=new_topic)
        await interaction.response.send_message(f"Approved and added to roster as #{entry_id:03d}", ephemeral=True)

    @discord.ui.button(label="🧾 Close & Log (Admin)", style=discord.ButtonStyle.danger, custom_id="coach_close_btn")
    async def close_and_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_channels:
            return await interaction.response.send_message("Admins only.", ephemeral=True)

        transcript_lines = []
        async for m in interaction.channel.history(limit=None, oldest_first=True):
            ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
            transcript_lines.append(f"[{ts}] {m.author}: {m.content}")

        transcript_data = "\n".join(transcript_lines).encode()
        file = discord.File(io.BytesIO(transcript_data), filename="coach_transcript.txt")

        g = self.bot.gcfg(interaction.guild_id)["coach"]
        log_ch = interaction.guild.get_channel(g.get("log_channel_id"))
        if log_ch:
            await log_ch.send(f"Coach ticket {interaction.channel.name} closed by {interaction.user}", file=file)
        else:
            await interaction.channel.send(file=file)

        await interaction.channel.delete()
# ---------- Slash Commands ----------
@app_commands.command(name="setup", description="Set ticket category and log channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_cmd(interaction: discord.Interaction, category: discord.CategoryChannel, log_channel: discord.TextChannel):
    g = bot.gcfg(interaction.guild_id)["tickets"]
    g["category_id"] = category.id
    g["log_channel_id"] = log_channel.id
    bot.save()
    await interaction.response.send_message("✅ Ticket system configured.", ephemeral=True)

@app_commands.command(name="panel", description="Post a ticket panel in the current channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def panel_cmd(interaction: discord.Interaction):
    await interaction.channel.send("Click below to open a ticket.", view=TicketPanelView(bot))
    await interaction.response.send_message("Panel posted.", ephemeral=True)

@app_commands.command(name="coach_setup", description="Set coach application category, roster channel, and log channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def coach_setup_cmd(interaction: discord.Interaction, category: discord.CategoryChannel, roster_channel: discord.TextChannel, log_channel: discord.TextChannel):
    g = bot.gcfg(interaction.guild_id)["coach"]
    g["category_id"] = category.id
    g["roster_channel_id"] = roster_channel.id
    g["log_channel_id"] = log_channel.id
    bot.save()
    await interaction.response.send_message("✅ Coach system configured.", ephemeral=True)

@app_commands.command(name="coach_panel", description="Post a coach application panel")
@app_commands.checks.has_permissions(manage_guild=True)
async def coach_panel_cmd(interaction: discord.Interaction):
    await interaction.channel.send("Click below to open a coach application ticket.", view=OpenCoachTicketView(bot))
    await interaction.response.send_message("Coach panel posted.", ephemeral=True)

bot.tree.add_command(setup_cmd)
bot.tree.add_command(panel_cmd)
bot.tree.add_command(coach_setup_cmd)
bot.tree.add_command(coach_panel_cmd)
# ---------- Run Bot ----------
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
    bot.run(token)

if __name__ == "__main__":
    main()
