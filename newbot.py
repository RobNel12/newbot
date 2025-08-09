# Python 3.10+ | discord.py 2.3+
import os
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any

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

def gcfg(data: Dict[str, Any], gid: int) -> Dict[str, Any]:
    s = data.setdefault(str(gid), {})
    s.setdefault("category_id", None)
    s.setdefault("log_channel_id", None)
    return s

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
        # Persistent views so buttons survive restarts
        self.add_view(OpenTicketView(self))
        self.add_view(CloseView(self))

        # Faster dev sync: set GUILD_ID to only sync in one server
        gid = os.getenv("GUILD_ID")
        if gid:
            guild = discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    def cfg(self, gid: int) -> Dict[str, Any]:
        return gcfg(self.store, gid)

    def save(self):
        save_all(self.store)

bot = Bot()

# ---------- constants ----------
OPEN_ID  = "ultra_ticket_open_v1"
CLOSE_ID = "ultra_ticket_close_v1"

# ---------- views ----------
class OpenTicketView(discord.ui.View):
    def __init__(self, bot: Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="🎫 Open Ticket", style=discord.ButtonStyle.primary, custom_id=OPEN_ID)
    async def open_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        cfg = self.bot.cfg(interaction.guild_id)
        cat_id = cfg.get("category_id")
        if not cat_id:
            return await interaction.response.send_message("Ticket category isn’t set yet. Ask an admin to run `/setup`.", ephemeral=True)

        category = interaction.guild.get_channel(cat_id)
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("Configured category no longer exists—run `/setup` again.", ephemeral=True)

        # One ticket per user
        channel_name = f"ticket-{interaction.user.id}"
        existing = discord.utils.get(interaction.guild.text_channels, name=channel_name)
        if existing:
            return await interaction.response.send_message(f"You already have a ticket: {existing.mention}", ephemeral=True)

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True, embed_links=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        # Let members with Manage Channels in
        for role in interaction.guild.roles:
            if role.permissions.manage_channels:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

        ch = await interaction.guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Ticket for {interaction.user} (ID {interaction.user.id})"
        )

        await interaction.response.send_message(f"Ticket created: {ch.mention}", ephemeral=True)
        await ch.send(
            f"Hey {interaction.user.mention}! A staff member will be with you shortly.\n"
            f"Use the button below to **Close & Transcript** when finished.",
            view=CloseView(self.bot)
        )

class CloseView(discord.ui.View):
    def __init__(self, bot: Bot):
        super().__init__(timeout=None)
        self.bot = bot

    def _is_allowed(self, i: discord.Interaction) -> bool:
        if not (i.guild and isinstance(i.channel, discord.TextChannel)):
            return False
        # opener can close
        if i.channel.name.startswith("ticket-"):
            try:
                opener_id = int(i.channel.name.split("-")[1])
                if opener_id == i.user.id:
                    return True
            except Exception:
                pass
        # or anyone with Manage Channels
        perms = i.user.guild_permissions
        return perms.manage_channels

    @discord.ui.button(label="✅ Close & Transcript", style=discord.ButtonStyle.secondary, custom_id=CLOSE_ID)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
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
        file = discord.File(fp=discord.BytesIO(transcript_text), filename=fname)

        # Try to send to log channel if configured
        log_channel: Optional[discord.TextChannel] = None
        cfg = self.bot.cfg(interaction.guild_id)
        log_id = cfg.get("log_channel_id")
        if log_id:
            cand = interaction.guild.get_channel(log_id)
            if isinstance(cand, discord.TextChannel):
                log_channel = cand

        if log_channel:
            await log_channel.send(f"Transcript from {ch.mention}", file=file)
        else:
            # fallback: post in channel before deletion
            await ch.send("Transcript (no log channel configured):", file=file)

        await ch.send("Closing in 3 seconds…")
        await asyncio.sleep(3)
        try:
            await ch.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            await ch.edit(name=f"closed-{ch.name}", reason="Close fallback")

# ---------- commands ----------
@app_commands.command(name="setup", description="Set ticket category and optional transcript log channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_cmd(interaction: discord.Interaction, category: discord.CategoryChannel, log_channel: Optional[discord.TextChannel] = None):
    cfg = bot.cfg(interaction.guild_id)
    cfg["category_id"] = category.id
    cfg["log_channel_id"] = log_channel.id if log_channel else None
    bot.save()
    msg = f"✅ Ticket category set to **{category.name}**."
    if log_channel:
        msg += f" Transcripts will go to {log_channel.mention}."
    else:
        msg += " No log channel set; transcripts will be posted in tickets before deletion."
    await interaction.response.send_message(msg, ephemeral=True)

@app_commands.command(name="panel", description="Post the Open Ticket button here or in a specified channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def panel_cmd(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None, message: Optional[str] = None):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("Pick a text channel.", ephemeral=True)
    text = message or "Need help? Click below to open a private support ticket."
    await target.send(text, view=OpenTicketView(bot))
    await interaction.response.send_message(f"Panel posted in {target.mention}.", ephemeral=True)

bot.tree.add_command(setup_cmd)
bot.tree.add_command(panel_cmd)

# ---------- run ----------
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Discord token missing. Set DISCORD_TOKEN environment variable.")
    bot.run(token)

if __name__ == "__main__":
    main()
