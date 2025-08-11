# combined_bot.py
# Python 3.10+ | discord.py 2.3+
# Combines:
#  1) Ticket Bot (multi-panel, claim, transcript)
#  2) Coach Roster Bot (with admin approval flow)
#
# NOTE: Enable "Message Content Intent" in the Discord Developer Portal for transcripts and reading coach submissions.

import re
import os
import io
import json
import asyncio
import aiohttp
import collections
import discord
from discord.ext import commands
from datetime import datetime, timezone, timezone
from typing import Optional, Dict, Any
from discord import app_commands

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
# AutoMod
    a = g.setdefault("automod", {})
    a.setdefault("enabled", True)
    a.setdefault("log_channel_id", None)
    a.setdefault("slurs", [])
    a.setdefault("spam_window_seconds", 6)
    a.setdefault("spam_max_messages", 5)
    a.setdefault("repeat_max_duplicates", 12)
    a.setdefault("mention_limit", 6)
    a.setdefault("caps_ratio_trigger", 0.8)
    a.setdefault("timeout_minutes", 0)

    # AutoRole
    ar = g.setdefault("autorole", {})
    ar.setdefault("role_id", None)

    # Reddit daily feed
    rd = g.setdefault("reddit_feed", {})
    rd.setdefault("subreddit", None)         # e.g., "funny"
    rd.setdefault("channel_id", None)        # target channel id
    rd.setdefault("time_hhmm", None)         # "09:00" 24h format, server time
    rd.setdefault("last_run_ymd", None)      # "YYYY-MM-DD" to avoid double-posting

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
        await self.add_cog(AutoMod(self))
        await self.add_cog(RolePanel(self))
        await self.add_cog(RedditFeed(self))
        
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

    async def on_member_join(member: discord.Member):
        g = bot.gcfg(member.guild.id).setdefault("autorole", {})
        role_id = g.get("role_id")
        if role_id:
            role = member.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason="AutoRole assignment")
                except discord.Forbidden:
                    print(f"[AutoRole] Missing permissions to assign role in {member.guild.name}")
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

    def _slug(self, s: str) -> str:
        s = (s or "").lower().replace(" ", "-")
        return re.sub(r"[^a-z0-9-]", "", s) or "user"

    @discord.ui.button(label="📌 Claim", style=discord.ButtonStyle.success, custom_id="ticket_claim_btn")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_channels:
            return await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)

        # Read topic metadata
        topic = interaction.channel.topic or ""
        parts = [p for p in topic.split("|") if "=" in p]
        meta = dict(p.split("=", 1) for p in parts)

        # Stop double-claim
        if meta.get("claimed") and meta.get("claimed") != "none":
            return await interaction.response.send_message("This ticket is already claimed.", ephemeral=True)

        # Get opener from topic; fall back to current user
        opener_id = int(meta.get("opener") or interaction.user.id)
        opener = interaction.guild.get_member(opener_id) or interaction.user

        # Extract 3-digit ticket sequence from the CURRENT name (prefix only)
        m = re.match(r"^(\d{3})", interaction.channel.name)
        seq = m.group(1) if m else "000"

        opener_slug = self._slug(opener.display_name or opener.name)
        claimer_slug = self._slug(interaction.user.display_name or interaction.user.name)

        # Build a CLEAN name: <seq>-<opener>-<claimer>
        new_name = f"{seq}-{opener_slug}-{claimer_slug}"

        # Update topic meta
        meta["claimed"] = str(interaction.user.id)
        new_topic = "|".join(f"{k}={v}" for k, v in meta.items())

        await interaction.channel.edit(name=new_name, topic=new_topic, reason=f"Claimed by {interaction.user}")
        await interaction.response.send_message(f"Claimed by {interaction.user.mention}.")
    
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


        # new
        try:
            await interaction.channel.delete()
        except discord.NotFound:
        # Channel is already deleted; just continue
            pass
# ---------- Coach Roster System ----------
DEFAULT_TEMPLATE = (
"""
```
In-Game Name:
PlayfabID: 
Region / Server Preference:
    
Hours Played: 
Casual Level: 
Current Rank (if applicable): 

Past Ranked or Competitive Experience (if applicable): 
    
Specialties (check all that apply):
    [] Duels
    [] Teamfights (3v3 / 5v5)
    [] Footwork / Mitigation
    [] Reading Opponents
    [] Swing Manipulation
    [] Offense
    [] Defense
    [] Other: 
    
Coaching Style (check all that apply):
    [] Drills & Private Matches
    [] Recorded Gameplay Analysis
    [] Live Match Commentary
    [] Other: 
    
Session Length & Structure:
(Example: “Typically 1-hour sessions with warmup, targeted drills, and live practice.”)
    
Availability (with Time Zone):
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

                # Notify in-channel
                await interaction.channel.send(
                    "📢 **Application submitted!** An admin may now review it.",
                    allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=False)
                )

                # Private confirmation
                return await interaction.response.send_message("✅ Submitted for review.", ephemeral=True)

        await interaction.response.send_message("Couldn't find your template message.", ephemeral=True)

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.primary, custom_id="coach_approve_btn")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_channels:
            return await interaction.response.send_message("Admins only.", ephemeral=True)

        g = self.bot.gcfg(interaction.guild_id)["coach"]
        roster_ch = interaction.guild.get_channel(g.get("roster_channel_id"))
        if not roster_ch:
            return await interaction.response.send_message("No roster channel set.", ephemeral=True)

        topic = interaction.channel.topic or ""
        parts = topic.split("|")
        opener_id = None
        submitted_msg_id = None
        for p in parts:
            if p.startswith("opener="):
                opener_id = int(p.split("=")[1])
            elif p.startswith("submitted="):
                try:
                    submitted_msg_id = int(p.split("=")[1])
                except ValueError:
                    pass

        if not submitted_msg_id:
            return await interaction.response.send_message("No submitted message found.", ephemeral=True)

        try:
            submitted_msg = await interaction.channel.fetch_message(submitted_msg_id)
        except discord.NotFound:
            return await interaction.response.send_message("Submitted message not found.", ephemeral=True)

        # Post to roster
        embed = discord.Embed(
            title=f"Coach Application - {interaction.guild.get_member(opener_id)}",
            description=submitted_msg.content,
            color=discord.Color.green()
        )
        await roster_ch.send(embed=embed)

        await interaction.response.send_message("✅ Approved and posted to roster.", ephemeral=True)

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

        # new
        try:
            await interaction.channel.delete()
        except discord.NotFound:
        # Channel is already deleted; just continue
            pass

class AutoMod(commands.Cog):
    """Basic slur + spam detection with per-guild config and logging."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # rolling window of message timestamps per guild->user
        self._history: dict[int, dict[int, collections.deque[float]]] = {}

    # ---------- helpers ----------
    def _cfg(self, guild_id: int) -> dict[str, Any]:
        return self.bot.gcfg(guild_id).setdefault("automod", {})

    def _log_ch(self, guild: discord.Guild, cfg: dict[str, Any]) -> Optional[discord.TextChannel]:
        ch_id = cfg.get("log_channel_id")
        return guild.get_channel(ch_id) if ch_id else None

    async def _log(self, message: discord.Message, reason: str, extra: Optional[str] = None):
        cfg = self._cfg(message.guild.id)
        ch = self._log_ch(message.guild, cfg)
        if not ch:
            return
        embed = discord.Embed(
            title="AutoMod action",
            description=reason,
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Author", value=f"{message.author} ({message.author.id})", inline=False)
        if message.content:
            trimmed = message.content if len(message.content) <= 4000 else message.content[:4000] + "…"
            embed.add_field(name="Content", value=trimmed, inline=False)
        if message.jump_url:
            embed.add_field(name="Jump", value=message.jump_url, inline=False)
        if extra:
            embed.set_footer(text=extra)
        await ch.send(embed=embed)

    async def _punish(self, member: discord.Member, minutes: int, reason: str):
        if minutes <= 0:
            return
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        try:
            await member.edit(timed_out_until=until, reason=f"AutoMod: {reason}")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    # ---------- checks ----------
    def _contains_slur(self, content: str, slurs: list[str]) -> Optional[str]:
        low = content.lower()
        for s in slurs:
            s = s.strip().lower()
            if not s:
                continue
        # Whole-word match, ignores case
        # \b = word boundary; re.escape ensures exact match
            if re.search(rf"\b{re.escape(s)}\b", low):
                return s
        return None

    def _too_many_mentions(self, message: discord.Message, limit: int) -> bool:
        total = len(message.mentions) + len(message.role_mentions)
        if message.mention_everyone:
            total += 1
        return total >= max(1, limit)

    def _too_many_caps(self, content: str, ratio_trigger: float) -> bool:
        letters = [c for c in content if c.isalpha()]
        if len(letters) < 20:  # ignore small messages
            return False
        caps = sum(1 for c in letters if c.isupper())
        return caps / max(1, len(letters)) >= max(0.5, min(1.0, ratio_trigger))

    def _long_repeats(self, content: str, repeat_threshold: int) -> bool:
        if repeat_threshold <= 1:
            return False
        # detect any character repeating N+ times (e.g., "!!!!!!!!!", "loooooool")
        return bool(re.search(rf"(.)\1{{{repeat_threshold},}}", content))

    def _rate_limit(self, guild_id: int, user_id: int, window_s: int, max_msgs: int) -> bool:
        now = discord.utils.utcnow().timestamp()
        hist_g = self._history.setdefault(guild_id, {})
        dq = hist_g.setdefault(user_id, collections.deque())
        dq.append(now)
        # drop old
        cutoff = now - max(1, window_s)
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq) > max(1, max_msgs)

    # ---------- events ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if message.author.guild_permissions.manage_messages:
            return  # staff bypass

        cfg = self._cfg(message.guild.id)
        if not cfg.get("enabled", True):
            return

        content = message.content or ""
        acted = False
        reason = ""

        # 1) slurs
        hit = self._contains_slur(content, cfg.get("slurs", []))
        if hit:
            reason = f"Slur detected: '{hit}'"
            acted = True

        # 2) mass mentions
        if not acted and self._too_many_mentions(message, cfg.get("mention_limit", 6)):
            reason = "Mass mention / mention spam"
            acted = True

        # 3) long repeats (e.g., '!!!!!!!!!' or 'loooooool')
        if not acted and self._long_repeats(content, cfg.get("repeat_max_duplicates", 12)):
            reason = "Excessive repeated characters"
            acted = True

        # 4) caps lock rage
        if not acted and self._too_many_caps(content, cfg.get("caps_ratio_trigger", 0.8)):
            reason = "Excessive CAPS"
            acted = True

        # 5) burst spam (messages per short window)
        if not acted and self._rate_limit(
            message.guild.id,
            message.author.id,
            cfg.get("spam_window_seconds", 6),
            cfg.get("spam_max_messages", 5),
        ):
            reason = "Message rate spam"
            acted = True

        if acted:
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

            # short public nudge (auto-deletes), no ping storm
            try:
                warn = await message.channel.send(
                    f"{message.author.mention} your message was removed by AutoMod: **{reason}**",
                    allowed_mentions=discord.AllowedMentions(users=[message.author]),
                )
                await warn.delete(delay=8)
            except Exception:
                pass

            await self._punish(message.author, int(cfg.get("timeout_minutes", 0)), reason)
            await self._log(message, reason)
            return

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Recheck edits (users sometimes try to sneak content post-send)
        if after and after.content != (before.content or ""):
            await self.on_message(after)

class RoleButton(discord.ui.View):
    def __init__(self, role_id: int, *, timeout=None, allow_toggle=True):
        super().__init__(timeout=timeout)
        self.role_id = role_id
        self.allow_toggle = allow_toggle

    @discord.ui.button(label="Get Role", style=discord.ButtonStyle.primary, custom_id="role_button")
    async def get_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = interaction.guild.get_role(self.role_id)
        if not role:
            return await interaction.response.send_message("⚠️ Role not found!", ephemeral=True)

        if role in interaction.user.roles:
            if self.allow_toggle:
                await interaction.user.remove_roles(role)
                await interaction.response.send_message(f"❌ Removed {role.mention} from you.", ephemeral=True)
            else:
                await interaction.response.send_message(f"✅ You already have {role.mention}.", ephemeral=True)
        else:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(f"✅ You now have {role.mention}.", ephemeral=True)


class RolePanel(commands.Cog):
    def __init__(self, bot: CombinedBot):
        self.bot = bot

    @app_commands.command(name="role_panel", description="Create a button panel to give/take a role.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.describe(
        role="The role to give",
        channel="Channel to post the panel in",
        allow_toggle="Allow removing role by clicking again"
    )
    async def role_panel(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        channel: discord.TextChannel,
        allow_toggle: bool = True
    ):
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "⚠️ I can't give that role because it's higher than my highest role.",
                ephemeral=True
            )

        view = RoleButton(role.id, allow_toggle=allow_toggle)
        embed = discord.Embed(
            title="Role Panel",
            description=f"Click the button below to get the {role.mention} role.",
            color=discord.Color.blurple()
        )
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(f"✅ Role panel created in {channel.mention}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(RolePanel(bot))

class RedditFeed(commands.Cog):
    """Posts top 3 posts of the day from a subreddit to a channel at a set time daily."""
    def __init__(self, bot):
        self.bot = bot
        self.feeds = {}  # guild_id -> {subreddit, channel_id, time_hhmm}
        self.daily_reddit_task.start()

    def cog_unload(self):
        self.daily_reddit_task.cancel()

    @app_commands.command(name="redditfeed_set", description="Set subreddit, channel, and time for daily top 3 posts.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def redditfeed_set(
        self,
        interaction: discord.Interaction,
        subreddit: str,
        channel: discord.TextChannel,
        time_hhmm: str
    ):
        """Set up daily reddit feed (HH:MM 24-hour format)."""
        self.feeds[interaction.guild_id] = {
            "subreddit": subreddit,
            "channel_id": channel.id,
            "time_hhmm": time_hhmm
        }
        await interaction.response.send_message(
            f"✅ Daily Reddit feed set for r/{subreddit} in {channel.mention} at {time_hhmm} every day.",
            ephemeral=True
        )

    @app_commands.command(name="redditfeed_disable", description="Disable the daily reddit feed.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def redditfeed_disable(self, interaction: discord.Interaction):
        if interaction.guild_id in self.feeds:
            del self.feeds[interaction.guild_id]
            await interaction.response.send_message("❌ Reddit feed disabled.", ephemeral=True)
        else:
            await interaction.response.send_message("No feed is currently set.", ephemeral=True)

   @app_commands.command(name="redditfeed_show", description="Show the current reddit feed settings.")
    async def redditfeed_show(self, interaction: discord.Interaction):
        cfg = self.feeds.get(interaction.guild_id)
        if not cfg:
            return await interaction.response.send_message("No Reddit feed configured.", ephemeral=True)

        ch = interaction.guild.get_channel(cfg["channel_id"])
        await interaction.response.send_message(
            f"**Subreddit:** r/{cfg['subreddit']}\n"
            f"**Channel:** {(ch.mention if ch else f'`{cfg[\"channel_id\"]}` (missing)')}\n"
            f"**Time:** {cfg['time_hhmm']} (server time)",
            ephemeral=True
        )

    @tasks.loop(minutes=1)
    async def daily_reddit_task(self):
        now = datetime.datetime.now().strftime("%H:%M")
        for guild_id, cfg in self.feeds.items():
            if cfg["time_hhmm"] == now:
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue
                channel = guild.get_channel(cfg["channel_id"])
                if not channel:
                    continue
                await self.post_top_posts(channel, cfg["subreddit"])

    async def post_top_posts(self, channel: discord.TextChannel, subreddit: str):
        url = f"https://www.reddit.com/r/{subreddit}/top/.json?t=day&limit=3"
        headers = {"User-Agent": "DiscordBot/1.0"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    await channel.send(f"⚠️ Failed to fetch posts from r/{subreddit}.")
                    return
                data = await resp.json()

        posts = data.get("data", {}).get("children", [])
        if not posts:
            await channel.send(f"⚠️ No posts found for r/{subreddit} today.")
            return

        embed = discord.Embed(
            title=f"Top 3 posts today from r/{subreddit}",
            color=discord.Color.orange(),
            timestamp=datetime.datetime.utcnow()
        )

        for post in posts:
            post_data = post["data"]
            title = post_data["title"]
            url = f"https://reddit.com{post_data['permalink']}"
            score = post_data["score"]
            embed.add_field(
                name=f"{title} (👍 {score})",
                value=url,
                inline=False
            )

        await channel.send(embed=embed)

async def setup(bot):
    await bot.add_cog(RedditFeed(bot))

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

@app_commands.command(name="automod_slurs", description="Add/remove/list slur terms (substring match, case-insensitive)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(action="What to do", term="Term to add or remove")
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="list", value="list"),
])
async def automod_slurs(interaction: discord.Interaction, action: app_commands.Choice[str], term: Optional[str] = None):
    g = bot.gcfg(interaction.guild_id)["automod"]
    slurs: list[str] = g.setdefault("slurs", [])

    if action.value == "list":
        shown = ", ".join(f"`{s}`" for s in slurs) or "_(none)_"
        return await interaction.response.send_message(f"Current slurs: {shown}", ephemeral=True)

    if not term:
        return await interaction.response.send_message("Please provide a term.", ephemeral=True)

    t = term.strip()
    if not t:
        return await interaction.response.send_message("Empty term not allowed.", ephemeral=True)

    if action.value == "add":
        if t.lower() in (s.lower() for s in slurs):
            return await interaction.response.send_message(f"`{t}` already in list.", ephemeral=True)
        slurs.append(t)
        bot.save()
        return await interaction.response.send_message(f"✅ Added `{t}`.", ephemeral=True)

    if action.value == "remove":
        lowered = [s.lower() for s in slurs]
        if t.lower() not in lowered:
            return await interaction.response.send_message(f"`{t}` not found.", ephemeral=True)
        # remove first case-insensitive match
        idx = lowered.index(t.lower())
        slurs.pop(idx)
        bot.save()
        return await interaction.response.send_message(f"✅ Removed `{t}`.", ephemeral=True)

@app_commands.command(name="automod_thresholds", description="Tune AutoMod thresholds")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    spam_window_seconds="Seconds in the spam window",
    spam_max_messages="Max messages allowed in the window",
    repeat_max_duplicates="Max allowed repeated chars before flag",
    mention_limit="Mentions allowed in a single message",
    caps_ratio_trigger="0.5–1.0 caps ratio trigger (>=20 letters)",
    timeout_minutes="Timeout to apply on hit (0 = none)",
)
async def automod_thresholds(
    interaction: discord.Interaction,
    spam_window_seconds: Optional[int] = None,
    spam_max_messages: Optional[int] = None,
    repeat_max_duplicates: Optional[int] = None,
    mention_limit: Optional[int] = None,
    caps_ratio_trigger: Optional[float] = None,
    timeout_minutes: Optional[int] = None,
):
    g = bot.gcfg(interaction.guild_id)["automod"]
    if spam_window_seconds is not None: g["spam_window_seconds"] = max(1, spam_window_seconds)
    if spam_max_messages is not None:   g["spam_max_messages"] = max(1, spam_max_messages)
    if repeat_max_duplicates is not None: g["repeat_max_duplicates"] = max(2, repeat_max_duplicates)
    if mention_limit is not None:       g["mention_limit"] = max(1, mention_limit)
    if caps_ratio_trigger is not None:  g["caps_ratio_trigger"] = float(min(1.0, max(0.5, caps_ratio_trigger)))
    if timeout_minutes is not None:     g["timeout_minutes"] = max(0, timeout_minutes)
    bot.save()

    cur = g.copy()
    cur["slurs"] = f"{len(g.get('slurs', []))} term(s)"
    await interaction.response.send_message(
        "✅ Updated AutoMod thresholds:\n" +
        "\n".join(f"- **{k}**: {v}" for k, v in cur.items() if k != "enabled" and k != "log_channel_id"),
        ephemeral=True
    )

@app_commands.command(name="automod", description="Enable or disable AutoMod")
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_toggle(interaction: discord.Interaction, enabled: bool):
    g = bot.gcfg(interaction.guild_id)["automod"]
    g["enabled"] = enabled
    bot.save()
    await interaction.response.send_message(f"✅ AutoMod {'enabled' if enabled else 'disabled'}.", ephemeral=True)

@app_commands.command(name="automod_log", description="Set the AutoMod log channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def automod_log(interaction: discord.Interaction, channel: discord.TextChannel):
    g = bot.gcfg(interaction.guild_id)["automod"]
    g["log_channel_id"] = channel.id
    bot.save()
    await interaction.response.send_message(f"✅ AutoMod log set to {channel.mention}.", ephemeral=True)

@app_commands.command(
    name="automod_slurs",
    description="Add, remove, or list slur terms (substring match, case-insensitive)."
)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(action="Choose add/remove/list", term="The slur term to add or remove")
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="list", value="list"),
])
async def automod_slurs(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    term: Optional[str] = None
):
    # Ensure config exists
    g = bot.gcfg(interaction.guild_id).setdefault("automod", {})
    slurs: list[str] = g.setdefault("slurs", [])

    if action.value == "list":
        # Show all current slurs
        shown = ", ".join(f"`{s}`" for s in slurs) if slurs else "_(none)_"
        return await interaction.response.send_message(
            f"**Current slurs:** {shown}", ephemeral=True
        )

    if not term:
        return await interaction.response.send_message(
            "❌ You must provide a `term` for add/remove.", ephemeral=True
        )

    t = term.strip()
    if not t:
        return await interaction.response.send_message(
            "❌ Empty term is not allowed.", ephemeral=True
        )

    if action.value == "add":
        # Avoid case-insensitive duplicates
        if t.lower() in (s.lower() for s in slurs):
            return await interaction.response.send_message(
                f"⚠️ `{t}` is already in the slur list.", ephemeral=True
            )
        slurs.append(t)
        bot.save()
        return await interaction.response.send_message(
            f"✅ Added `{t}` to AutoMod slur list.", ephemeral=True
        )

    if action.value == "remove":
        lowered = [s.lower() for s in slurs]
        if t.lower() not in lowered:
            return await interaction.response.send_message(
                f"⚠️ `{t}` is not in the slur list.", ephemeral=True
            )
        idx = lowered.index(t.lower())
        removed = slurs.pop(idx)
        bot.save()
        return await interaction.response.send_message(
            f"✅ Removed `{removed}` from AutoMod slur list.", ephemeral=True
        )

@app_commands.command(name="panel_delete", description="Delete an existing ticket panel")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(panel_id="The ID number of the panel to delete")
async def panel_delete(interaction: discord.Interaction, panel_id: int):
    g = bot.gcfg(interaction.guild_id)["tickets"]
    panels = g.get("panels", {})

    if str(panel_id) not in panels:
        return await interaction.response.send_message(
            f"❌ No ticket panel with ID `{panel_id}` found.",
            ephemeral=True
        )

    # Remove from config
    removed_panel = panels.pop(str(panel_id))
    bot.save()

    await interaction.response.send_message(
        f"✅ Ticket panel `{panel_id}` deleted. "
        f"Any existing buttons for it will stop working after the next bot restart.",
        ephemeral=True
    )

@app_commands.command(name="purge", description="Delete the last N messages, optionally from a specific user.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(
    amount="Number of recent messages to delete (max 100)",
    user="Only delete messages from this user (optional)"
)
async def purge(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 100],
    user: Optional[discord.User] = None
):
    # Confirm command is in a text channel
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("❌ This command can only be used in text channels.", ephemeral=True)

    # Let the user know we're working
    await interaction.response.defer(ephemeral=True)

    def check(m: discord.Message):
        return (user is None or m.author.id == user.id)

    deleted = await interaction.channel.purge(limit=amount, check=check)
    await interaction.followup.send(
        f"✅ Deleted {len(deleted)} message(s){f' from {user.mention}' if user else ''}.",
        ephemeral=True
    )

@app_commands.command(name="autorole", description="Set or clear the role automatically assigned to new members.")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(role="The role to auto-assign (omit to clear)")
async def autorole(interaction: discord.Interaction, role: Optional[discord.Role] = None):
    g = bot.gcfg(interaction.guild_id).setdefault("autorole", {})

    if role:
        # Check if bot can assign it
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("❌ I can't assign that role (it's higher than my top role).", ephemeral=True)
        g["role_id"] = role.id
        bot.save()
        return await interaction.response.send_message(f"✅ AutoRole set to {role.mention}.", ephemeral=True)
    else:
        g["role_id"] = None
        bot.save()
        return await interaction.response.send_message("✅ AutoRole cleared.", ephemeral=True)

bot.tree.add_command(tickets_setup)
bot.tree.add_command(tickets_panel)
bot.tree.add_command(tickets_panels_list)
bot.tree.add_command(tickets_panel_roles)
bot.tree.add_command(panel_delete)
bot.tree.add_command(coach_setup_cmd)
bot.tree.add_command(coach_panel_cmd)
bot.tree.add_command(automod_toggle)
bot.tree.add_command(automod_log)
bot.tree.add_command(automod_slurs)
bot.tree.add_command(automod_thresholds)
bot.tree.add_command(purge)
bot.tree.add_command(autorole)

# ---------- Run Bot ----------
def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
    bot.run(token)

if __name__ == "__main__":
    main()
