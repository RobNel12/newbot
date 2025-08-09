# ultra_ticket_bot.py
# Python 3.10+ | discord.py 2.3+
import os
import io
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

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
        # Register Close view (constant custom_id)
        self.add_view(CloseView(self))

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
OPEN_PREFIX  = "ultra_ticket_open"   # custom_id = f"{OPEN_PREFIX}:{guild_id}:{panel_id}"
CLOSE_ID     = "ultra_ticket_close_v1"

# ---------- helpers ----------
def parse_panel_id_from_channel_name(name: str) -> Optional[int]:
    # ticket-<panel_id>-<user_id>
    if not name.startswith("ticket-"):
        return None
    parts = name.split("-")
    if len(parts) < 3:
        return None
    try:
        return int(parts[1])
    except Exception:
        return None

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

        # One ticket per user per panel
        channel_name = f"ticket-{self.panel_id}-{interaction.user.id}"
        existing = discord.utils.get(interaction.guild.text_channels, name=channel_name)
        if existing:
            return await interaction.response.send_message(f"You already have a ticket for this panel: {existing.mention}", ephemeral=True)

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
        # Admin override: Manage Channels
        for role in interaction.guild.roles:
            if role.permissions.manage_channels:
                overwrites.setdefault(role, discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True))

        ch = await interaction.guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Ticket (panel {self.panel_id}) for {interaction.user} (ID {interaction.user.id})"
        )

        await interaction.response.send_message(f"Ticket created: {ch.mention}", ephemeral=True)

        open_text = panel.get("open_text") or "A staff member will be with you shortly."
        intro = (
            f"Hey {interaction.user.mention}! {open_text}\n\n"
            f"Use the button below to **Close & Transcript** when finished."
        )
        await ch.send(intro, view=CloseView(self.bot))


class CloseView(discord.ui.View):
    """Persistent 'Close & Transcript' button for any ticket channel."""
    def __init__(self, bot: Bot):
        super().__init__(timeout=None)
        self.bot = bot
        btn = discord.ui.Button(
            label="✅ Close & Transcript",
            style=discord.ButtonStyle.secondary,
            custom_id=CLOSE_ID
        )
        btn.callback = self.close
        self.add_item(btn)

    def _is_allowed(self, i: discord.Interaction) -> bool:
        if not (i.guild and isinstance(i.channel, discord.TextChannel)):
            return False

        # opener can close
        parts = i.channel.name.split("-")
        if len(parts) >= 3:
            try:
                opener_id = int(parts[2])
                if opener_id == i.user.id:
                    return True
            except Exception:
                pass

        # panel-specific roles can close, and admins (Manage Channels)
        panel_id = parse_panel_id_from_channel_name(i.channel.name)
        g = self.bot.gcfg(i.guild.id)
        panel = g["panels"].get(str(panel_id), {}) if panel_id is not None else {}
        allowed_role_ids: List[int] = panel.get("role_ids", [])

        user_role_ids = {r.id for r in getattr(i.user, "roles", [])}
        if user_role_ids.intersection(allowed_role_ids):
            return True

        return i.user.guild_permissions.manage_channels

    async def close(self, interaction: discord.Interaction):
        if not self._is_allowed(interaction):
            return await interaction.response.send_message("You can’t close this ticket.", ephemeral=True)
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Not a ticket channel.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Build transcript text
        ch = interaction.channel
        lines = []
        header = f"[Transcript] #{ch.name} • Guild: {interaction.guild.name} • Closed by: {interaction.user} • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}"
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
        g = self.bot.gcfg(interaction.guild_id)
        log_id = g.get("log_channel_id")
        log_channel: Optional[discord.TextChannel] = None
        if log_id:
            cand = interaction.guild.get_channel(log_id)
            if isinstance(cand, discord.TextChannel):
                log_channel = cand

        if log_channel:
            await log_channel.send(f"Transcript from {ch.mention}", file=file)
        else:
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
    - panel_text: the message that accompanies the button (defaults to a generic prompt)
    - role1..role5: roles permitted to view/handle/close tickets from this panel
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