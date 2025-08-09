# coach_roster_bot.py
# Python 3.10+ | discord.py 2.3+
# A lightweight, separate bot that manages a "coach roster" with ADMIN APPROVAL.
# Flow:
#  - Admin runs /coach_setup to set the application ticket category + roster channel (+ optional log channel)
#  - Admin runs /coach_panel to post a panel. Coaches click to open a private ticket
#  - Bot posts the coaching template prompt in the ticket + a control view (Submit + Approve + Close & Log)
#  - Coach pastes their filled template as a normal message, then clicks "Submit"
#    -> Bot records the message ID in topic metadata and marks the ticket as submitted (but NOT posted)
#  - Admin clicks "Approve"
#    -> Bot pulls the stored message, builds an embed, and posts it to the roster channel; marks approved + entry id
#  - Admin can then "Close & Log" to export a transcript to the log channel and delete the ticket.
#
# NOTE: Enable "Message Content Intent" in the Discord Developer Portal, since the bot reads message contents.

import os
import io
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

CONFIG_PATH = "coach_roster_config.json"

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
    g.setdefault("category_id", None)          # where coach application tickets go
    g.setdefault("roster_channel_id", None)    # "running list" channel for roster entries
    g.setdefault("log_channel_id", None)       # where transcripts go (optional)
    g.setdefault("next_entry_id", 1)           # simple incremental roster id
    g.setdefault("template_text", None)        # last template text used
    return g

# ---------- topic helpers ----------
def topic_pack(
    opener_id: int,
    created_iso: str,
    submitted_iso: Optional[str] = None,
    submitted_msg_id: Optional[int] = None,
    approved_iso: Optional[str] = None,
    entry_id: Optional[int] = None
) -> str:
    return (
        "coach_app"
        f"|opener={opener_id}"
        f"|created={created_iso}"
        f"|submitted={submitted_iso if submitted_iso else 'none'}"
        f"|smsg={submitted_msg_id if submitted_msg_id else 'none'}"
        f"|approved={approved_iso if approved_iso else 'none'}"
        f"|entry={entry_id if entry_id else 'none'}"
    )

def topic_unpack(topic: Optional[str]) -> Dict[str, Optional[str]]:
    out = {"opener": None, "created": None, "submitted": None, "smsg": None, "approved": None, "entry": None}
    if not topic or "coach_app" not in topic:
        return out
    try:
        kv = dict(pair.split("=", 1) for pair in topic.split("|")[1:])
        out["opener"] = kv.get("opener")
        out["created"] = kv.get("created")
        out["submitted"] = None if (kv.get("submitted") in (None, "none")) else kv.get("submitted")
        out["smsg"] = None if (kv.get("smsg") in (None, "none")) else kv.get("smsg")
        out["approved"] = None if (kv.get("approved") in (None, "none")) else kv.get("approved")
        out["entry"] = None if (kv.get("entry") in (None, "none")) else kv.get("entry")
    except Exception:
        pass
    return out

# ---------- bot ----------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True  # needed to read the coach's filled template message

class CoachBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.store = load_all()

    async def setup_hook(self):
        # Register persistent control view (Submit + Approve + Close & Log)
        self.add_view(CoachControlsView(self))

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

bot = CoachBot()

# ---------- constants ----------
SUBMIT_ID  = "coach_submit_v3"
APPROVE_ID = "coach_approve_v1"
CLOSE_ID   = "coach_close_log_v1"

DEFAULT_TEMPLATE = (
    "**Coaching Template**\n"
    "• IGN: \n"
    "• Region/Time Zone: \n"
    "• Availability: \n"
    "• Roles/Specs Coached: \n"
    "• Experience/Certifications: \n"
    "• Rates (if any): \n"
    "• Contact: \n"
    "\n"
    "_Paste your completed template as a message below, then press **Submit**._"
)

# ---------- views ----------
class CoachControlsView(discord.ui.View):
    """Controls for coach application tickets: Submit (coach) + Approve (admin) + Close & Log (admin)"""
    def __init__(self, bot: CoachBot):
        super().__init__(timeout=None)
        self.bot = bot

        submit_btn = discord.ui.Button(
            label="📥 Submit",
            style=discord.ButtonStyle.success,
            custom_id=SUBMIT_ID
        )
        submit_btn.callback = self.submit
        self.add_item(submit_btn)

        approve_btn = discord.ui.Button(
            label="✅ Approve (Admin)",
            style=discord.ButtonStyle.primary,
            custom_id=APPROVE_ID
        )
        approve_btn.callback = self.approve
        self.add_item(approve_btn)

        close_btn = discord.ui.Button(
            label="🧾 Close & Log (Admin)",
            style=discord.ButtonStyle.secondary,
            custom_id=CLOSE_ID
        )
        close_btn.callback = self.close_and_log
        self.add_item(close_btn)

    async def submit(self, interaction: discord.Interaction):
        # Only valid in a text channel within a guild
        if not (interaction.guild and isinstance(interaction.channel, discord.TextChannel)):
            return await interaction.response.send_message("This isn't a valid ticket channel.", ephemeral=True)

        # Ensure topic is correct
        meta = topic_unpack(interaction.channel.topic)
        opener_id = meta.get("opener")

        # Only opener can submit (admins can advise, but not submit)
        if str(interaction.user.id) != str(opener_id):
            return await interaction.response.send_message("Only the opener can submit their application.", ephemeral=True)

        # Find opener's latest non-bot message
        latest = None
        async for m in interaction.channel.history(limit=200, oldest_first=False):
            if m.author.id == interaction.user.id and not m.author.bot and m.type == discord.MessageType.default:
                latest = m
                break

        if latest is None or (not latest.content and not latest.attachments):
            return await interaction.response.send_message("I couldn't find your filled template message. Paste it in this channel, then press Submit again.", ephemeral=True)

        # Mark submitted in topic and store the message id
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_topic = topic_pack(
            opener_id=interaction.user.id,
            created_iso=meta.get("created") or now_iso,
            submitted_iso=now_iso,
            submitted_msg_id=latest.id,
            approved_iso=None,
            entry_id=None
        )
        try:
            new_name = f"{interaction.channel.name}-submitted"
            await interaction.channel.edit(name=new_name, topic=new_topic, reason=f"Coach submitted by {interaction.user}")
        except Exception:
            try:
                await interaction.channel.edit(topic=new_topic, reason=f"Coach submitted by {interaction.user}")
            except Exception:
                pass

        await interaction.response.send_message("✅ Submitted! An admin will review and approve your entry.", ephemeral=True)
        # Notify admins in-channel
        await interaction.channel.send("A new application has been **submitted** and awaits **Admin Approval**.")

    async def approve(self, interaction: discord.Interaction):
        if not (interaction.guild and isinstance(interaction.channel, discord.TextChannel)):
            return await interaction.response.send_message("Not a valid ticket channel.", ephemeral=True)

        # Admins only (Manage Channels or Manage Guild)
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.manage_guild):
            return await interaction.response.send_message("Admins only.", ephemeral=True)

        meta = topic_unpack(interaction.channel.topic)
        if not meta.get("submitted") or not meta.get("smsg"):
            return await interaction.response.send_message("Nothing to approve yet. The coach must submit first.", ephemeral=True)

        # Already approved?
        if meta.get("approved") and meta.get("entry"):
            return await interaction.response.send_message("This application was already approved and posted.", ephemeral=True)

        # Fetch the submitted message
        try:
            msg_id = int(meta["smsg"])  # type: ignore
            submitted_msg = await interaction.channel.fetch_message(msg_id)
        except Exception:
            return await interaction.response.send_message("Couldn't load the submitted message. Ask the coach to re-submit.", ephemeral=True)

        g = bot.gcfg(interaction.guild_id)
        roster_id = g.get("roster_channel_id")
        roster_channel = interaction.guild.get_channel(roster_id) if roster_id else None
        if not isinstance(roster_channel, discord.TextChannel):
            return await interaction.response.send_message("Roster channel not configured. Ask an admin to run /coach_setup.", ephemeral=True)

        # Create roster entry
        entry_id = g["next_entry_id"]
        g["next_entry_id"] = entry_id + 1
        bot.save()

        opener_id = int(meta.get("opener") or interaction.user.id)
        opener = interaction.guild.get_member(opener_id) or submitted_msg.author
        timestamp = datetime.now(timezone.utc)

        embed = discord.Embed(title=f"Coach #{entry_id:03d}", color=discord.Color.green(), timestamp=timestamp)
        embed.add_field(name="Coach", value=f"{opener} (`{opener.id}`)", inline=False)
        embed.add_field(name="Source", value=f"{interaction.channel.mention}", inline=True)

        content = submitted_msg.content or ""
        file = None
        if len(content) > 1900:
            data = content.encode("utf-8", errors="replace")
            file = discord.File(fp=io.BytesIO(data), filename=f"coach_{entry_id:03d}.txt")
            embed.add_field(name="Template", value="(attached as file)", inline=False)
        else:
            embed.add_field(name="Template", value=content if content else "(no text; see attachments)", inline=False)

        if submitted_msg.attachments:
            urls = "\n".join(a.url for a in submitted_msg.attachments)
            embed.add_field(name="Attachments", value=urls, inline=False)

        if file:
            await roster_channel.send(embed=embed, file=file)
        else:
            await roster_channel.send(embed=embed)

        # Update topic: mark approved and attach entry id
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_topic = topic_pack(
            opener_id=opener_id,
            created_iso=meta.get("created") or now_iso,
            submitted_iso=meta.get("submitted"),
            submitted_msg_id=msg_id,
            approved_iso=now_iso,
            entry_id=entry_id
        )
        try:
            new_name = f"{interaction.channel.name}-approved"
            await interaction.channel.edit(name=new_name, topic=new_topic, reason=f"Coach application approved by {interaction.user}")
        except Exception:
            try:
                await interaction.channel.edit(topic=new_topic, reason=f"Coach application approved by {interaction.user}")
            except Exception:
                pass

        await interaction.response.send_message(f"✅ Approved and posted to {roster_channel.mention}.", ephemeral=True)
        await interaction.channel.send("✅ This application has been **approved** and posted to the roster.")

    async def close_and_log(self, interaction: discord.Interaction):
        if not (interaction.guild and isinstance(interaction.channel, discord.TextChannel)):
            return await interaction.response.send_message("Not a valid ticket channel.", ephemeral=True)

        # Admins only
        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.manage_guild):
            return await interaction.response.send_message("Admins only.", ephemeral=True)

        meta = topic_unpack(interaction.channel.topic)
        opener_id = meta.get("opener")
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Build transcript text
        ch = interaction.channel
        guild = interaction.guild
        opener_member = guild.get_member(int(opener_id)) if opener_id else None
        closer_member = interaction.user
        submitted_display = meta.get("submitted") or "no"
        approved_display = meta.get("approved") or "no"
        entry_display = meta.get("entry") or "none"

        header = (
            f"[Coach Application Transcript] #{ch.name} • Guild: {guild.name} • "
            f"Opened by: {opener_member or opener_id} • "
            f"Submitted: {submitted_display} • Approved: {approved_display} • Entry: {entry_display} • "
            f"Closed by: {closer_member} ({closer_member.id}) • "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}"
        )

        lines = [header, "-" * len(header)]
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
        fname = f"coach_transcript_{ch.name}_{int(datetime.now().timestamp())}.txt"
        file = discord.File(fp=io.BytesIO(transcript_text), filename=fname)

        # Send to log channel if configured
        g = bot.gcfg(guild.id)
        log_id = g.get("log_channel_id")
        log_channel: Optional[discord.TextChannel] = None
        if log_id:
            cand = guild.get_channel(log_id)
            if isinstance(cand, discord.TextChannel):
                log_channel = cand

        embed = discord.Embed(title="Coach Ticket Closed", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Channel", value=ch.mention, inline=True)
        embed.add_field(name="Opened By", value=str(opener_member or opener_id), inline=False)
        embed.add_field(name="Submitted", value=submitted_display, inline=True)
        embed.add_field(name="Approved", value=approved_display, inline=True)
        embed.add_field(name="Roster Entry", value=str(entry_display), inline=True)
        embed.add_field(name="Closed By", value=f"{closer_member} ({closer_member.id})", inline=False)

        if log_channel:
            await log_channel.send(embed=embed, file=file)
        else:
            await ch.send(embed=embed)
            await ch.send("Transcript (no log channel configured):", file=file)

        await ch.send("Closing in 3 seconds…")
        await asyncio.sleep(3)
        try:
            await ch.delete(reason=f"Coach ticket closed by {interaction.user}")
        except discord.HTTPException:
            await ch.edit(name=f"closed-{ch.name}", reason="Close fallback")

# ---------- slash commands ----------
@app_commands.command(name="coach_setup", description="Set the coach ticket category, roster channel, and optional log channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def coach_setup(
    interaction: discord.Interaction,
    category: Optional[discord.CategoryChannel],
    roster_channel: Optional[discord.TextChannel],
    log_channel: Optional[discord.TextChannel] = None
):
    g = bot.gcfg(interaction.guild_id)
    if category:
        g["category_id"] = category.id
    if roster_channel:
        g["roster_channel_id"] = roster_channel.id
    if log_channel:
        g["log_channel_id"] = log_channel.id
    bot.save()

    cat_name = category.name if category else "(unchanged)"
    rc_name = roster_channel.mention if roster_channel else "(unchanged)"
    lc_name = log_channel.mention if log_channel else "(unchanged)"
    await interaction.response.send_message(
        f"✅ Category: **{cat_name}** | Roster: {rc_name} | Log: {lc_name}", ephemeral=True
    )

@app_commands.command(name="coach_panel", description="Post a coach application panel (uses the configured template or custom text)")
@app_commands.checks.has_permissions(manage_guild=True)
async def coach_panel(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    template_text: Optional[str] = None
):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("Pick a text channel.", ephemeral=True)

    # Verify setup
    g = bot.gcfg(interaction.guild_id)
    cat_id = g.get("category_id")
    if not cat_id:
        return await interaction.response.send_message("Category not configured. Run /coach_setup first.", ephemeral=True)

    # Post the launcher message with an "Open Application" button
    view = OpenCoachTicketView(bot)
    text = "Coaches: click below to open a private application ticket."
    await target.send(text, view=view)
    await interaction.response.send_message(f"Panel posted in {target.mention}.", ephemeral=True)

    # Store the current template in memory for tickets to use (per-guild)
    g["template_text"] = template_text or DEFAULT_TEMPLATE
    bot.save()

class OpenCoachTicketView(discord.ui.View):
    """Button to open a coach application ticket"""
    def __init__(self, bot: CoachBot):
        super().__init__(timeout=None)
        self.bot = bot

        btn = discord.ui.Button(
            label="🎫 Open Application",
            style=discord.ButtonStyle.primary,
            custom_id="coach_open_v3"
        )
        btn.callback = self.open_ticket
        self.add_item(btn)

    async def open_ticket(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        g = self.bot.gcfg(interaction.guild_id)
        category_id = g.get("category_id")
        if not category_id:
            return await interaction.response.send_message("Category not configured. Ask an admin to run /coach_setup.", ephemeral=True)

        category = interaction.guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("Configured category no longer exists. Ask an admin to reconfigure.", ephemeral=True)

        # Avoid dup ticket for same user: search category for channel with their id in topic
        existing = None
        for ch in category.text_channels:
            meta = topic_unpack(ch.topic)
            if str(interaction.user.id) == str(meta.get("opener")):
                existing = ch
                break

        if existing:
            return await interaction.response.send_message(f"You already have an application ticket: {existing.mention}", ephemeral=True)

        # Private overwrites: opener + bot + admin override (Manage Channels)
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
        }
        for role in interaction.guild.roles:
            if role.permissions.manage_channels:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

        # Name channel with user's display name slug (basic)
        slug = (interaction.user.display_name or interaction.user.name).lower().replace(" ", "-")
        name = f"coach-{slug}"[:95]  # keep under Discord's channel name limit

        created_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        topic = topic_pack(opener_id=interaction.user.id, created_iso=created_iso)

        ch = await interaction.guild.create_text_channel(
            name=name,
            category=category,
            overwrites=overwrites,
            topic=topic
        )

        await interaction.response.send_message(f"Application ticket created: {ch.mention}", ephemeral=True)

        template = g.get("template_text") or DEFAULT_TEMPLATE
        await ch.send(template, view=CoachControlsView(self.bot))

# Register commands
bot.tree.add_command(coach_setup)
bot.tree.add_command(coach_panel)

# ---------- run ----------
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Discord token missing. Set DISCORD_TOKEN environment variable.")
    bot.run(token)

if __name__ == "__main__":
    main()