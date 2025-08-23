# cogs/application_tickets.py
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, Dict, Any, List, Tuple
import asyncio
import json
import os
import io
import datetime
from collections import Counter

CONFIG_FILE = "app_ticket_config.json"

# ---------------- Persistence ----------------
def _load_cfg() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_cfg(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def gkey(guild: discord.Guild) -> str:
    return str(guild.id)

def get_guild_cfg(guild: discord.Guild) -> Dict[str, Any]:
    cfg = _load_cfg()
    return cfg.get(gkey(guild), {})

def update_guild_cfg(guild: discord.Guild, updates: Dict[str, Any]):
    cfg = _load_cfg()
    g = cfg.get(gkey(guild), {})
    g.update(updates)
    cfg[gkey(guild)] = g
    _save_cfg(cfg)

# ---------------- Utilities ----------------
def role_from_id(guild: discord.Guild, rid: Optional[int]) -> Optional[discord.Role]:
    return guild.get_role(rid) if rid else None

def channel_from_id(guild: discord.Guild, cid: Optional[int]) -> Optional[discord.TextChannel]:
    if not cid:
        return None
    ch = guild.get_channel(cid)
    return ch if isinstance(ch, discord.TextChannel) else None

async def try_export_transcript(channel: discord.TextChannel, title: str) -> Tuple[str, discord.File]:
    """
    Try chat_exporter (HTML). Fallback: plaintext.
    Returns (filename, discord.File)
    """
    try:
        import chat_exporter  # type: ignore
        html = await chat_exporter.export(channel=channel, limit=None, tz_info="UTC")
        if html:
            return f"{title}.html", discord.File(io.BytesIO(html.encode("utf-8")), filename=f"{title}.html")
    except Exception:
        pass

    # Fallback plain text
    buf = io.StringIO()
    buf.write(f"Transcript for #{channel.name} ({channel.id})\n")
    buf.write(f"Guild: {channel.guild.name} ({channel.guild.id})\n")
    buf.write(f"Exported at: {datetime.datetime.utcnow().isoformat()}Z\n\n")
    async for msg in channel.history(limit=None, oldest_first=True):
        # Skip system / non-default content for cleanliness
        if msg.type is not discord.MessageType.default:
            continue
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        buf.write(f"[{ts}] {msg.author} ({msg.author.id}): {msg.content or ''}\n")
        for a in msg.attachments:
            buf.write(f"  [Attachment] {a.filename} - {a.url}\n")
    data = buf.getvalue().encode("utf-8")
    return f"{title}.txt", discord.File(io.BytesIO(data), filename=f"{title}.txt")

def human(member: Optional[discord.Member]) -> str:
    return f"{member.mention}" if member else "N/A"

def rel_ts(dt: datetime.datetime) -> str:
    # <t:unix:R> => “2 hours ago”
    unix = int(dt.timestamp())
    return f"<t:{unix}:R>"

# ---------------- Views ----------------
class AppPanelView(discord.ui.View):
    def __init__(self, cog: "ApplicationTickets", panel_name: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.panel_name = panel_name  # remembered when user clicks Open

    @discord.ui.button(
        label="Open Application",
        emoji="🎫",  # white ticket stub
        style=discord.ButtonStyle.success,
        custom_id="apptickets:open"
    )
    async def open_app(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_open_ticket(interaction, panel_name=self.panel_name, origin_channel_id=interaction.channel.id if interaction.channel else None)

class TicketView(discord.ui.View):
    def __init__(self, cog: "ApplicationTickets", opener_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.opener_id = opener_id

    @discord.ui.button(label="Claim", emoji="🎟️", style=discord.ButtonStyle.secondary, custom_id="apptickets:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_claim(interaction)

    @discord.ui.button(label="Submit", emoji="📨", style=discord.ButtonStyle.primary, custom_id="apptickets:submit")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_submit(interaction, self.opener_id)

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="apptickets:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_close(interaction)

    @discord.ui.button(label="Reopen", emoji="🔓", style=discord.ButtonStyle.success, custom_id="apptickets:reopen")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_reopen(interaction)

    @discord.ui.button(label="Approve", emoji="✅", style=discord.ButtonStyle.success, custom_id="apptickets:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_approve(interaction, self.opener_id)

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="apptickets:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_delete(interaction, self.opener_id)

# ---------------- Cog ----------------
class ApplicationTickets(commands.Cog):
    """Application ticket system with configurable template and workflow."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Persistent panel view will be created when /app post-panel runs.

    # ---------- Internal helpers ----------
    def _cfg(self, guild: discord.Guild) -> Dict[str, Any]:
        return get_guild_cfg(guild)

    def _logs_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        return channel_from_id(guild, self._cfg(guild).get("logs_channel_id"))

    def _staff_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        return role_from_id(guild, self._cfg(guild).get("staff_role_id"))

    def _admin_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        return role_from_id(guild, self._cfg(guild).get("admin_role_id"))

    def _approve_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        return role_from_id(guild, self._cfg(guild).get("approve_role_id"))

    def _category(self, guild: discord.Guild) -> Optional[discord.CategoryChannel]:
        cid = self._cfg(guild).get("category_id")
        ch = guild.get_channel(cid) if cid else None
        return ch if isinstance(ch, discord.CategoryChannel) else None

    def _template_lines(self, guild: discord.Guild) -> List[str]:
        tmpl = self._cfg(guild).get("template") or "1) Why do you want to join?\n2) Relevant experience?\n3) Anything else?"
        return [line.strip() for line in tmpl.splitlines() if line.strip()]

    def _ticket_name(self, member: discord.Member) -> str:
        base = f"app-{member.name}".lower().replace(" ", "-")
        return base[:90]

    async def _ensure_category(self, guild: discord.Guild) -> discord.CategoryChannel:
        cat = self._category(guild)
        if cat:
            return cat
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        cat = await guild.create_category("Applications", overwrites=overwrites, reason="Application tickets category")
        update_guild_cfg(guild, {"category_id": cat.id})
        return cat

    async def _post_template_prompt(self, channel: discord.TextChannel, opener: discord.Member):
        lines = self._template_lines(channel.guild)
        qlist = "\n".join(f"**{i+1}.** {q}" for i, q in enumerate(lines))
        embed = discord.Embed(
            title="Application Ticket",
            description=(f"Hi {opener.mention}! Please answer the questions below.\n\n{qlist}\n\n"
                         "When finished, click **Submit**. Staff can **Claim**, **Close** or **Approve**; "
                         "Admins can **Delete & Log** when done."),
            color=discord.Color.blurple()
        )
        await channel.send(embed=embed, view=TicketView(self, opener.id))

    async def _parse_topic(self, channel: discord.TextChannel) -> Dict[str, str]:
        topic = channel.topic or ""
        data = {}
        for part in (topic.split("|") if topic else []):
            if ":" in part:
                k, v = part.split(":", 1)
                data[k] = v
        return data

    async def _write_topic(self, channel: discord.TextChannel, **updates):
        data = await self._parse_topic(channel)
        data.update(updates)
        # rebuild (always keep a small sentinel so we know it's one of ours)
        pieces = [f"{k}:{v}" for k, v in data.items()]
        await channel.edit(topic="|".join(pieces))

    # ---------- Button Handlers ----------
    async def handle_open_ticket(self, interaction: discord.Interaction, panel_name: str, origin_channel_id: Optional[int]):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        guild = interaction.guild
        cat = await self._ensure_category(guild)
        staff_role = self._staff_role(guild)
        admin_role = self._admin_role(guild)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)

        # increment per-guild ticket counter
        cfg = self._cfg(guild)
        counter = int(cfg.get("counter", 0)) + 1
        update_guild_cfg(guild, {"counter": counter})

        channel = await guild.create_text_channel(
            name=self._ticket_name(interaction.user),
            category=cat,
            overwrites=overwrites,
            reason=f"Application opened by {interaction.user} ({interaction.user.id})"
        )

        await channel.edit(topic="|".join([
            f"opener:{interaction.user.id}",
            "claimed_by:0",
            "closed:0",
            "approved_by:0",
            "submitted:0",
            f"ticket_no:{counter}",
            f"panel_name:{panel_name}",
            f"origin_channel:{origin_channel_id or 0}",
        ]))

        await interaction.response.send_message(f"Created {channel.mention} for your application.", ephemeral=True)
        await self._post_template_prompt(channel, interaction.user)

    def _can_close(self, member: discord.Member) -> bool:
        role = self._staff_role(member.guild)
        return bool(role and role in member.roles) or member.guild_permissions.manage_channels

    def _is_admin(self, member: discord.Member) -> bool:
        role = self._admin_role(member.guild)
        return bool(role and role in member.roles) or member.guild_permissions.administrator

    async def handle_claim(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return
        if not self._can_close(interaction.user):
            return await interaction.response.send_message("You don't have permission to claim this.", ephemeral=True)
        await self._write_topic(interaction.channel, claimed_by=str(interaction.user.id))
        await interaction.response.send_message(f"{interaction.user.mention} claimed this application.", ephemeral=False)

    async def handle_submit(self, interaction: discord.Interaction, opener_id: int):
        if not isinstance(interaction.channel, discord.TextChannel):
            return
        if interaction.user.id != opener_id:
            return await interaction.response.send_message("Only the applicant can submit.", ephemeral=True)
        await self._write_topic(interaction.channel, submitted="1")
        staff = self._staff_role(interaction.guild) if interaction.guild else None
        ping = staff.mention if staff else "@here"
        await interaction.response.send_message(f"Application submitted! {ping}", allowed_mentions=discord.AllowedMentions(roles=True, everyone=True))

    async def handle_close(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return
        if not self._can_close(interaction.user):
            return await interaction.response.send_message("You don't have permission to close this.", ephemeral=True)
        await self._write_topic(interaction.channel, closed="1")
        # lock applicant
        topic = await self._parse_topic(interaction.channel)
        opener = interaction.guild.get_member(int(topic.get("opener", "0") or "0"))
        if opener:
            await interaction.channel.set_permissions(opener, send_messages=False, reason="Application closed")
        await interaction.response.send_message("Ticket closed 🔒", ephemeral=False)

    async def handle_reopen(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return
        if not self._can_close(interaction.user):
            return await interaction.response.send_message("You don't have permission to reopen this.", ephemeral=True)
        await self._write_topic(interaction.channel, closed="0")
        topic = await self._parse_topic(interaction.channel)
        opener = interaction.guild.get_member(int(topic.get("opener", "0") or "0"))
        if opener:
            await interaction.channel.set_permissions(opener, send_messages=True, reason="Application reopened")
        await interaction.response.send_message("Ticket reopened 🔓", ephemeral=False)

    async def handle_approve(self, interaction: discord.Interaction, opener_id: int):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return
        if not self._can_close(interaction.user):
            return await interaction.response.send_message("You don't have permission to approve.", ephemeral=True)
        approve_role = self._approve_role(interaction.guild)
        if not approve_role:
            return await interaction.response.send_message("No approve role configured.", ephemeral=True)
        member = interaction.guild.get_member(opener_id)
        if not member:
            return await interaction.response.send_message("Applicant not found.", ephemeral=True)
        try:
            await member.add_roles(approve_role, reason=f"Approved by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("I don't have permission to assign that role.", ephemeral=True)
        await self._write_topic(interaction.channel, approved_by=str(interaction.user.id))
        await interaction.response.send_message(f"Approved ✅ — {member.mention} was given {approve_role.mention}.", ephemeral=False)

    async def handle_delete(self, interaction: discord.Interaction, opener_id: int):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return
        if not self._is_admin(interaction.user):
            return await interaction.response.send_message("Admins only.", ephemeral=True)

        channel = interaction.channel
        guild = interaction.guild
        logs = self._logs_channel(guild)
        if logs is None:
            return await interaction.response.send_message("No logs channel set. Use /app set-logs first.", ephemeral=True)

        topic = await self._parse_topic(channel)
        opener = guild.get_member(int(topic.get("opener", "0") or "0"))
        claimed_by = guild.get_member(int(topic.get("claimed_by", "0") or "0"))
        approved_by = guild.get_member(int(topic.get("approved_by", "0") or "0"))
        submitted = topic.get("submitted", "0") == "1"
        closed = topic.get("closed", "0") == "1"
        ticket_no = int(topic.get("ticket_no", "0") or "0")
        panel_name = topic.get("panel_name", "Applications")
        origin_channel = guild.get_channel(int(topic.get("origin_channel", "0") or "0"))

        # Count participants & messages (skip non-default/system)
        counts: Counter[int] = Counter()
        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.type is not discord.MessageType.default:
                continue
            counts[msg.author.id] += 1
        # Build participant lines
        parts: List[str] = []
        for uid, n in counts.most_common():
            member = guild.get_member(uid)
            if member:
                parts.append(f"{n} messages by {member.mention}")
        participants_value = "\n".join(parts) if parts else "No conversation"

        # Export transcript first
        safe_title = f"{guild.name.replace(' ', '_')}-{channel.name}-{channel.id}"
        fname, ffile = await try_export_transcript(channel, safe_title)

        # Compose the embed like the screenshot
        ticket_title = f"Ticket #{ticket_no:03d} in {panel_name}!"
        emb = discord.Embed(color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        emb.title = ticket_title
        # "Type" section: 'from Coaching in #need-a-coach'
        type_desc = f"from **{panel_name}** in {origin_channel.mention if isinstance(origin_channel, discord.TextChannel) else '#unknown'}"
        emb.add_field(name="Type", value=type_desc, inline=False)
        # Created by / Deleted by / Claimed by
        created_line = f"{human(opener)} {rel_ts(channel.created_at)}" if opener else "N/A"
        deleted_line = f"{human(interaction.user)} {rel_ts(discord.utils.utcnow())}"
        claimed_line = human(claimed_by) if claimed_by else "—"
        emb.add_field(name="Created by", value=created_line, inline=False)
        emb.add_field(name="Deleted by", value=deleted_line, inline=False)
        emb.add_field(name="Claimed by", value=claimed_line, inline=False)
        # Participants
        emb.add_field(name="Participants", value=participants_value, inline=False)

        # Status footer (optional)
        status_bits = []
        status_bits.append("Submitted" if submitted else "Not Submitted")
        status_bits.append("Closed" if closed else "Open")
        if approved_by:
            status_bits.append("Approved")
        emb.set_footer(text=", ".join(status_bits))

        await interaction.response.send_message("Archiving and deleting…", ephemeral=True)

        # Send a single log message that we'll edit to add the link button once we have the attachment URL
        log_msg = await logs.send(embed=emb, file=ffile)

        # Create a link button to the just-uploaded attachment
        transcript_url = log_msg.attachments[0].url if log_msg.attachments else None
        if transcript_url:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Transcript", url=transcript_url))
            try:
                await log_msg.edit(view=view)
            except Exception:
                pass

        # Finally, delete the ticket channel
        try:
            await channel.delete(reason=f"Deleted by {interaction.user}")
        except discord.Forbidden:
            await logs.send("I lacked permission to delete the channel after logging.")

    # ---------- Slash Commands ----------
    group = app_commands.Group(name="app", description="Application ticket setup & panel")

    @group.command(name="set-template", description="Set the application template (one question per line).")
    @app_commands.describe(template="Paste your template; one question per line.")
    async def set_template(self, interaction: discord.Interaction, template: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Manage Server required.", ephemeral=True)
        update_guild_cfg(interaction.guild, {"template": template})
        await interaction.response.send_message("Template updated.", ephemeral=True)

    @group.command(name="set-logs", description="Set the logs channel.")
    @app_commands.describe(channel="Channel to send logs to.")
    async def set_logs(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Manage Server required.", ephemeral=True)
        update_guild_cfg(interaction.guild, {"logs_channel_id": channel.id})
        await interaction.response.send_message(f"Logs channel set to {channel.mention}.", ephemeral=True)

    @group.command(name="set-roles", description="Set staff, admin, and approve roles.")
    @app_commands.describe(
        staff_role="Role allowed to claim/close/approve.",
        admin_role="Role allowed to delete & log tickets.",
        approve_role="Role automatically assigned on Approve."
    )
    async def set_roles(
        self,
        interaction: discord.Interaction,
        staff_role: Optional[discord.Role],
        admin_role: Optional[discord.Role],
        approve_role: Optional[discord.Role]
    ):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Manage Server required.", ephemeral=True)
        updates = {}
        if staff_role: updates["staff_role_id"] = staff_role.id
        if admin_role: updates["admin_role_id"] = admin_role.id
        if approve_role: updates["approve_role_id"] = approve_role.id
        if not updates:
            return await interaction.response.send_message("Provide at least one role.", ephemeral=True)
        update_guild_cfg(interaction.guild, updates)
        await interaction.response.send_message("Roles updated.", ephemeral=True)

    @group.command(name="set-category", description="Set the category for application channels.")
    @app_commands.describe(category="Category to create application channels in.")
    async def set_category(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Manage Server required.", ephemeral=True)
        update_guild_cfg(interaction.guild, {"category_id": category.id})
        await interaction.response.send_message(f"Category set to **{category.name}**.", ephemeral=True)

    @group.command(name="post-panel", description="Post the Application panel with the Open button.")
    @app_commands.describe(channel="Channel to post the panel in.", panel_name="Display name for this panel (e.g., Coaching)")
    async def post_panel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None, panel_name: Optional[str] = "Applications"):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Manage Server required.", ephemeral=True)
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("Pick a text channel.", ephemeral=True)

        # Add a persistent view that remembers the panel name
        view = AppPanelView(self, panel_name or "Applications")
        self.bot.add_view(view)  # persistent

        embed = discord.Embed(
            title=f"{panel_name or 'Applications'} Center",
            description=("Click **Open Application** to create a private channel with your application form.\n"
                         "Staff can **Claim**, then **Close** or **Approve**; Admins can **Delete & Log** when finished."),
            color=discord.Color.green()
        )
        await target.send(embed=embed, view=view)
        await interaction.response.send_message(f"Panel posted in {target.mention}.", ephemeral=True)

    @app_commands.command(name="app-sync", description="Force resync app commands (Admin)")
    async def app_sync(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        await self.bot.tree.sync(guild=interaction.guild)
        await self.bot.tree.sync()
        await interaction.response.send_message("Commands synced.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(ApplicationTickets(bot))