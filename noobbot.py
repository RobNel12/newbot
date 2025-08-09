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
    # Tickets (per-panel categories)
    t = g.setdefault("tickets", {})
    t.setdefault("log_channel_id", None)          # transcript log for tickets (optional)
    t.setdefault("panels", {})                    # {panel_id: {"category_id": int, "open_text": str, "role_ids": [int,...]}}
    t.setdefault("next_panel_id", 1)
    t.setdefault("next_ticket_seq", 1)

    # Coach (unchanged)
    c = g.setdefault("coach", {})
    c.setdefault("category_id", None)
    c.setdefault("roster_channel_id", None)
    c.setdefault("log_channel_id", None)
    c.setdefault("next_entry_id", 1)
    c.setdefault("template_text", None)
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
        # Always add core views
        self.add_view(TicketControlsView(self))
        self.add_view(CoachControlsView(self))
        self.add_view(OpenCoachTicketView(self))

        # Add all saved ticket panels so their buttons keep working after restart
        for gid, gdata in self.store.items():
            panels = gdata.get("tickets", {}).get("panels", {})
            for pid in panels.keys():
                self.add_view(OpenPanelView(self, guild_id=int(gid), panel_id=int(pid)))

        # Slash sync
        gid = os.getenv("GUILD_ID")
        if gid:
            guild = discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
    # --- ADD THESE TWO METHODS ---
    def gcfg(self, gid: int) -> Dict[str, Any]:
        """Return (and initialize if needed) the per-guild config dict."""
        return ensure_guild(self.store, gid)

    def save(self) -> None:
        """Persist the in-memory store to disk."""
        save_all(self.store)
CONFIG_PATH = "combined_config.json"

def load_all() -> Dict[str, Any]:
    import json, os
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_all(data: Dict[str, Any]) -> None:
    import json
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def ensure_guild(data: Dict[str, Any], gid: int) -> Dict[str, Any]:
    g = data.setdefault(str(gid), {})
    # Tickets (per-panel)
    t = g.setdefault("tickets", {})
    t.setdefault("log_channel_id", None)
    t.setdefault("panels", {})
    t.setdefault("next_panel_id", 1)
    t.setdefault("next_ticket_seq", 1)
    # Coach
    c = g.setdefault("coach", {})
    c.setdefault("category_id", None)
    c.setdefault("roster_channel_id", None)
    c.setdefault("log_channel_id", None)
    c.setdefault("next_entry_id", 1)
    c.setdefault("template_text", None)
    return g

bot = CombinedBot()
# ---------- Ticket Panel System ----------
OPEN_PREFIX  = "ultra_ticket_open"  # custom_id = f"{OPEN_PREFIX}:{guild_id}:{panel_id}"
CLAIM_ID     = "ultra_ticket_claim_v1"
CLOSE_ID     = "ultra_ticket_close_v1"

class OpenPanelView(discord.ui.View):
    """Persistent 'Open Ticket' button for a specific panel (has its own category, text, roles)."""
    def __init__(self, bot: CombinedBot, guild_id: int, panel_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.panel_id = panel_id

        btn = discord.ui.Button(
            label="🎫 Open Ticket",
            style=discord.ButtonStyle.primary,
            custom_id=f"{OPEN_PREFIX}:{guild_id}:{panel_id}",
        )
        btn.callback = self.open_ticket
        self.add_item(btn)

    async def open_ticket(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            return await interaction.response.send_message("Use this in the right server.", ephemeral=True)

        g = self.bot.gcfg(self.guild_id)
        ts = g["tickets"]
        panel = ts["panels"].get(str(self.panel_id))
        if not panel:
            return await interaction.response.send_message("This panel is no longer configured.", ephemeral=True)

        category = interaction.guild.get_channel(panel["category_id"])
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("Panel category no longer exists. Ask an admin to reconfigure.", ephemeral=True)

        # One ticket per user per panel
        for ch in interaction.guild.text_channels:
            topic = ch.topic or ""
            if f"panel={self.panel_id}" in topic and f"opener={interaction.user.id}" in topic:
                return await interaction.response.send_message(f"You already have a ticket for this panel: {ch.mention}", ephemeral=True)

        # Next ticket id
        ticket_seq = ts["next_ticket_seq"]
        ts["next_ticket_seq"] = ticket_seq + 1
        self.bot.save()

        opener_name = (interaction.user.display_name or interaction.user.name).lower().replace(" ", "-")
        name = f"{ticket_seq:03d}-{opener_name}-(unclaimed)"

        # Overwrites: opener, bot, panel roles, admins
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        for rid in panel.get("role_ids", []):
            role = interaction.guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        for role in interaction.guild.roles:
            if role.permissions.manage_channels:
                overwrites.setdefault(role, discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True))

        topic = f"ticket_meta|panel={self.panel_id}|ticket={ticket_seq}|opener={interaction.user.id}|claimer=none"
        ch = await interaction.guild.create_text_channel(
            name=name, category=category, overwrites=overwrites, topic=topic
        )

        await interaction.response.send_message(f"Ticket created: {ch.mention}", ephemeral=True)

        open_text = panel.get("open_text") or "A staff member will be with you shortly."
        await ch.send(
            f"Hey {interaction.user.mention}! {open_text}\n\nUse the buttons below to **Claim** or **Close & Transcript**.",
            view=TicketControlsView(self.bot),
        )

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

        # rest of your code...

    async for m in interaction.channel.history(limit=50, oldest_first=False):
        if m.author == interaction.user and not m.author.bot and m.content:
            g = self.bot.gcfg(interaction.guild_id)["coach"]
            new_topic = topic.replace("submitted=none", f"submitted={m.id}")
            await interaction.channel.edit(topic=new_topic)

            # 🔹 Public message to let admins know
            await interaction.channel.send(
                "📢 **Application submitted!** An admin may now review it.",
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=False)
            )

            # Private confirmation to the submitter
            return await interaction.response.send_message("✅ Submitted for review.", ephemeral=True)

    await interaction.response.send_message("Couldn't find your template message.", ephemeral=True)

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
@app_commands.command(name="setup", description="(Tickets) Set a transcript log channel (optional)")
@app_commands.checks.has_permissions(manage_guild=True)
async def tickets_setup(interaction: discord.Interaction, log_channel: Optional[discord.TextChannel] = None):
    g = bot.gcfg(interaction.guild_id)
    ts = g["tickets"]
    if log_channel:
        ts["log_channel_id"] = log_channel.id
        bot.save()
        return await interaction.response.send_message(f"🧾 Tickets log set to {log_channel.mention}.", ephemeral=True)
    await interaction.response.send_message("🧾 Tickets log unchanged.", ephemeral=True)

@app_commands.command(
    name="panel",
    description="Create a ticket panel with its own category, intro text, and handler roles"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def tickets_panel(
    interaction: discord.Interaction,
    category: discord.CategoryChannel,
    panel_text: Optional[str] = None,
    ticket_open_text: Optional[str] = None,
    role1: Optional[discord.Role] = None,
    role2: Optional[discord.Role] = None,
    role3: Optional[discord.Role] = None,
    role4: Optional[discord.Role] = None,
    role5: Optional[discord.Role] = None,
    post_in: Optional[discord.TextChannel] = None,
):
    target = post_in or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("Pick a text channel to post the panel.", ephemeral=True)

    g = bot.gcfg(interaction.guild_id)
    ts = g["tickets"]

    panel_id = ts["next_panel_id"]
    ts["next_panel_id"] = panel_id + 1
    ts["panels"][str(panel_id)] = {
        "category_id": category.id,
        "open_text": ticket_open_text or "A staff member will be with you shortly.",
        "role_ids": [r.id for r in [role1, role2, role3, role4, role5] if r],
    }
    bot.save()

    view = OpenPanelView(bot, guild_id=interaction.guild_id, panel_id=panel_id)
    bot.add_view(view)  # persist this button across restarts

    msg = panel_text or "Need help? Click below to open a private ticket."
    await target.send(msg, view=view)

    roles_str = ", ".join(r.mention for r in [role1, role2, role3, role4, role5] if r) or "None"
    await interaction.response.send_message(
        f"✅ Panel **#{panel_id}** posted in {target.mention}\n"
        f"• Category: **{category.name}**\n"
        f"• Handler roles: {roles_str}",
        ephemeral=True
    )

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

@app_commands.command(name="panel_roles", description="Set/replace handler roles for an existing panel")
@app_commands.checks.has_permissions(manage_guild=True)
async def tickets_panel_roles(
    interaction: discord.Interaction,
    panel_id: int,
    role1: Optional[discord.Role] = None,
    role2: Optional[discord.Role] = None,
    role3: Optional[discord.Role] = None,
    role4: Optional[discord.Role] = None,
    role5: Optional[discord.Role] = None,
):
    g = bot.gcfg(interaction.guild_id)
    ts = g["tickets"]
    key = str(panel_id)
    if key not in ts["panels"]:
        return await interaction.response.send_message(f"Panel #{panel_id} not found.", ephemeral=True)

    ts["panels"][key]["role_ids"] = [r.id for r in [role1, role2, role3, role4, role5] if r]
    bot.save()
    roles_str = ", ".join(r.mention for r in [role1, role2, role3, role4, role5] if r) or "None"
    await interaction.response.send_message(f"✅ Updated roles for panel **#{panel_id}** → {roles_str}", ephemeral=True)

@app_commands.command(name="panels", description="List configured ticket panels")
@app_commands.checks.has_permissions(manage_guild=True)
async def tickets_panels_list(interaction: discord.Interaction):
    g = bot.gcfg(interaction.guild_id)
    ts = g["tickets"]
    if not ts["panels"]:
        return await interaction.response.send_message("No panels configured yet.", ephemeral=True)

    lines = []
    for pid, pdata in sorted(ts["panels"].items(), key=lambda x: int(x[0])):
        cat = interaction.guild.get_channel(pdata["category_id"])
        roles = [interaction.guild.get_role(rid) for rid in pdata.get("role_ids", [])]
        role_mentions = ", ".join(r.mention for r in roles if r) or "None"
        lines.append(f"**#{pid}** • Category: **{getattr(cat, 'name', 'Unknown')}** • Roles: {role_mentions}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

bot.tree.add_command(tickets_setup)
bot.tree.add_command(tickets_panel)
bot.tree.add_command(tickets_panels_list)
bot.tree.add_command(tickets_panel_roles)
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
