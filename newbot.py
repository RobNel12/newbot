# ultra_ticket_bot.py
# Python 3.10+ | discord.py 2.3+
import os
import io
import json
import asyncio
import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

CONFIG_PATH = "ticket_ultra_config.json"

# ---------- tiny persistence ----------
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
    g.setdefault("default_category_id", None)          # optional global default
    g.setdefault("log_channel_id", None)               # where transcripts go
    # panels: {panel_id: {"category_id": int, "open_text": str, "role_ids": [int,...]}}
    g.setdefault("panels", {})
    g.setdefault("next_panel_id", 1)
    g.setdefault("next_ticket_seq", 1)                 # monotonically increasing ticket counter
    return g

# ---------- bot ----------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True  # needed for transcripts

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.store = load_all()

    async def setup_hook(self):
        # Persistent ticket controls (Claim + Close)
        self.add_view(TicketControlsView(self))

        # Register a persistent view for every saved panel (custom_id includes panel_id)
        for gid, gdata in self.store.items():
            for pid in gdata.get("panels", {}).keys():
                try:
                    self.add_view(OpenPanelView(self, guild_id=int(gid), panel_id=int(pid)))
                except Exception:
                    pass

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

bot = Bot()

# ---------- constants ----------
OPEN_PREFIX  = "ultra_ticket_open"     # custom_id = f"{OPEN_PREFIX}:{guild_id}:{panel_id}"
CLOSE_ID     = "ultra_ticket_close_v1"
CLAIM_ID     = "ultra_ticket_claim_v1"

# ---------- helpers ----------
def slugify_username(name: str) -> str:
    """lowercase, replace spaces with '-', keep a-z0-9 and '-' only."""
    base = name.lower().strip()
    base = base.replace(" ", "-")
    base = re.sub(r"[^a-z0-9\-]", "", base)
    # Avoid empty slug
    return base or "user"

def meta_pack(panel_id: int, ticket_seq: int, opener_id: int, claimer_id: Optional[int] = None) -> str:
    return f"ticket_meta|panel={panel_id}|ticket={ticket_seq}|opener={opener_id}|claimer={claimer_id if claimer_id else 'none'}"

def meta_unpack(topic: Optional[str]) -> Dict[str, Optional[int]]:
    out = {"panel": None, "ticket": None, "opener": None, "claimer": None}
    if not topic or "ticket_meta" not in topic:
        return out
    try:
        parts = dict(pair.split("=", 1) for pair in topic.split("|")[1:])
        out["panel"] = int(parts.get("panel")) if parts.get("panel") else None
        out["ticket"] = int(parts.get("ticket")) if parts.get("ticket") else None
        out["opener"] = int(parts.get("opener")) if parts.get("opener") else None
        cl = parts.get("claimer")
        out["claimer"] = None if (cl in (None, "none")) else int(cl)
    except Exception:
        pass
    return out

async def fetch_member_safe(guild: discord.Guild, user_id: Optional[int]) -> Optional[discord.Member]:
    if not user_id:
        return None
    m = guild.get_member(user_id)
    if m:
        return m
    try:
        return await guild.fetch_member(user_id)
    except Exception:
        return None

def format_ticket_name(ticket_seq: int, opener_name: str, claimer_name: Optional[str]) -> str:
    seq = f"{ticket_seq:03d}"
    if claimer_name:
        return f"{seq}-{opener_name}-{claimer_name}"
    else:
        # NOTE: parentheses/spaces in channel names are allowed by Discord UI (it auto-slugifies if needed).
        # If your guild disallows them, Discord will coerce to dashes automatically.
        return f"{seq}-{opener_name}-(unclaimed)"

def user_has_panel_permission(member: discord.Member, panel_role_ids: List[int]) -> bool:
    if member.guild_permissions.manage_channels:
        return True
    member_role_ids = {r.id for r in getattr(member, "roles", [])}
    return bool(member_role_ids.intersection(panel_role_ids))

# ---------- views ----------
class OpenPanelView(discord.ui.View):
    """Persistent 'Open Ticket' button tied to a specific panel (category + opening text + roles)."""
    def __init__(self, bot: Bot, guild_id: int, panel_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.panel_id = panel_id

        custom_id = f"{OPEN_PREFIX}:{guild_id}:{panel_id}"
        button = discord.ui.Button(
            label="🎫 Open Ticket",
            style=discord.ButtonStyle.primary,
            custom_id=custom_id
        )
        button.callback = self.open_ticket
        self.add_item(button)

    async def open_ticket(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            return await interaction.response.send_message("Use this in the server where the panel was posted.", ephemeral=True)

        g = self.bot.gcfg(self.guild_id)
        panel = g["panels"].get(str(self.panel_id))
        if not panel:
            return await interaction.response.send_message("This panel is no longer configured.", ephemeral=True)

        category_id = panel.get("category_id") or g.get("default_category_id")
        if not category_id:
            return await interaction.response.send_message("No ticket category is configured for this panel.", ephemeral=True)
        category = interaction.guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("Configured category no longer exists. Ask an admin to reconfigure.", ephemeral=True)

        # One ticket per user per panel -> scan for topic metadata
        for ch in interaction.guild.text_channels:
            meta = meta_unpack(ch.topic)
            if meta["panel"] == self.panel_id and meta["opener"] == interaction.user.id:
                return await interaction.response.send_message(f"You already have a ticket for this panel: {ch.mention}", ephemeral=True)

        # Allocate ticket sequence (per guild)
        ticket_seq = g["next_ticket_seq"]
        g["next_ticket_seq"] = ticket_seq + 1
        self.bot.save()

        opener_slug = slugify_username(interaction.user.display_name or interaction.user.name)
        name = format_ticket_name(ticket_seq, opener_slug, None)

        role_ids: List[int] = panel.get("role_ids", [])

        # Overwrites: opener, bot, roles for this panel, and anyone with Manage Channels
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True, embed_links=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        # Panel-assigned roles
        for rid in role_ids:
            role = interaction.guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        # Admin override
        for role in interaction.guild.roles:
            if role.permissions.manage_channels:
                overwrites.setdefault(role, discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True))

        topic = meta_pack(panel_id=self.panel_id, ticket_seq=ticket_seq, opener_id=interaction.user.id, claimer_id=None)
        ch = await interaction.guild.create_text_channel(
            name=name,
            category=category,
            overwrites=overwrites,
            topic=topic
        )

        await interaction.response.send_message(f"Ticket created: {ch.mention}", ephemeral=True)

        open_text = panel.get("open_text") or "A staff member will be with you shortly."
        intro = (
            f"Hey {interaction.user.mention}! {open_text}\n\n"
            f"Use the buttons below to **Claim** or **Close & Transcript**."
        )
        await ch.send(intro, view=TicketControlsView(self.bot))

class TicketControlsView(discord.ui.View):
    """Persistent controls for tickets: Claim + Close."""
    def __init__(self, bot: Bot):
        super().__init__(timeout=None)
        self.bot = bot

        claim_btn = discord.ui.Button(
            label="🪪 Claim",
            style=discord.ButtonStyle.success,
            custom_id=CLAIM_ID
        )
        claim_btn.callback = self.claim
        self.add_item(claim_btn)

        close_btn = discord.ui.Button(
            label="✅ Close & Transcript",
            style=discord.ButtonStyle.secondary,
            custom_id=CLOSE_ID
        )
        close_btn.callback = self.close
        self.add_item(close_btn)

    async def claim(self, interaction: discord.Interaction):
        if not (interaction.guild and isinstance(interaction.channel, discord.TextChannel)):
            return await interaction.response.send_message("Not a ticket channel.", ephemeral=True)

        meta = meta_unpack(interaction.channel.topic)
        if meta["panel"] is None:
            return await interaction.response.send_message("This ticket is missing metadata; cannot claim.", ephemeral=True)

        g = self.bot.gcfg(interaction.guild.id)
        panel = g["panels"].get(str(meta["panel"]), {})
        panel_roles: List[int] = panel.get("role_ids", [])

        if not user_has_panel_permission(interaction.user, panel_roles):
            return await interaction.response.send_message("You’re not allowed to claim this ticket.", ephemeral=True)

        if meta["claimer"]:
            # Already claimed
            claimer = await fetch_member_safe(interaction.guild, meta["claimer"])
            return await interaction.response.send_message(
                f"Already claimed by **{claimer.display_name if claimer else meta['claimer']}**.",
                ephemeral=True
            )

        # Set claimer, rename channel
        claimer_slug = slugify_username(interaction.user.display_name or interaction.user.name)
        opener = await fetch_member_safe(interaction.guild, meta["opener"])
        opener_slug = slugify_username(opener.display_name if opener else "user")
        new_name = format_ticket_name(meta["ticket"] or 0, opener_slug, claimer_slug)

        # Update topic meta
        new_topic = meta_pack(panel_id=meta["panel"], ticket_seq=meta["ticket"] or 0, opener_id=meta["opener"] or 0, claimer_id=interaction.user.id)

        try:
            await interaction.channel.edit(name=new_name, topic=new_topic, reason=f"Ticket claimed by {interaction.user}")
        except discord.HTTPException:
            # If rename fails, still set topic
            await interaction.channel.edit(topic=new_topic, reason=f"Ticket claimed by {interaction.user}")

        await interaction.response.send_message(f"Claimed by {interaction.user.mention}.", ephemeral=False)

    async def close(self, interaction: discord.Interaction):
        if not (interaction.guild and isinstance(interaction.channel, discord.TextChannel)):
            return await interaction.response.send_message("Not a ticket channel.", ephemeral=True)

        meta = meta_unpack(interaction.channel.topic)
        if meta["panel"] is None:
            return await interaction.response.send_message("This ticket is missing metadata; cannot close.", ephemeral=True)

        # Permissions to close: opener, panel roles, or Manage Channels
        g = self.bot.gcfg(interaction.guild.id)
        panel = g["panels"].get(str(meta["panel"]), {})
        panel_roles: List[int] = panel.get("role_ids", [])

        can_close = (
            (meta["opener"] == interaction.user.id) or
            user_has_panel_permission(interaction.user, panel_roles)
        )
        if not can_close:
            return await interaction.response.send_message("You can’t close this ticket.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Build transcript text
        ch = interaction.channel
        opener_member = await fetch_member_safe(interaction.guild, meta["opener"])
        claimer_member = await fetch_member_safe(interaction.guild, meta["claimer"])
        closer_member = interaction.user

        lines = []
        header = (
            f"[Transcript] #{ch.name} • Guild: {interaction.guild.name} • "
            f"Opened by: {opener_member} ({meta['opener']}) • "
            f"Claimed by: {(claimer_member or 'None')} "
            f"• Closed by: {closer_member} ({closer_member.id}) • "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}"
        )
        lines.append(header)
        lines.append("-" * len(header))

        async for m in ch.history(limit=None, oldest_first=True):
            ts = m.created_at.replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
            author = f"{m.author} ({m.author.id})"
            content = (m.content or "").replace("\r", "")
            attach = ""
            if m.attachments:
                attach_urls = ", ".join(a.url for a in m.attachments)
                attach = f" [attachments: {attach_urls}]"
            lines.append(f"{ts} | {author}: {content}{attach}")

        transcript_text = "\n".join(lines).encode("utf-8", errors="replace")
        fname = f"transcript_{ch.name}_{int(datetime.now().timestamp())}.txt"
        file = discord.File(fp=io.BytesIO(transcript_text), filename=fname)

        # Send to log channel if configured
        log_id = g.get("log_channel_id")
        log_channel: Optional[discord.TextChannel] = None
        if log_id:
            cand = interaction.guild.get_channel(log_id)
            if isinstance(cand, discord.TextChannel):
                log_channel = cand

        embed = discord.Embed(title="Ticket Closed", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Channel", value=ch.mention, inline=True)
        embed.add_field(name="Ticket ID", value=f"{(meta['ticket'] or 0):03d}", inline=True)
        embed.add_field(name="Panel", value=str(meta["panel"]), inline=True)
        embed.add_field(name="Opened By", value=f"{opener_member} ({meta['opener']})", inline=False)
        embed.add_field(name="Claimed By", value=str(claimer_member) if claimer_member else "None", inline=False)
        embed.add_field(name="Closed By", value=f"{interaction.user} ({interaction.user.id})", inline=False)

        if log_channel:
            await log_channel.send(embed=embed, file=file)
        else:
            await ch.send(embed=embed)
            await ch.send("Transcript (no log channel configured):", file=file)

        await ch.send("Closing in 3 seconds…")
        await asyncio.sleep(3)
        try:
            await ch.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            await ch.edit(name=f"closed-{ch.name}", reason="Close fallback")

# ---------- commands ----------
@app_commands.command(name="setup", description="Set a default category (optional) and/or a transcript log channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_cmd(
    interaction: discord.Interaction,
    default_category: Optional[discord.CategoryChannel] = None,
    log_channel: Optional[discord.TextChannel] = None
):
    g = bot.gcfg(interaction.guild_id)
    if default_category:
        g["default_category_id"] = default_category.id
    if log_channel:
        g["log_channel_id"] = log_channel.id
    bot.save()

    parts = []
    parts.append(f"✅ Default category: **{default_category.name}**" if default_category else "✅ Default category unchanged.")
    parts.append(f"🧾 Log channel: {log_channel.mention}" if log_channel else "🧾 Log channel unchanged.")
    await interaction.response.send_message(" | ".join(parts), ephemeral=True)

@app_commands.command(name="panel", description="Post a panel bound to a category, custom opening text, and handler roles")
@app_commands.checks.has_permissions(manage_guild=True)
async def panel_cmd(
    interaction: discord.Interaction,
    category: Optional[discord.CategoryChannel],
    ticket_message: Optional[str] = None,
    channel: Optional[discord.TextChannel] = None,
    panel_text: Optional[str] = None,
    role1: Optional[discord.Role] = None,
    role2: Optional[discord.Role] = None,
    role3: Optional[discord.Role] = None,
    role4: Optional[discord.Role] = None,
    role5: Optional[discord.Role] = None,
):
    """
    Create & post a panel.
    - category: which category new ticket channels will be created in (required unless default set)
    - ticket_message: custom text sent inside each new ticket opened from this panel
    - channel: where to post the panel (defaults to current channel)
    - panel_text: the message that accompanies the button
    - role1..role5: roles permitted to view/handle/claim/close tickets from this panel
    """
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("Pick a text channel.", ephemeral=True)

    g = bot.gcfg(interaction.guild_id)
    cat_id = category.id if category else g.get("default_category_id")
    if not cat_id:
        return await interaction.response.send_message("No category provided and no default configured. Set one with `/setup` or pass a category here.", ephemeral=True)

    role_ids = [r.id for r in [role1, role2, role3, role4, role5] if r]

    # Create and store a new panel
    panel_id = g["next_panel_id"]
    g["next_panel_id"] = panel_id + 1
    g["panels"][str(panel_id)] = {
        "category_id": cat_id,
        "open_text": ticket_message or "A staff member will be with you shortly.",
        "role_ids": role_ids
    }
    bot.save()

    # Persistent view for this panel id
    view = OpenPanelView(bot, guild_id=interaction.guild_id, panel_id=panel_id)
    bot.add_view(view)

    text = panel_text or "Need help? Click below to open a private ticket."
    await target.send(text, view=view)

    # Nice confirmation
    cat = interaction.guild.get_channel(cat_id)
    role_mentions = ", ".join(interaction.guild.get_role(r).mention for r in role_ids if interaction.guild.get_role(r)) or "None"
    await interaction.response.send_message(
        f"✅ Panel **#{panel_id}** posted in {target.mention}.\n"
        f"• Category: **{getattr(cat, 'name', 'Unknown')}**\n"
        f"• Handler roles: {role_mentions}",
        ephemeral=True
    )

@app_commands.command(name="panel_roles", description="Set/replace handler roles for an existing panel")
@app_commands.checks.has_permissions(manage_guild=True)
async def panel_roles_cmd(
    interaction: discord.Interaction,
    panel_id: int,
    role1: Optional[discord.Role] = None,
    role2: Optional[discord.Role] = None,
    role3: Optional[discord.Role] = None,
    role4: Optional[discord.Role] = None,
    role5: Optional[discord.Role] = None,
):
    g = bot.gcfg(interaction.guild_id)
    key = str(panel_id)
    if key not in g["panels"]:
        return await interaction.response.send_message(f"Panel #{panel_id} not found.", ephemeral=True)

    new_role_ids = [r.id for r in [role1, role2, role3, role4, role5] if r]
    g["panels"][key]["role_ids"] = new_role_ids
    bot.save()

    role_mentions = ", ".join(r.mention for r in [role1, role2, role3, role4, role5] if r) or "None"
    await interaction.response.send_message(
        f"✅ Updated roles for panel **#{panel_id}** → {role_mentions}\n"
        f"(Members with **Manage Channels** still have access.)",
        ephemeral=True
    )

@app_commands.command(name="panels", description="List configured panels")
@app_commands.checks.has_permissions(manage_guild=True)
async def panels_list_cmd(interaction: discord.Interaction):
    g = bot.gcfg(interaction.guild_id)
    panels = g.get("panels", {})
    if not panels:
        return await interaction.response.send_message("No panels configured yet.", ephemeral=True)

    lines = []
    for pid, pdata in sorted(panels.items(), key=lambda kv: int(kv[0])):
        cat = interaction.guild.get_channel(pdata.get("category_id"))
        roles = [interaction.guild.get_role(rid) for rid in pdata.get("role_ids", [])]
        role_mentions = ", ".join(r.mention for r in roles if r) or "None"
        lines.append(f"**#{pid}** • Category: **{getattr(cat, 'name', 'Unknown')}** • Roles: {role_mentions}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)

# Register commands
bot.tree.add_command(setup_cmd)
bot.tree.add_command(panel_cmd)
bot.tree.add_command(panel_roles_cmd)
bot.tree.add_command(panels_list_cmd)

# ---------- run ----------
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Discord token missing. Set DISCORD_TOKEN environment variable.")
    bot.run(token)

if __name__ == "__main__":
    main()