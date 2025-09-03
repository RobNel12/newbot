# ticketing.py (general-purpose)
import discord, json, os, asyncio, datetime, io, time
from discord.ext import commands
from discord import app_commands
from typing import List, Optional, Dict
import chat_exporter

CONFIG_FILE = "ticket_config.json"

# Neutral default artwork (server owners can override with /ticket_image_set and /ticket_thumb_set)
DEFAULT_TICKET_THUMB_URL  = "https://example.com/ticket-thumb.png"
DEFAULT_TICKET_BANNER_URL = ""

# Who can always delete tickets (owner override). Leave empty or add your Discord user IDs.
OWNER_IDS = set()

# ---------------- Persistence ----------------
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

# ---------------- Helpers ----------------
def slugify(name: str, max_len: int = 90) -> str:
    name = name.lower()
    cleaned = []
    last_sep = False
    for ch in name:
        if ch.isalnum() or ch in "_-":
            cleaned.append(ch)
            last_sep = False
        else:
            if not last_sep:
                cleaned.append("-")
            last_sep = True
    slug = "".join(cleaned).strip("-_")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-_")
    return slug or "user"

# ---------------- Setup UI ----------------
class TicketSetupView(discord.ui.View):
    def __init__(self, cog, guild: discord.Guild, panel_name: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.panel_name = panel_name

        self.category: Optional[int] = None
        self.view_roles: List[int] = []
        self.delete_roles: List[int] = []
        self.log_channel: Optional[int] = None
        
        self.add_item(CategorySelect(self))
        self.add_item(ViewRolesSelect(self))
        self.add_item(DeleteRolesSelect(self))
        self.add_item(LogChannelSelect(self))
        

    @discord.ui.button(label="✅ Save Panel", style=discord.ButtonStyle.green)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.category or not self.view_roles or not self.log_channel:
            return await interaction.response.send_message(
                "❌ You must select a category, at least one staff role, and a log channel.",
                ephemeral=True,
            )

        gid = str(self.guild.id)
        gdata = self.cog.config.setdefault(gid, {})
        panels = gdata.setdefault("panels", {})
        panels[self.panel_name] = {
            "category": self.category,
            "view_roles": self.view_roles,
            "delete_roles": self.delete_roles,
            "log_channel": self.log_channel,
        }
        
        save_config(self.cog.config)

        embed = discord.Embed(
            title=f"{self.panel_name.title()}",
            description=(
                "Click the button below to open a ticket.\n\n"
                "A private channel will be created that only you and server staff can see. "
                "When you’re done, staff can close and archive the ticket with a transcript."
            ),
            color=0xEFA56D
        )
        # Neutral banner (admins can override later with /ticket_image_set)
        embed.set_image(url=DEFAULT_TICKET_BANNER_URL)

        view = TicketPanelView(self.cog, self.guild.id, self.panel_name)
        sent = await interaction.channel.send(embed=embed, view=view)

        panels[self.panel_name]["message_id"] = sent.id
        panels[self.panel_name]["channel_id"] = interaction.channel.id
        save_config(self.cog.config)
        
        await interaction.response.send_message(
            f"✅ Panel `{self.panel_name}` configured and posted in {interaction.channel.mention}",
            ephemeral=True
        )
        self.stop()


class CategorySelect(discord.ui.ChannelSelect):
    def __init__(self, view: "TicketSetupView"):
        super().__init__(placeholder="Select category", channel_types=[discord.ChannelType.category], min_values=1, max_values=1)
        self.view_ref = view
    async def callback(self, interaction: discord.Interaction):
        self.view_ref.category = self.values[0].id
        await interaction.response.defer()

class ViewRolesSelect(discord.ui.RoleSelect):
    def __init__(self, view: "TicketSetupView"):
        super().__init__(placeholder="Select staff roles (can view/participate)", min_values=1, max_values=5)
        self.view_ref = view
    async def callback(self, interaction: discord.Interaction):
        self.view_ref.view_roles = [r.id for r in self.values]
        await interaction.response.defer()

class DeleteRolesSelect(discord.ui.RoleSelect):
    def __init__(self, view: "TicketSetupView"):
        super().__init__(placeholder="Select delete roles (optional)", min_values=0, max_values=5)
        self.view_ref = view
    async def callback(self, interaction: discord.Interaction):
        self.view_ref.delete_roles = [r.id for r in self.values]
        await interaction.response.defer()

class LogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, view: "TicketSetupView"):
        super().__init__(placeholder="Select log channel", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
        self.view_ref = view
    async def callback(self, interaction: discord.Interaction):
        self.view_ref.log_channel = self.values[0].id
        await interaction.response.defer()

class ClaimRoleSelect(discord.ui.RoleSelect):  # kept for parity, not used in the setup flow by default
    def __init__(self, view: "TicketSetupView"):
        super().__init__(placeholder="Select claiming role (optional)", min_values=0, max_values=1)
        self.view_ref = view
    async def callback(self, interaction: discord.Interaction):
        self.view_ref.claim_role_id = self.values[0].id if self.values else None
        await interaction.response.defer()

# ---------------- Ticket Panel ----------------
class TicketPanelView(discord.ui.View):
    def __init__(self, cog, guild_id: int, panel_name: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = str(guild_id)
        self.panel_name = panel_name

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.green, emoji="🎫", custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.config.get(self.guild_id, {}).get("panels", {}).get(self.panel_name)
        if not cfg:
            return await interaction.response.send_message("⚠️ Panel not configured anymore.", ephemeral=True)

        guild = interaction.guild
        category = guild.get_channel(cfg["category"])
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("⚠️ Category missing.", ephemeral=True)

        guild_cfg = self.cog.config.setdefault(self.guild_id, {})
        counter = guild_cfg.setdefault("ticket_counter", 1)
        ticket_number = counter
        guild_cfg["ticket_counter"] = counter + 1
        save_config(self.cog.config)

        opener_slug = slugify(interaction.user.name)
        chan_name = f"{ticket_number:03d}-{opener_slug}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        for rid in cfg["view_roles"]:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        channel = await guild.create_text_channel(chan_name, category=category, overwrites=overwrites)

        # Save meta for later (rename on claim; transcript details)
        self.cog.channel_meta[str(channel.id)] = {
            "ticket_number": ticket_number,
            "panel_name": self.panel_name,
            "panel_channel_id": interaction.channel.id,  # where the panel lives
            "opener_id": interaction.user.id,
            "opener_slug": opener_slug,
            "opened_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        save_config(self.cog.config)

        log_channel = guild.get_channel(cfg["log_channel"])

        # Build the embed (general-purpose)
        embed = discord.Embed(
            description=(
                "📨 A new ticket has been opened.\n\n"
                f"**Hello {interaction.user.display_name}!**\n\n"
                "Staff will be with you shortly. Please describe your request clearly. "
                "When resolved, a staff member can close and archive the ticket."
            ),
            color=0xEFA56D,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=DEFAULT_TICKET_THUMB_URL)
        embed.set_image(url=DEFAULT_TICKET_BANNER_URL)
        
        # Send the welcome embed with controls
        msg = await channel.send(
            embed=embed,
            view=TicketChannelView(
                opener_id=interaction.user.id,
                cog=self.cog,
                log_channel=log_channel,
                log_msg=None,
                channel_id=channel.id,
            ),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
        )
        
        # Pin it if possible
        try:
            await msg.pin(reason="Pin initial ticket instructions")
        except (discord.Forbidden, discord.HTTPException):
            pass
        
        # Confirm ticket creation to the user
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)


# ---------------- Feedback Modal ----------------
class FeedbackModal(discord.ui.Modal, title="Send feedback to the opener"):
    def __init__(self, cog: "TicketCog", opener_id: int, claimer_id: int, channel: discord.TextChannel):
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = opener_id
        self.claimer_id = claimer_id
        self.channel = channel

        self.feedback = discord.ui.TextInput(
            label="Your message",
            placeholder="Type your message to the ticket opener…",
            style=discord.TextStyle.paragraph,
            min_length=5,
            max_length=2000,
            required=True,
        )
        self.add_item(self.feedback)

    async def on_submit(self, interaction: discord.Interaction):
        # Pull ticket meta
        meta = self.cog.channel_meta.get(str(self.channel.id), {})
        ticket_no = meta.get("ticket_number", 0)
        panel_name = meta.get("panel_name", "?")

        opener = interaction.guild.get_member(self.opener_id)
        claimer = interaction.guild.get_member(self.claimer_id) or interaction.user

        embed = discord.Embed(
            title="New message from the ticket handler",
            description=self.feedback.value,
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Ticket", value=f"#{ticket_no:03d} ({panel_name})", inline=True)
        embed.add_field(name="From", value=claimer.mention if claimer else f"<@{self.claimer_id}>", inline=True)
        embed.add_field(name="Channel", value=self.channel.mention, inline=False)

        dm_ok = False
        if opener:
            try:
                await opener.send(embed=embed)
                dm_ok = True
            except Exception:
                dm_ok = False

        # Mirror to logs channel (if configured)
        gconf = self.cog.config.get(str(interaction.guild.id), {})
        panel_cfg = gconf.get("panels", {}).get(panel_name, {}) if panel_name else {}
        logs = interaction.guild.get_channel(panel_cfg.get("log_channel") or 0)
        if logs:
            try:
                await logs.send(embed=embed)
            except Exception:
                pass

        # Persist the "used" flag and disable the button
        meta["claimer_feedback_sent"] = True
        self.cog.channel_meta[str(self.channel.id)] = meta
        save_config(self.cog.config)

        try:
            if interaction.message:
                for child in interaction.view.children:
                    if isinstance(child, discord.ui.Button) and child.custom_id == "ticket:feedback":
                        child.disabled = True
                await interaction.message.edit(view=interaction.view)
        except Exception:
            pass

        note = "✉️ Sent as a DM to the opener." if dm_ok else "⚠️ Could not DM the opener (DMs closed). Logged to the logs channel."
        await interaction.response.send_message(f"✅ Feedback recorded. {note}", ephemeral=True)

# ---------------- Ticket Channel Controls ----------------
class TicketChannelView(discord.ui.View):
    def __init__(self, opener_id: int, cog: "TicketCog", log_channel: Optional[discord.TextChannel], log_msg: Optional[discord.Message], channel_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id
        self.cog = cog
        self.log_channel = log_channel
        self.log_msg = log_msg
        self.claimer_id: Optional[int] = None
        self.closed: bool = False
        self.channel_id = channel_id

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, emoji="🧰", custom_id="ticket:claim", row=0)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.cog.config.get(str(interaction.guild.id), {})
        roster = g.get("roster", {})
        if str(interaction.user.id) not in roster:
            return await interaction.response.send_message("⚠️ You are not in the ticket staff roster and cannot claim.", ephemeral=True)

        self.claimer_id = interaction.user.id

        # Rename channel to 000-opener-claimer
        meta = self.cog.channel_meta.get(str(interaction.channel.id), {})
        opener_slug = meta.get("opener_slug", "user")
        claimer_slug = slugify(interaction.user.display_name or interaction.user.name)
        ticket_no = meta.get("ticket_number", 0)
        new_name = f"{ticket_no:03d}-{opener_slug}-{claimer_slug}"
        try:
            if interaction.channel.name != new_name:
                await interaction.channel.edit(name=new_name)
        except discord.HTTPException:
            pass

        # Persist claimer info
        meta["claimer_id"] = self.claimer_id
        meta["claimer_slug"] = claimer_slug
        self.cog.channel_meta[str(interaction.channel.id)] = meta
        save_config(self.cog.config)

        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, emoji="🔒", custom_id="ticket:close", row=0)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.closed:
            return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
        await self._lock_channel(interaction.channel, lock=True)
        self.closed = True
        await interaction.response.send_message("🔒 Ticket closed. Use **Reopen** to unlock or **Delete** to archive.", ephemeral=False)

        opener = interaction.guild.get_member(self.opener_id)
        opener_display = opener.mention if opener else f"<@{self.opener_id}>"
        
        claimer_member = interaction.guild.get_member(self.claimer_id) or interaction.user
        claimer_display = claimer_member.mention
        
        await interaction.channel.send(
            f"{opener_display}, please leave feedback for {claimer_display}:",
            view=ReviewView(
                self.cog,
                self.log_channel,
                opener_id=self.opener_id,
                staff_id=claimer_member.id,
                log_msg=self.log_msg
            )
        )

    @discord.ui.button(label="Reopen", style=discord.ButtonStyle.success, emoji="🔓", custom_id="ticket:reopen", row=0)
    async def reopen_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.closed:
            return await interaction.response.send_message("This ticket is not closed.", ephemeral=True)
        await self._lock_channel(interaction.channel, lock=False)
        self.closed = False
        await interaction.response.send_message("🔓 Ticket reopened.", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="ticket:delete", row=0)
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.config.get(str(interaction.guild.id), {}).get("panels", {}).get(
            self.cog.channel_meta.get(str(self.channel_id), {}).get("panel_name", ""), {}
        )

        # owner override
        is_owner_override = interaction.user.id in OWNER_IDS

        # admins always allowed
        allowed = interaction.user.guild_permissions.administrator or is_owner_override

        # delete-roles allowed
        if not allowed:
            for rid in cfg.get("delete_roles", []):
                role = interaction.guild.get_role(rid)
                if role and role in interaction.user.roles:
                    allowed = True
                    break

        if not allowed:
            return await interaction.response.send_message("You don't have permission to delete this ticket.", ephemeral=True)

        await interaction.response.send_message("Archiving and deleting ticket…", ephemeral=True)
        await asyncio.sleep(1)
        await self._log_and_delete(interaction.channel, interaction.user)


    async def _lock_channel(self, channel: discord.TextChannel, lock: bool):
        overwrites = channel.overwrites
        for target, perms in list(overwrites.items()):
            if isinstance(target, (discord.Role, discord.Member)):
                if perms.send_messages is not None:
                    perms.send_messages = not lock
                    overwrites[target] = perms
        await channel.edit(overwrites=overwrites)

    async def _log_and_delete(self, channel: discord.TextChannel, deleted_by: discord.Member):
        # Count human participants (skip obvious bot/system prompts)
        counts: Dict[int, int] = {}
        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.author.bot:
                lc = (msg.content or "").lower()
                if any(s in lc for s in ["opened a ticket", "leave feedback", "ticket closed", "archiving"]):
                    continue
            counts[msg.author.id] = counts.get(msg.author.id, 0) + 1

        # Resolve the logs channel directly from panel config
        meta = self.cog.channel_meta.get(str(channel.id), {})
        panel_name = meta.get("panel_name")
        gconf = self.cog.config.get(str(channel.guild.id), {})
        panel_cfg = gconf.get("panels", {}).get(panel_name, {}) if panel_name else {}
        logs_id = panel_cfg.get("log_channel")
        logs = channel.guild.get_channel(logs_id) if logs_id else None

        # If we can't find a logs channel, just delete and bail
        if not logs:
            await channel.delete()
            return

        # Export transcript HTML (DO NOT send to current channel)
        transcript_html = await chat_exporter.export(
            channel,
            limit=None,
            bot=self.cog.bot,
        )
        if not transcript_html:
            transcript_html = "<html><body><p>No transcript available.</p></body></html>"

        # Build filename like transcript-000-opener[-claimer].html
        ticket_no = meta.get("ticket_number", 0)
        fname = f"transcript-{ticket_no:03d}-{channel.name.split('-', 1)[-1]}.html"
        transcript_file = discord.File(io.BytesIO(transcript_html.encode("utf-8")), filename=fname)

        # Prepare members and times
        opener = channel.guild.get_member(meta.get("opener_id", 0))
        opener_display = opener.mention if opener else f"<@{meta.get('opener_id')}>"

        claimer_id = meta.get("claimer_id")
        if claimer_id:
            m = channel.guild.get_member(claimer_id)
            claimers_display = m.mention if m else f"<@{claimer_id}>"
        else:
            claimers_display = "None"

        closer_display = deleted_by.mention if deleted_by else "Unknown"

        # Opened/deleted relative times
        opened_at = meta.get("opened_at")
        try:
            opened_dt = datetime.datetime.fromisoformat(opened_at)
        except Exception:
            opened_dt = None
        created_rel = discord.utils.format_dt(opened_dt, "R") if opened_dt else "some time ago"
        deleted_rel = discord.utils.format_dt(discord.utils.utcnow(), "R")

        # Panel message channel mention for "Type"
        panel_chan = channel.guild.get_channel(meta.get("panel_channel_id", 0))
        panel_where = panel_chan.mention if panel_chan else "#unknown"

        # Upload the transcript file to LOGS channel
        sent = await logs.send(file=transcript_file)
        transcript_url = sent.attachments[0].url if sent.attachments else None

        # Build embed (general-purpose)
        embed = discord.Embed(
            title=f"Ticket #{ticket_no:03d}{' in ' + panel_name.title() if panel_name else ''}",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Panel", value=f"{panel_name.title() if panel_name else '?'} in {panel_where}", inline=False)
        embed.add_field(name="Created by", value=f"{opener_display} {created_rel}", inline=True)
        embed.add_field(name="Deleted by", value=f"{closer_display} {deleted_rel}", inline=True)
        embed.add_field(name="Claimed by", value=claimers_display, inline=False)

        if counts:
            lines = []
            for uid, c in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]:
                mem = channel.guild.get_member(uid)
                name = mem.mention if mem else f"<@{uid}>"
                lines.append(f"{c} messages by {name}")
            embed.add_field(name="Participants", value="\n".join(lines), inline=False)

        view = None
        if transcript_url:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Transcript", url=transcript_url))

        # Send the embed (no duplicate file) to the logs channel
        await logs.send(embed=embed, view=view)

        # Finally delete the ticket channel itself
        await channel.delete()

    @discord.ui.button(label="DM Feedback to Opener", style=discord.ButtonStyle.primary, emoji="✉️", custom_id="ticket:feedback", row=1)
    async def dm_feedback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only the claimer can send feedback
        if not self.claimer_id or interaction.user.id != self.claimer_id:
            return await interaction.response.send_message("Only the claimer can send feedback to the opener.", ephemeral=True)

        if not self.closed:
            return await interaction.response.send_message("Close the ticket before sending feedback to the opener.", ephemeral=True)

        # Enforce one-time per ticket
        meta = self.cog.channel_meta.get(str(interaction.channel.id), {})
        if meta.get("claimer_feedback_sent"):
            return await interaction.response.send_message("Feedback for this ticket has already been sent.", ephemeral=True)

        # Show modal
        modal = FeedbackModal(self.cog, opener_id=self.opener_id, claimer_id=self.claimer_id, channel=interaction.channel)
        await interaction.response.send_modal(modal)

# ---------------- Review ----------------
class ReviewView(discord.ui.View):
    def __init__(self, cog: "TicketCog", log_channel: Optional[discord.TextChannel], opener_id: int, staff_id: int, log_msg: Optional[discord.Message]):
        super().__init__(timeout=None)
        self.cog = cog
        self.log_channel = log_channel
        self.opener_id = opener_id
        self.staff_id = staff_id
        self.log_msg = log_msg
        self._used = False

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener_id:
            await interaction.response.send_message("Only the ticket opener can leave feedback.", ephemeral=True)
            return False
        if self._used:
            await interaction.response.send_message("This feedback has already been submitted.", ephemeral=True)
            return False
        return True

    async def _finalize(self, interaction: discord.Interaction, positive: bool):
        await self.cog.record_review(interaction.guild.id, self.staff_id, positive=positive)
        self._used = True

        # disable buttons
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            if interaction.message:
                await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

        text = "Thanks for your feedback! ✅" if positive else "Thanks for your feedback! ❌"
        if not interaction.response.is_done():
            await interaction.response.send_message(text, ephemeral=True)
        else:
            await interaction.channel.send(text, delete_after=5)

    @discord.ui.button(emoji="👍", style=discord.ButtonStyle.success, custom_id="ticket:review_up")
    async def thumbs_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await self._finalize(interaction, positive=True)

    @discord.ui.button(emoji="👎", style=discord.ButtonStyle.danger, custom_id="ticket:review_down")
    async def thumbs_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await self._finalize(interaction, positive=False)

# ---------------- Cog (tail) ----------------
class TicketCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: Dict[str, Dict] = load_config()
        self.channel_meta: Dict[str, Dict] = self.config.setdefault("_channel_meta", {})
        save_config(self.config)
        self._autopost_task = self.bot.loop.create_task(self.autopost_loop())

        self._suppress_sync = False  # prevent spammy updates during bulk ops


    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        if before.name == after.name:
            return
        changed = False
        for gid, g in self.config.items():
            if gid == "_channel_meta":
                continue
            roster = g.get("roster", {})
            if str(after.id) in roster:
                roster[str(after.id)]["name"] = after.name
                changed = True
                try:
                    await self.update_roster_message(int(gid))
                except Exception:
                    pass
        if changed:
            save_config(self.config)

    def build_ticket_welcome_embed(self, member: discord.Member, coach_role: Optional[discord.Role], thumb_url: str, banner_url: str) -> discord.Embed:
        # Kept function name/signature for compatibility; wording generalized.
        display = member.display_name
        _unused_role = coach_role  # retained to avoid breaking callers
    
        headline = "📨 A new ticket has been opened."
        greeting = f"**Hello {display}!**"
        body = (
            "Staff will be with you shortly. Please describe your request clearly. "
            "When resolved, a staff member can close and archive the ticket."
        )
    
        embed = discord.Embed(
            description=f"{headline}\n\n{greeting}\n\n{body}",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        if thumb_url:
            embed.set_thumbnail(url=thumb_url)
        if banner_url:
            embed.set_image(url=banner_url)
        return embed
    
    # ---------- Roster commands ----------
    @app_commands.command(name="ticket_roster_add", description="Add a member to the ticket staff roster")
    @app_commands.checks.has_permissions(administrator=True)
    async def roster_add(self, interaction: discord.Interaction, member: discord.Member):
        g = self.config.setdefault(str(interaction.guild.id), {})
        roster = g.setdefault("roster", {})
        if str(member.id) in roster:
            return await interaction.response.send_message("⚠️ That member is already in the roster.", ephemeral=True)
        roster[str(member.id)] = {"name": member.name, "good": 0, "bad": 0}
        save_config(self.config)

        # Give claim role if configured
        role = self._get_claim_role(interaction.guild)
        if role and role not in member.roles:
            try:
                await member.add_roles(role, reason="Added to ticket roster")
            except Exception:
                pass

        await interaction.response.send_message(f"✅ Added {member.mention} to the roster.", ephemeral=True)

    @app_commands.command(name="ticket_roster_remove", description="Remove a member from the ticket staff roster")
    @app_commands.checks.has_permissions(administrator=True)
    async def roster_remove(self, interaction: discord.Interaction, member: discord.Member):
        g = self.config.setdefault(str(interaction.guild.id), {})
        roster = g.setdefault("roster", {})
        if roster.pop(str(member.id), None) is None:
            return await interaction.response.send_message("⚠️ That member is not in the roster.", ephemeral=True)
        save_config(self.config)

        # Optionally remove claim role when removed from roster
        role = self._get_claim_role(interaction.guild)
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason="Removed from ticket roster")
            except Exception:
                pass

        await interaction.response.send_message(f"❌ Removed {member.mention} from the roster.", ephemeral=True)

    @app_commands.command(name="ticket_roster", description="View the public ticket staff roster with ratings")
    async def roster_view(self, interaction: discord.Interaction):
        embeds = self.build_roster_embeds(interaction.guild.id)
        channel = interaction.channel
    
        g = self.config.setdefault(str(interaction.guild.id), {})
        auto = g.get("roster_autopost")
        msg = None
        if auto and auto.get("message_id"):
            try:
                msg = await channel.fetch_message(auto["message_id"])
            except discord.NotFound:
                msg = None
    
        if msg:
            if len(embeds) == 1:
                await msg.edit(embed=embeds[0], content=None)
            else:
                await msg.edit(embeds=embeds, content=None)
        else:
            if len(embeds) == 1:
                msg = await channel.send(embed=embeds[0])
            else:
                msg = await channel.send(embeds=embeds)
    
            if auto is not None:
                auto["message_id"] = msg.id
                save_config(self.config)
    
        await interaction.response.send_message("✅ Roster posted.", ephemeral=True)



    def build_roster_embeds(self, guild_id: int) -> list[discord.Embed]:
        g = self.config.get(str(guild_id), {})
        roster = g.get("roster", {})
        members = list(roster.items())  # (uid, data)
        embeds: list[discord.Embed] = []
    
        # If empty roster
        if not members:
            e = discord.Embed(
                title="🎟️ Ticket Staff Roster",
                description="No members are on the roster yet.",
                color=discord.Color.gold(),
                timestamp=discord.utils.utcnow()
            )
            e.set_footer(text="Last updated")
            return [e]
    
        guild = self.bot.get_guild(guild_id)
    
        # Chunk into pages of 25
        for i in range(0, len(members), 25):
            e = discord.Embed(
                title="🎟️ Ticket Staff Roster",
                color=discord.Color.gold(),
                timestamp=discord.utils.utcnow()
            )
            e.set_footer(text="Last updated")
    
            for uid, data in members[i:i+25]:
                member_obj = guild.get_member(int(uid)) if guild else None
    
                if member_obj:
                    display = member_obj.display_name
                    uname = member_obj.name
                    live_name = uname if display == uname else f"{display} ({uname})"
                else:
                    live_name = data.get("name") or "Unknown"
    
                name = live_name[:256]
                good = int(data.get("good", 0))
                bad = int(data.get("bad", 0))
                total = good + bad
                rating = f"{(good/total)*100:.1f}% 👍 ({good} / {total})" if total else "No reviews yet"
    
                e.add_field(name=name, value=rating[:1024], inline=False)
    
            embeds.append(e)
    
        return embeds

    # ---------- Auto Roster Posting ----------
    @app_commands.command(name="ticket_roster_autopost_set", description="Set up auto-posting roster updates")
    @app_commands.checks.has_permissions(administrator=True)
    async def roster_autopost_set(self, interaction: discord.Interaction, channel: discord.TextChannel, interval_minutes: Optional[int] = 60):
        g = self.config.setdefault(str(interaction.guild.id), {})
        g["roster_autopost"] = {
            "channel_id": channel.id,
            "message_id": None,
            "interval": interval_minutes
        }
        save_config(self.config)
        await interaction.response.send_message(
            f"✅ Auto roster posting enabled in {channel.mention} every {interval_minutes} minutes.",
            ephemeral=True
        )
        await self.update_roster_message(interaction.guild.id)

    @app_commands.command(name="ticket_roster_autopost_disable", description="Disable auto roster posting")
    @app_commands.checks.has_permissions(administrator=True)
    async def roster_autopost_disable(self, interaction: discord.Interaction):
        g = self.config.setdefault(str(interaction.guild.id), {})
        g.pop("roster_autopost", None)
        save_config(self.config)
        await interaction.response.send_message("❌ Auto roster posting disabled.", ephemeral=True)

    @app_commands.command(name="ticket_roster_autopost_now", description="Force refresh the auto roster message")
    @app_commands.checks.has_permissions(administrator=True)
    async def roster_autopost_now(self, interaction: discord.Interaction):
        await self.update_roster_message(interaction.guild.id, force_new=True)
        await interaction.response.send_message("🔄 Roster message refreshed.", ephemeral=True)

    async def update_roster_message(self, guild_id: int, force_new: bool = False):
        g = self.config.get(str(guild_id), {})
        auto = g.get("roster_autopost")
        if not auto:
            return
    
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        channel = guild.get_channel(auto.get("channel_id"))
        if not channel:
            return
    
        embeds = self.build_roster_embeds(guild.id)
    
        msg = None
        if not force_new and auto.get("message_id"):
            try:
                msg = await channel.fetch_message(auto["message_id"])
            except discord.NotFound:
                msg = None
    
        if msg:
            if len(embeds) == 1:
                await msg.edit(embed=embeds[0], content=None)
            else:
                await msg.edit(embeds=embeds, content=None)
        else:
            if len(embeds) == 1:
                msg = await channel.send(embed=embeds[0])
            else:
                msg = await channel.send(embeds=embeds)
            auto["message_id"] = msg.id
            save_config(self.config)


    async def autopost_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                now = time.time()
                for gid, g in list(self.config.items()):
                    if gid == "_channel_meta":
                        continue
                    auto = g.get("roster_autopost")
                    if not auto:
                        continue
                    interval = max(1, int(auto.get("interval", 60))) * 60
                    last = float(auto.get("last_post", 0))
                    if now - last >= interval:
                        await self.update_roster_message(int(gid))
                        auto["last_post"] = time.time()
                        save_config(self.config)
                await asyncio.sleep(60)
            except Exception:
                await asyncio.sleep(60)

    async def record_review(self, guild_id: int, staff_id: int, positive: bool):
        g = self.config.setdefault(str(guild_id), {})
        roster = g.setdefault("roster", {})
        entry = roster.setdefault(str(staff_id), {"name": "Unknown", "good": 0, "bad": 0})
        if positive:
            entry["good"] += 1
        else:
            entry["bad"] += 1
        save_config(self.config)
        await self.update_roster_message(guild_id)

    # ---------- Claim role: config & syncing ----------
    def _get_claim_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        gid = str(guild.id)
        rid = self.config.get(gid, {}).get("claim_role_id")
        return guild.get_role(rid) if rid else None

    @app_commands.command(name="ticket_claim_role_set", description="Set the role whose members can claim tickets (also auto-sync with roster)")
    @app_commands.checks.has_permissions(administrator=True)
    async def claim_role_set(self, interaction: discord.Interaction, role: discord.Role):
        g = self.config.setdefault(str(interaction.guild.id), {})
        g["claim_role_id"] = role.id
        save_config(self.config)
        await interaction.response.send_message(f"✅ Claiming role set to {role.mention}. Use `/ticket_roster_sync` to reconcile now.", ephemeral=True)

    @app_commands.command(name="ticket_roster_sync", description="Sync claim role ↔ roster (two-way)")
    @app_commands.checks.has_permissions(administrator=True)
    async def roster_sync(self, interaction: discord.Interaction):
        guild = interaction.guild
        g = self.config.setdefault(str(guild.id), {})
        roster = g.setdefault("roster", {})
        role = self._get_claim_role(guild)

        added_to_roster = 0
        role_granted = 0

        # A) ensure: all role members are in roster
        if role:
            for m in role.members:
                if str(m.id) not in roster:
                    roster[str(m.id)] = {"name": m.display_name, "good": 0, "bad": 0}
                    added_to_roster += 1

        # B) ensure: all roster members have role
        if role:
            for uid in list(roster.keys()):
                member = guild.get_member(int(uid))
                if member and role not in member.roles:
                    try:
                        await member.add_roles(role, reason="Roster sync")
                        role_granted += 1
                    except Exception:
                        pass

        save_config(self.config)
        await self.update_roster_message(guild.id)
        await interaction.response.send_message(f"🔁 Sync complete. Added **{added_to_roster}** to roster; granted role to **{role_granted}**.", ephemeral=True)

    @app_commands.command(name="ticket_roster_purge", description="Remove claim role from everyone and clear the roster")
    @app_commands.checks.has_permissions(administrator=True)
    async def roster_purge(self, interaction: discord.Interaction, confirm: bool = False):
        if not confirm:
            return await interaction.response.send_message(
                "⚠️ This will clear the roster and remove the claim role from all members. Re-run with `confirm: True` to proceed.",
                ephemeral=True
            )
    
        guild = interaction.guild
        g = self.config.setdefault(str(guild.id), {})
        role = self._get_claim_role(guild)
    
        await interaction.response.send_message("🧹 Purging roster… this may take a moment.", ephemeral=True)
    
        removed = 0
        self._suppress_sync = True  # stop on_member_update churn
    
        try:
            if role:
                for m in list(role.members):
                    try:
                        await m.remove_roles(role, reason="Roster purge")
                        removed += 1
                    except Exception:
                        pass
                    await asyncio.sleep(0.15)
    
            g["roster"] = {}
            save_config(self.config)
            await self.update_roster_message(guild.id, force_new=False)
    
        finally:
            self._suppress_sync = False
            await self.update_roster_message(guild.id, force_new=False)
    
        await interaction.followup.send(
            f"✅ Purged. Removed role from **{removed}** members and cleared the roster.",
            ephemeral=True
        )


    # ---------- Panel setup ----------
    @app_commands.command(name="ticket_setup", description="Create a ticket panel")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_setup(self, interaction: discord.Interaction, panel_name: str):
        view = TicketSetupView(self, interaction.guild, panel_name)
        await interaction.response.send_message(
            f"Configuring panel `{panel_name}` — choose options below:", view=view, ephemeral=True
        )

    @app_commands.command(name="ticket_panel_edit", description="Edit an existing ticket panel embed")
    @app_commands.checks.has_permissions(administrator=True)
    async def panel_edit(self, interaction: discord.Interaction, panel_name: str, new_title: str, new_description: str):
        gid = str(interaction.guild.id)
        gdata = self.config.get(gid, {})
        panels = gdata.get("panels", {})
        panel = panels.get(panel_name)
        if not panel:
            return await interaction.response.send_message(f"⚠️ Panel `{panel_name}` not found.", ephemeral=True)
    
        channel = interaction.guild.get_channel(panel.get("channel_id"))
        if not channel:
            return await interaction.response.send_message("⚠️ Panel channel missing.", ephemeral=True)
    
        try:
            msg = await channel.fetch_message(panel.get("message_id"))
        except Exception:
            return await interaction.response.send_message("⚠️ Could not fetch panel message.", ephemeral=True)
    
        embed = discord.Embed(
            title=new_title,
            description=new_description,
            color=discord.Color.orange()
        )
        # Keep or override banner
        embed.set_image(url=panel.get("ticket_image_url") or DEFAULT_TICKET_BANNER_URL)
    
        view = TicketPanelView(self, interaction.guild.id, panel_name)
    
        await msg.edit(embed=embed, view=view)
        await interaction.response.send_message(f"✅ Panel `{panel_name}` updated.", ephemeral=True)

    @app_commands.command(
        name="ticket_image_set",
        description="Set the large banner image shown on new ticket welcome messages"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_image_set(
        self,
        interaction: discord.Interaction,
        panel_name: str,
        image_url: str
    ):
        gid = str(interaction.guild.id)
        panel = self.config.setdefault(gid, {}).setdefault("panels", {}).get(panel_name)
        if not panel:
            return await interaction.response.send_message(
                f"⚠️ Panel `{panel_name}` not found.", ephemeral=True
            )
    
        panel["ticket_image_url"] = image_url
        save_config(self.config)
        await interaction.response.send_message("✅ Updated banner image.", ephemeral=True)
    
    
    @app_commands.command(
        name="ticket_thumb_set",
        description="Set the small thumbnail image shown on new ticket welcome messages"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_thumb_set(
        self,
        interaction: discord.Interaction,
        panel_name: str,
        image_url: str
    ):
        gid = str(interaction.guild.id)
        panel = self.config.setdefault(gid, {}).setdefault("panels", {}).get(panel_name)
        if not panel:
            return await interaction.response.send_message(
                f"⚠️ Panel `{panel_name}` not found.", ephemeral=True
            )
    
        panel["ticket_thumb_url"] = image_url
        save_config(self.config)
        await interaction.response.send_message("✅ Updated thumbnail image.", ephemeral=True)


    # ---------- Persistent views ----------
    async def cog_load(self):
        if not hasattr(self, "config") or self.config is None:
            self.config = load_config()
            self.channel_meta = self.config.setdefault("_channel_meta", {})
        for gid, gdata in list(self.config.items()):
            if gid == "_channel_meta":
                continue
            for panel_name in gdata.get("panels", {}):
                self.bot.add_view(TicketPanelView(self, int(gid), panel_name))
        self.bot.add_view(TicketChannelView(0, self, None, None, 0))
        self.bot.add_view(ReviewView(self, None, 0, 0, None))

    # ---------- Listeners to auto-sync when role changes ----------
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if self._suppress_sync:
            return
    
        role = self._get_claim_role(after.guild)
        if not role:
            return
        had = role in before.roles
        has = role in after.roles
        if had == has:
            return
    
        g = self.config.setdefault(str(after.guild.id), {})
        roster = g.setdefault("roster", {})
        if has and str(after.id) not in roster:
            roster[str(after.id)] = {"name": after.display_name, "good": 0, "bad": 0}
            save_config(self.config)
            await self.update_roster_message(after.guild.id)
        elif not has and str(after.id) in roster:
            roster.pop(str(after.id), None)
            save_config(self.config)
            await self.update_roster_message(after.guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
