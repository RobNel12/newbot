# cogs/application_tickets.py
import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional, Dict, Any, List, Tuple
import asyncio, json, os, io, datetime
from collections import Counter

CONFIG_FILE = "app_ticket_config.json"

# ---------------- Persistence ----------------
def _load_cfg() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_FILE): return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_cfg(cfg: Dict[str, Any]) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def gkey(guild: discord.Guild) -> str: return str(guild.id)
def get_guild_cfg(guild: discord.Guild) -> Dict[str, Any]: return _load_cfg().get(gkey(guild), {})
def update_guild_cfg(guild: discord.Guild, updates: Dict[str, Any]):
    cfg = _load_cfg()
    g = cfg.get(gkey(guild), {})
    g.update({k:v for k,v in updates.items() if v is not None})
    cfg[gkey(guild)] = g
    _save_cfg(cfg)

# ---------------- Utilities ----------------
def role_from_id(guild: discord.Guild, rid: Optional[int]) -> Optional[discord.Role]:
    return guild.get_role(rid) if rid else None

def channel_from_id(guild: discord.Guild, cid: Optional[int]) -> Optional[discord.abc.GuildChannel]:
    return guild.get_channel(cid) if cid else None

async def try_export_transcript(channel: discord.TextChannel, title: str) -> Tuple[str, discord.File]:
    try:
        import chat_exporter  # type: ignore
        html = await chat_exporter.export(channel=channel, limit=None, tz_info="UTC")
        if html:
            return f"{title}.html", discord.File(io.BytesIO(html.encode("utf-8")), filename=f"{title}.html")
    except Exception:
        pass
    buf = io.StringIO()
    buf.write(f"Transcript for #{channel.name} ({channel.id})\n")
    buf.write(f"Guild: {channel.guild.name} ({channel.guild.id})\n")
    buf.write(f"Exported at: {datetime.datetime.utcnow().isoformat()}Z\n\n")
    async for msg in channel.history(limit=None, oldest_first=True):
        if msg.type is not discord.MessageType.default:  # skip system
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
    return f"<t:{int(dt.timestamp())}:R>"

def _has_any_role(member: discord.Member, role_ids: List[int]) -> bool:
    if not role_ids: return False
    have = {r.id for r in member.roles}
    return any(rid in have for rid in role_ids)

# ---------------- Panel / Ticket Views ----------------
class AppPanelView(discord.ui.View):
    def __init__(self, cog: "ApplicationTickets", panel_name: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.panel_name = panel_name

    @discord.ui.button(label="Open Application", emoji="🎫", style=discord.ButtonStyle.success, custom_id="apptickets:open")
    async def open_app(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_open_ticket(
            interaction,
            panel_name=self.panel_name,
            origin_channel_id=interaction.channel.id if interaction.channel else None
        )

class TicketView(discord.ui.View):
    def __init__(self, cog: "ApplicationTickets", opener_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.opener_id = opener_id

    @discord.ui.button(label="Claim",   emoji="🎟️", style=discord.ButtonStyle.secondary, custom_id="apptickets:claim")
    async def claim(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_claim(interaction)

    @discord.ui.button(label="Submit",  emoji="📨", style=discord.ButtonStyle.primary,   custom_id="apptickets:submit")
    async def submit(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_submit(interaction, self.opener_id)

    @discord.ui.button(label="Close",   emoji="🔒", style=discord.ButtonStyle.danger,    custom_id="apptickets:close")
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_close(interaction)

    @discord.ui.button(label="Reopen",  emoji="🔓", style=discord.ButtonStyle.success,   custom_id="apptickets:reopen")
    async def reopen(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_reopen(interaction)

    @discord.ui.button(label="Approve", emoji="✅", style=discord.ButtonStyle.success,   custom_id="apptickets:approve")
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_approve(interaction, self.opener_id)

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="apptickets:delete")
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_delete(interaction, self.opener_id)

# ---------------- Setup Wizard (multi-stage) ----------------
class WizardState:
    def __init__(self, cog: "ApplicationTickets", panel_name: str):
        self.cog = cog
        self.panel_name = panel_name
        self.category_id: Optional[int] = None
        self.panel_channel_id: Optional[int] = None
        self.support_role_ids: List[int] = []
        self.delete_role_ids: List[int] = []
        self.approve_role_id: Optional[int] = None
        self.logs_channel_id: Optional[int] = None

# Step 1: Basics
class SetupStep1(discord.ui.View):
    def __init__(self, state: WizardState):
        super().__init__(timeout=600)
        self.state = state
        self.add_item(_CatPick(state, row=0))
        self.add_item(_PanelChannelPick(state, row=1))
        self.add_item(_NextButton(state, target="roles", row=4))

    async def on_timeout(self):
        # Nothing special; ephemeral message will become inert
        return

# Step 2: Roles
class SetupStep2(discord.ui.View):
    def __init__(self, state: WizardState):
        super().__init__(timeout=600)
        self.state = state
        self.add_item(_SupportRolesPick(state, row=0))
        self.add_item(_DeleteRolesPick(state, row=1))
        self.add_item(_ApproveRolePick(state, row=2))
        self.add_item(_BackButton(state, row=4))
        self.add_item(_NextButton(state, target="logging", row=4))

# Step 3: Logging / Save
class SetupStep3(discord.ui.View):
    def __init__(self, state: WizardState):
        super().__init__(timeout=600)
        self.state = state
        self.add_item(_LogsChannelPick(state, row=0))
        self.add_item(_BackButton(state, row=4))
        self.add_item(_SaveButton(state, row=4))

# ---- Selects & Buttons (shared) ----
class _CatPick(discord.ui.ChannelSelect):
    def __init__(self, state: WizardState, row: int = 0):
        super().__init__(placeholder="Select category", channel_types=[discord.ChannelType.category], min_values=0, max_values=1, row=row)
        self.state = state
    async def callback(self, interaction: discord.Interaction):
        self.state.category_id = self.values[0].id if self.values else None
        await interaction.response.defer()

class _PanelChannelPick(discord.ui.ChannelSelect):
    def __init__(self, state: WizardState, row: int = 0):
        super().__init__(placeholder="Select panel channel (optional)", channel_types=[discord.ChannelType.text], min_values=0, max_values=1, row=row)
        self.state = state
    async def callback(self, interaction: discord.Interaction):
        self.state.panel_channel_id = self.values[0].id if self.values else None
        await interaction.response.defer()

class _SupportRolesPick(discord.ui.RoleSelect):
    def __init__(self, state: WizardState, row: int = 0):
        super().__init__(placeholder="Select support roles", min_values=0, max_values=5, row=row)
        self.state = state
    async def callback(self, interaction: discord.Interaction):
        self.state.support_role_ids = [r.id for r in self.values]
        await interaction.response.defer()

class _DeleteRolesPick(discord.ui.RoleSelect):
    def __init__(self, state: WizardState, row: int = 0):
        super().__init__(placeholder="Select delete roles", min_values=0, max_values=5, row=row)
        self.state = state
    async def callback(self, interaction: discord.Interaction):
        self.state.delete_role_ids = [r.id for r in self.values]
        await interaction.response.defer()

class _ApproveRolePick(discord.ui.RoleSelect):
    def __init__(self, state: WizardState, row: int = 0):
        super().__init__(placeholder="Select approve role (single)", min_values=0, max_values=1, row=row)
        self.state = state
    async def callback(self, interaction: discord.Interaction):
        self.state.approve_role_id = self.values[0].id if self.values else None
        await interaction.response.defer()

class _LogsChannelPick(discord.ui.ChannelSelect):
    def __init__(self, state: WizardState, row: int = 0):
        super().__init__(placeholder="Select log channel", channel_types=[discord.ChannelType.text], min_values=0, max_values=1, row=row)
        self.state = state
    async def callback(self, interaction: discord.Interaction):
        self.state.logs_channel_id = self.values[0].id if self.values else None
        await interaction.response.defer()

class _BackButton(discord.ui.Button):
    def __init__(self, state: WizardState, row: int = 4):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary, row=row)
        self.state = state
    async def callback(self, interaction: discord.Interaction):
        # Determine previous step by current view class
        if isinstance(interaction.message.components[0].children[0], discord.ui.SelectMenu):  # heuristic, but OK
            pass
        # Switch based on current view class
        if isinstance(self.view, SetupStep2):
            await interaction.response.edit_message(content=f"Configuring **{self.state.panel_name}** — Basics", view=SetupStep1(self.state))
        elif isinstance(self.view, SetupStep3):
            await interaction.response.edit_message(content=f"Configuring **{self.state.panel_name}** — Roles", view=SetupStep2(self.state))

class _NextButton(discord.ui.Button):
    def __init__(self, state: WizardState, target: str, row: int = 4):
        super().__init__(label="Next", style=discord.ButtonStyle.primary, row=row)
        self.state = state
        self.target = target
    async def callback(self, interaction: discord.Interaction):
        if self.target == "roles":
            await interaction.response.edit_message(content=f"Configuring **{self.state.panel_name}** — Roles", view=SetupStep2(self.state))
        elif self.target == "logging":
            await interaction.response.edit_message(content=f"Configuring **{self.state.panel_name}** — Logging", view=SetupStep3(self.state))

class _SaveButton(discord.ui.Button):
    def __init__(self, state: WizardState, row: int = 4):
        super().__init__(label="Save Panel", style=discord.ButtonStyle.success, emoji="✅", row=row)
        self.state = state
    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use in a server.", ephemeral=True)
        updates = {
            "category_id": self.state.category_id,
            "staff_role_ids": self.state.support_role_ids,
            "admin_role_ids": self.state.delete_role_ids,
            "approve_role_id": self.state.approve_role_id,
            "logs_channel_id": self.state.logs_channel_id,
            "panel_name": self.state.panel_name,
        }
        update_guild_cfg(interaction.guild, updates)

        target = interaction.guild.get_channel(self.state.panel_channel_id) if self.state.panel_channel_id else interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.edit_message(content="Pick a text channel to post the panel.", view=None)

        panel_view = AppPanelView(self.state.cog, self.state.panel_name)
        self.state.cog.bot.add_view(panel_view)  # persistent

        embed = discord.Embed(
            title=f"{self.state.panel_name} Center",
            description=("Click **Open Application** to create a private channel with your application form.\n"
                         "Staff can **Claim**, then **Close** or **Approve**; Admins can **Delete & Log** when finished."),
            color=discord.Color.green()
        )
        await target.send(embed=embed, view=panel_view)
        await interaction.response.edit_message(content=f"✅ Saved **{self.state.panel_name}** and posted the panel in {target.mention}.", view=None)

# ---------------- Cog ----------------
class ApplicationTickets(commands.Cog):
    """Application tickets with setup wizard, configurable template, and workflow."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- Config helpers ----------
    def _cfg(self, guild: discord.Guild) -> Dict[str, Any]: return get_guild_cfg(guild)
    def _logs_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        ch = channel_from_id(guild, self._cfg(guild).get("logs_channel_id")); return ch if isinstance(ch, discord.TextChannel) else None
    def _staff_role_ids(self, guild: discord.Guild) -> List[int]: return list(self._cfg(guild).get("staff_role_ids", []))
    def _admin_role_ids(self, guild: discord.Guild) -> List[int]: return list(self._cfg(guild).get("admin_role_ids", []))
    def _approve_role(self, guild: discord.Guild) -> Optional[discord.Role]: return role_from_id(guild, self._cfg(guild).get("approve_role_id"))
    def _category(self, guild: discord.Guild) -> Optional[discord.CategoryChannel]:
        cid = self._cfg(guild).get("category_id"); ch = guild.get_channel(cid) if cid else None
        return ch if isinstance(ch, discord.CategoryChannel) else None
    def _panel_name(self, guild: discord.Guild) -> str: return self._cfg(guild).get("panel_name", "Applications")
    def _template_lines(self, guild: discord.Guild) -> List[str]:
        tmpl = self._cfg(guild).get("template") or "1) Why do you want to join?\n2) Relevant experience?\n3) Anything else?"
        return [line.strip() for line in tmpl.splitlines() if line.strip()]

    # ---------- Ticket creation ----------
    def _ticket_name(self, member: discord.Member) -> str:
        base = f"app-{member.name}".lower().replace(" ", "-")
        return base[:90]

    async def _ensure_category(self, guild: discord.Guild) -> discord.CategoryChannel:
        cat = self._category(guild)
        if cat: return cat
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        cat = await guild.create_category("Applications", overwrites=overwrites, reason="Application tickets category")
        update_guild_cfg(guild, {"category_id": cat.id})
        return cat

    async def _post_template_prompt(self, channel: discord.TextChannel, opener: discord.Member):
        qlist = "\n".join(f"**{i+1}.** {q}" for i, q in enumerate(self._template_lines(channel.guild)))
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
        data: Dict[str, str] = {}
        for part in (topic.split("|") if topic else []):
            if ":" in part:
                k, v = part.split(":", 1); data[k] = v
        return data

    async def _write_topic(self, channel: discord.TextChannel, **updates):
        data = await self._parse_topic(channel); data.update(updates)
        await channel.edit(topic="|".join(f"{k}:{v}" for k, v in data.items()))

    # ---------- Button handlers ----------
    async def handle_open_ticket(self, interaction: discord.Interaction, panel_name: str, origin_channel_id: Optional[int]):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        guild = interaction.guild
        cat = await self._ensure_category(guild)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        for rid in self._staff_role_ids(guild):
            r = guild.get_role(rid)
            if r: overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        for rid in self._admin_role_ids(guild):
            r = guild.get_role(rid)
            if r: overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)

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
            "claimed_by:0","closed:0","approved_by:0","submitted:0",
            f"ticket_no:{counter}",
            f"panel_name:{panel_name}",
            f"origin_channel:{origin_channel_id or 0}",
        ]))

        await interaction.response.send_message(f"Created {channel.mention} for your application.", ephemeral=True)
        await self._post_template_prompt(channel, interaction.user)

    def _can_close(self, member: discord.Member) -> bool:
        return _has_any_role(member, self._staff_role_ids(member.guild)) or member.guild_permissions.manage_channels
    def _is_admin(self, member: discord.Member) -> bool:
        return _has_any_role(member, self._admin_role_ids(member.guild)) or member.guild_permissions.administrator

    async def handle_claim(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel): return
        if not self._can_close(interaction.user):
            return await interaction.response.send_message("You don't have permission to claim this.", ephemeral=True)
        await self._write_topic(interaction.channel, claimed_by=str(interaction.user.id))
        await interaction.response.send_message(f"{interaction.user.mention} claimed this application.", ephemeral=False)

    async def handle_submit(self, interaction: discord.Interaction, opener_id: int):
        if not isinstance(interaction.channel, discord.TextChannel):
            return
    
        # Only the opener can submit
        if interaction.user.id != opener_id:
            return await interaction.response.send_message("Only the applicant can submit.", ephemeral=True)
    
        # Mark submitted
        await self._write_topic(interaction.channel, submitted="1")
    
        # Look up the approve role
        approve_role = self._approve_role(interaction.guild) if interaction.guild else None
        if not approve_role:
            return await interaction.response.send_message("No approve role configured.", ephemeral=True)
    
        # Notify the approvers (single message, safe allowed mentions)
        allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)
        await interaction.channel.send(f"Application submitted! {approve_role.mention}", allowed_mentions=allowed)
    
        # Confirmation to the applicant
        await interaction.response.send_message("Submitted! We’ve notified the approvers.", ephemeral=True)
    
    async def handle_close(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel): return
        if not self._can_close(interaction.user):
            return await interaction.response.send_message("You don't have permission to close this.", ephemeral=True)
        await self._write_topic(interaction.channel, closed="1")
        topic = await self._parse_topic(interaction.channel)
        opener = interaction.guild.get_member(int(topic.get("opener", "0") or "0"))
        if opener:
            await interaction.channel.set_permissions(opener, send_messages=False, reason="Application closed")
        await interaction.response.send_message("Ticket closed 🔒", ephemeral=False)

    async def handle_reopen(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel): return
        if not self._can_close(interaction.user):
            return await interaction.response.send_message("You don't have permission to reopen this.", ephemeral=True)
        await self._write_topic(interaction.channel, closed="0")
        topic = await self._parse_topic(interaction.channel)
        opener = interaction.guild.get_member(int(topic.get("opener", "0") or "0"))
        if opener:
            await interaction.channel.set_permissions(opener, send_messages=True, reason="Application reopened")
        await interaction.response.send_message("Ticket reopened 🔓", ephemeral=False)

    async def handle_approve(self, interaction: discord.Interaction, opener_id: int):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel): return
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
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel): return
        if not self._is_admin(interaction.user):
            return await interaction.response.send_message("Admins only.", ephemeral=True)

        channel = interaction.channel
        guild = interaction.guild
        logs = self._logs_channel(guild)
        if logs is None:
            return await interaction.response.send_message("No logs channel set. Use /app setup.", ephemeral=True)

        topic = await self._parse_topic(channel)
        opener = guild.get_member(int(topic.get("opener", "0") or "0"))
        claimed_by = guild.get_member(int(topic.get("claimed_by", "0") or "0"))
        approved_by = guild.get_member(int(topic.get("approved_by", "0") or "0"))
        submitted = topic.get("submitted", "0") == "1"
        closed = topic.get("closed", "0") == "1"
        ticket_no = int(topic.get("ticket_no", "0") or "0")
        panel_name = topic.get("panel_name", "Applications")
        origin_channel = guild.get_channel(int(topic.get("origin_channel", "0") or "0"))

        # participants
        counts: Counter[int] = Counter()
        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.type is not discord.MessageType.default: continue
            counts[msg.author.id] += 1
        parts = []
        for uid, n in counts.most_common():
            m = guild.get_member(uid)
            if m: parts.append(f"{n} messages by {m.mention}")
        participants_value = "\n".join(parts) if parts else "No conversation"

        # transcript
        safe_title = f"{guild.name.replace(' ', '_')}-{channel.name}-{channel.id}"
        _, ffile = await try_export_transcript(channel, safe_title)

        # embed like your card
        emb = discord.Embed(color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        emb.title = f"Ticket #{ticket_no:03d} in {panel_name}!"
        emb.add_field(name="Type", value=f"from **{panel_name}** in {origin_channel.mention if isinstance(origin_channel, discord.TextChannel) else '#unknown'}", inline=False)
        emb.add_field(name="Created by", value=(f"{human(opener)} {rel_ts(channel.created_at)}" if opener else "N/A"), inline=False)
        emb.add_field(name="Deleted by", value=f"{human(interaction.user)} {rel_ts(discord.utils.utcnow())}", inline=False)
        emb.add_field(name="Claimed by", value=(human(claimed_by) if claimed_by else "—"), inline=False)
        emb.add_field(name="Participants", value=participants_value, inline=False)
        status_bits = [("Submitted" if submitted else "Not Submitted"), ("Closed" if closed else "Open")]
        if approved_by: status_bits.append("Approved")
        emb.set_footer(text=", ".join(status_bits))

        await interaction.response.send_message("Archiving and deleting…", ephemeral=True)
        log_msg = await logs.send(embed=emb, file=ffile)

        if log_msg.attachments:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Transcript", url=log_msg.attachments[0].url))
            try: await log_msg.edit(view=view)
            except Exception: pass

        try:
            await channel.delete(reason=f"Deleted by {interaction.user}")
        except discord.Forbidden:
            await logs.send("I lacked permission to delete the channel after logging.")

    # ---------- Slash commands ----------
    group = app_commands.Group(name="app", description="Application ticket setup & panel")

    @group.command(name="setup", description="Open the application panel setup wizard.")
    @app_commands.describe(panel_name="Display name (e.g., 'Coaching')", template="Application questions, one per line (optional)")
    async def setup(self, interaction: discord.Interaction, panel_name: str, template: Optional[str] = None):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Manage Server required.", ephemeral=True)
        if template:
            update_guild_cfg(interaction.guild, {"template": template})
        state = WizardState(self, panel_name)
        await interaction.response.send_message(
            f"Configuring **{panel_name}** — Basics",
            view=SetupStep1(state),
            ephemeral=True
        )

    @app_commands.command(name="app-sync", description="Force resync app commands (Admin)")
    async def app_sync(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        await self.bot.tree.sync(guild=interaction.guild)
        await self.bot.tree.sync()
        await interaction.response.send_message("Commands synced.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(ApplicationTickets(bot))