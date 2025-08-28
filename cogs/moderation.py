# cogs/moderation.py
# Discord.py 2.x moderation cog
# Features: purge, mute/unmute (timeout), kick, ban/unban, slowmode, lock/unlock, setnick,
#           warn (add/list/clear), modlog channel configuration, robust errors/permissions.

from __future__ import annotations
import asyncio
import json
import os
import re
from datetime import timedelta, datetime, timezone
from typing import Optional, List

import discord
from discord import app_commands
from discord.ext import commands

CONFIG_FILE = "moderation_config.json"   # stores per-guild modlog channel id
WARN_FILE = "warnings.json"              # stores per-guild warnings


# ---------------------------- Persistence ----------------------------

def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_guild_cfg(guild_id: int) -> dict:
    cfg = _load_json(CONFIG_FILE)
    return cfg.get(str(guild_id), {})

def set_guild_cfg(guild_id: int, key: str, value):
    cfg = _load_json(CONFIG_FILE)
    gid = str(guild_id)
    if gid not in cfg:
        cfg[gid] = {}
    cfg[gid][key] = value
    _save_json(CONFIG_FILE, cfg)

def get_warns(guild_id: int) -> dict:
    data = _load_json(WARN_FILE)
    return data.get(str(guild_id), {})

def set_warns(guild_id: int, warns: dict):
    data = _load_json(WARN_FILE)
    data[str(guild_id)] = warns
    _save_json(WARN_FILE, data)


# ---------------------------- Utils ----------------------------

DURATION_RE = re.compile(
    r"(?:(?P<weeks>\d+)\s*w)?\s*(?:(?P<days>\d+)\s*d)?\s*(?:(?P<hours>\d+)\s*h)?\s*(?:(?P<minutes>\d+)\s*m)?\s*(?:(?P<seconds>\d+)\s*s)?",
    re.I
)

def parse_duration(s: str) -> Optional[timedelta]:
    """
    Parse strings like '10m', '2h30m', '1d', '1w2d3h', '45s'.
    Returns a timedelta or None if invalid or zero.
    """
    s = (s or "").strip()
    if not s:
        return None
    m = DURATION_RE.fullmatch(s)
    if not m:
        return None
    parts = {k: int(v) for k, v in m.groupdict(default="0").items()}
    td = timedelta(
        weeks=parts["weeks"],
        days=parts["days"],
        hours=parts["hours"],
        minutes=parts["minutes"],
        seconds=parts["seconds"],
    )
    if td.total_seconds() <= 0:
        return None
    # Discord timeout limit is 28 days
    if td > timedelta(days=28):
        td = timedelta(days=28)
    return td

def fmt_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    weeks, rem = divmod(total, 7*24*3600)
    days, rem = divmod(rem, 24*3600)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    bits = []
    if weeks: bits.append(f"{weeks}w")
    if days: bits.append(f"{days}d")
    if hours: bits.append(f"{hours}h")
    if minutes: bits.append(f"{minutes}m")
    if seconds and not bits:  # show seconds if small duration
        bits.append(f"{seconds}s")
    return " ".join(bits) if bits else "0s"

def can_manage(member: discord.Member, target: discord.Member) -> bool:
    """Return True if `member` can act on `target` based on top role position."""
    if member == target:
        return False
    if target.guild.owner_id == target.id:
        return False
    return member.top_role > target.top_role

async def send_modlog(guild: discord.Guild, embed: discord.Embed):
    cfg = get_guild_cfg(guild.id)
    channel_id = cfg.get("modlog_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.send(embed=embed)
    except Exception:
        pass

def base_embed(action: str, moderator: discord.Member, reason: Optional[str]) -> discord.Embed:
    em = discord.Embed(
        title=f"{action}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    em.set_footer(text=f"Moderator: {moderator} • ID: {moderator.id}")
    if reason:
        em.add_field(name="Reason", value=discord.utils.escape_markdown(reason), inline=False)
    return em


# ---------------------------- Cog ----------------------------

class Moderation(commands.Cog):
    """Moderation commands (slash)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Register the "Quick Mute 10m" user context menu at runtime
        self._quick_mute_ctx = app_commands.ContextMenu(
            name="Quick Mute 10m",
            callback=self.quick_mute_ctx,            # points to the method below
        )
        # Attach it to the global command tree
        self.bot.tree.add_command(self._quick_mute_ctx)

    async def cog_unload(self):
        # Clean up the context menu when the cog unloads/reloads
        try:
            self.bot.tree.remove_command(self._quick_mute_ctx.name, type=self._quick_mute_ctx.type)
        except Exception:
            pass

    # ---- Admin & Setup ----
    @app_commands.command(name="setmodlog", description="Set the channel to receive moderation logs.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(channel="Text channel for moderation logs")
    async def setmodlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        set_guild_cfg(interaction.guild_id, "modlog_channel_id", channel.id)
        await interaction.response.send_message(
            f"✅ Mod-log channel set to {channel.mention}.",
            ephemeral=True
        )

    # ---- Purge ----
    @purge.command(name="bulk", description="Quickly delete the last N messages (uses Discord bulk delete).")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(
        amount="Number of recent messages to delete (1-1000)"
    )
    async def purge_bulk(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 1000],
    ):
        # Avoid 'Interaction failed'
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            # Uses the bulk delete API under the hood when bulk=True
            deleted = await interaction.channel.purge(
                limit=amount,
                bulk=True,
                reason=f"Purged by {interaction.user} via /purge bulk"
            )
        except discord.Forbidden:
            return await interaction.followup.send("❌ I don’t have permission to delete messages here.", ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send("⚠️ Bulk delete failed (messages older than 14 days can’t be bulk-deleted).", ephemeral=True)

        # Ephemeral confirmation to the moderator
        await interaction.followup.send(f"🧹 Deleted **{len(deleted)}** messages.", ephemeral=True)

        # Mod-log entry (reuses your helpers)
        em = base_embed("Purge (Bulk)", interaction.user, reason=f"{len(deleted)} messages deleted.")
        em.add_field(name="Amount Requested", value=str(amount))
        em.add_field(name="Channel", value=interaction.channel.mention)
        await send_modlog(interaction.guild, em)



    
    purge = app_commands.Group(name="purge", description="Delete messages in bulk.")

    @purge.command(name="messages", description="Delete a number of messages with optional filters.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(
        amount="Number of messages to delete (1-1000)",
        user="Only delete messages from this user",
        contains="Only delete messages that contain this text (case-insensitive)",
        bots_only="Only delete messages sent by bots",
        attachments_only="Only delete messages that have attachments"
    )
    async def purge_messages(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 1000],
        user: Optional[discord.User] = None,
        contains: Optional[str] = None,
        bots_only: Optional[bool] = False,
        attachments_only: Optional[bool] = False,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        def check(msg: discord.Message) -> bool:
            if user and msg.author.id != user.id:
                return False
            if bots_only and not msg.author.bot:
                return False
            if attachments_only and not msg.attachments:
                return False
            if contains and contains.lower() not in (msg.content or "").lower():
                return False
            return True

        deleted: List[discord.Message] = await interaction.channel.purge(limit=amount, check=check, bulk=True, reason=f"Purged by {interaction.user} via /purge")
        await interaction.followup.send(f"🧹 Deleted {len(deleted)} messages.", ephemeral=True)

        em = base_embed("Purge", interaction.user, reason=f"{len(deleted)} messages deleted in {interaction.channel.mention}")
        details = []
        details.append(f"Amount requested: **{amount}**")
        if user: details.append(f"User: {user.mention}")
        if contains: details.append(f"Contains: `{contains}`")
        if bots_only: details.append("Bots only: **Yes**")
        if attachments_only: details.append("Attachments only: **Yes**")
        if details:
            em.add_field(name="Filters", value="\n".join(details), inline=False)
        em.add_field(name="Channel", value=interaction.channel.mention)
        await send_modlog(interaction.guild, em)

    # ---- Mute / Unmute (Timeout) ----
    @app_commands.command(name="mute", description="Timeout (mute) a member for a duration (e.g., 10m, 2h, 1d).")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(member="Member to mute", duration="e.g., 10m, 2h, 1d (max 28d)", reason="Optional reason")
    async def mute(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: Optional[str] = None):
        if not can_manage(interaction.user, member):
            return await interaction.response.send_message("❌ You can’t mute this member (role hierarchy).", ephemeral=True)
        td = parse_duration(duration)
        if not td:
            return await interaction.response.send_message("❌ Invalid duration. Try examples like `10m`, `2h30m`, `1d`.", ephemeral=True)
        try:
            await member.timeout(until=discord.utils.utcnow() + td, reason=reason or f"Muted by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don’t have permission to timeout that member.", ephemeral=True)
        except discord.HTTPException:
            return await interaction.response.send_message("⚠️ Failed to timeout the member.", ephemeral=True)

        await interaction.response.send_message(f"🔇 {member.mention} muted for **{fmt_duration(td)}**.", ephemeral=True)
        em = base_embed("Mute", interaction.user, reason)
        em.add_field(name="Member", value=f"{member} (`{member.id}`)")
        em.add_field(name="Duration", value=fmt_duration(td))
        await send_modlog(interaction.guild, em)

    @app_commands.command(name="unmute", description="Remove timeout from a member.")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(member="Member to unmute", reason="Optional reason")
    async def unmute(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
        try:
            await member.timeout(until=None, reason=reason or f"Unmuted by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don’t have permission to unmute that member.", ephemeral=True)
        except discord.HTTPException:
            return await interaction.response.send_message("⚠️ Failed to unmute the member.", ephemeral=True)
        await interaction.response.send_message(f"🔊 {member.mention} unmuted.", ephemeral=True)
        em = base_embed("Unmute", interaction.user, reason)
        em.add_field(name="Member", value=f"{member} (`{member.id}`)")
        await send_modlog(interaction.guild, em)

    # ---- Kick / Ban / Unban ----
    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.describe(member="Member to kick", reason="Optional reason")
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
        if not can_manage(interaction.user, member):
            return await interaction.response.send_message("❌ You can’t kick this member (role hierarchy).", ephemeral=True)
        try:
            await member.kick(reason=reason or f"Kicked by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don’t have permission to kick that member.", ephemeral=True)
        await interaction.response.send_message(f"👢 {member.mention} kicked.", ephemeral=True)
        em = base_embed("Kick", interaction.user, reason)
        em.add_field(name="Member", value=f"{member} (`{member.id}`)")
        await send_modlog(interaction.guild, em)

    @app_commands.command(name="ban", description="Ban a user from the server.")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.describe(
        member="Member to ban",
        delete_message_days="Delete message history (0-7 days)",
        reason="Optional reason"
    )
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
        reason: Optional[str] = None
    ):
        if not can_manage(interaction.user, member):
            return await interaction.response.send_message("❌ You can’t ban this member (role hierarchy).", ephemeral=True)
        try:
            await interaction.guild.ban(member, reason=reason or f"Banned by {interaction.user}", delete_message_days=delete_message_days)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don’t have permission to ban that member.", ephemeral=True)
        await interaction.response.send_message(f"⛔ {member.mention} banned. (Deleted {delete_message_days}d of messages)", ephemeral=True)
        em = base_embed("Ban", interaction.user, reason)
        em.add_field(name="Member", value=f"{member} (`{member.id}`)")
        if delete_message_days:
            em.add_field(name="Messages Deleted", value=f"{delete_message_days} days")
        await send_modlog(interaction.guild, em)

    @app_commands.command(name="unban", description="Unban a user.")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.describe(user="User to unban (not a member)", reason="Optional reason")
    async def unban(self, interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
        try:
            await interaction.guild.unban(user, reason=reason or f"Unbanned by {interaction.user}")
        except discord.NotFound:
            return await interaction.response.send_message("❌ That user isn’t banned.", ephemeral=True)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don’t have permission to unban that user.", ephemeral=True)
        await interaction.response.send_message(f"✅ {user.mention} unbanned.", ephemeral=True)
        em = base_embed("Unban", interaction.user, reason)
        em.add_field(name="User", value=f"{user} (`{user.id}`)")
        await send_modlog(interaction.guild, em)

    # ---- Slowmode ----
    @app_commands.command(name="slowmode", description="Set slowmode on a channel.")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.describe(seconds="Slowmode seconds (0 to disable)", channel="Target channel (defaults to current)")
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600], channel: Optional[discord.TextChannel] = None):
        channel = channel or interaction.channel
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"Slowmode set by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don’t have permission to edit that channel.", ephemeral=True)
        verb = "disabled" if seconds == 0 else f"set to **{seconds}s**"
        await interaction.response.send_message(f"🐢 Slowmode {verb} in {channel.mention}.", ephemeral=True)
        em = base_embed("Slowmode", interaction.user, None)
        em.add_field(name="Channel", value=channel.mention)
        em.add_field(name="Value", value=str(seconds))
        await send_modlog(interaction.guild, em)

    # ---- Lock / Unlock ----
    @app_commands.command(name="lock", description="Lock a channel (prevent @everyone from sending messages).")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.describe(channel="Channel to lock (defaults to current)", reason="Optional reason")
    async def lock(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None, reason: Optional[str] = None):
        channel = channel or interaction.channel
        overwrites = channel.overwrites_for(interaction.guild.default_role)
        overwrites.send_messages = False
        try:
            await channel.set_permissions(interaction.guild.default_role, overwrite=overwrites, reason=reason or f"Locked by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don’t have permission to lock that channel.", ephemeral=True)
        await interaction.response.send_message(f"🔒 Locked {channel.mention}.", ephemeral=True)
        em = base_embed("Lock", interaction.user, reason)
        em.add_field(name="Channel", value=channel.mention)
        await send_modlog(interaction.guild, em)

    @app_commands.command(name="unlock", description="Unlock a channel (allow @everyone to send messages).")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.describe(channel="Channel to unlock (defaults to current)", reason="Optional reason")
    async def unlock(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None, reason: Optional[str] = None):
        channel = channel or interaction.channel
        overwrites = channel.overwrites_for(interaction.guild.default_role)
        overwrites.send_messages = None  # reset to channel default
        try:
            await channel.set_permissions(interaction.guild.default_role, overwrite=overwrites, reason=reason or f"Unlocked by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don’t have permission to unlock that channel.", ephemeral=True)
        await interaction.response.send_message(f"🔓 Unlocked {channel.mention}.", ephemeral=True)
        em = base_embed("Unlock", interaction.user, reason)
        em.add_field(name="Channel", value=channel.mention)
        await send_modlog(interaction.guild, em)

    # ---- Nickname ----
    @app_commands.command(name="setnick", description="Change a member’s nickname.")
    @app_commands.checks.has_permissions(manage_nicknames=True)
    @app_commands.describe(member="Member", nickname="New nickname (empty to clear)", reason="Optional reason")
    async def setnick(self, interaction: discord.Interaction, member: discord.Member, nickname: Optional[str] = None, reason: Optional[str] = None):
        if not can_manage(interaction.user, member):
            return await interaction.response.send_message("❌ You can’t change this member’s nickname (role hierarchy).", ephemeral=True)
        try:
            await member.edit(nick=nickname, reason=reason or f"Nickname changed by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don’t have permission to change that nickname.", ephemeral=True)
        label = nickname if nickname else "cleared"
        await interaction.response.send_message(f"🏷️ Nickname {('set to ' + discord.utils.escape_markdown(nickname)) if nickname else 'cleared'} for {member.mention}.", ephemeral=True)
        em = base_embed("Set Nickname", interaction.user, reason)
        em.add_field(name="Member", value=f"{member} (`{member.id}`)")
        em.add_field(name="Nickname", value=label)
        await send_modlog(interaction.guild, em)

    # ---- Warnings ----
    warn = app_commands.Group(name="warn", description="Manage user warnings.")

    @warn.command(name="add", description="Add a warning to a member.")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(member="Member to warn", reason="Reason for the warning")
    async def warn_add(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if not can_manage(interaction.user, member):
            return await interaction.response.send_message("❌ You can’t warn this member (role hierarchy).", ephemeral=True)
        warns = get_warns(interaction.guild_id)
        user_w = warns.get(str(member.id), [])
        entry = {
            "reason": reason,
            "by": interaction.user.id,
            "at": int(discord.utils.utcnow().timestamp())
        }
        user_w.append(entry)
        warns[str(member.id)] = user_w
        set_warns(interaction.guild_id, warns)

        await interaction.response.send_message(f"⚠️ Warning added to {member.mention}. They now have **{len(user_w)}** warning(s).", ephemeral=True)
        em = base_embed("Warn", interaction.user, reason)
        em.add_field(name="Member", value=f"{member} (`{member.id}`)")
        em.add_field(name="Total Warnings", value=str(len(user_w)))
        await send_modlog(interaction.guild, em)

    @warn.command(name="list", description="List warnings for a member.")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(member="Member")
    async def warn_list(self, interaction: discord.Interaction, member: discord.Member):
        warns = get_warns(interaction.guild_id).get(str(member.id), [])
        if not warns:
            return await interaction.response.send_message(f"✅ {member.mention} has no warnings.", ephemeral=True)
        lines = []
        for i, w in enumerate(warns, start=1):
            ts = datetime.fromtimestamp(w["at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            mod = interaction.guild.get_member(w["by"])
            mod_tag = f"{mod}" if mod else f"{w['by']}"
            lines.append(f"**{i}.** {discord.utils.escape_markdown(w['reason'])} — by `{mod_tag}` on {ts}")
        msg = "\n".join(lines)
        await interaction.response.send_message(f"Warnings for {member.mention}:\n{msg}", ephemeral=True)

    @warn.command(name="clear", description="Clear warnings for a member.")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.describe(member="Member")
    async def warn_clear(self, interaction: discord.Interaction, member: discord.Member):
        data = get_warns(interaction.guild_id)
        count = len(data.get(str(member.id), []))
        data[str(member.id)] = []
        set_warns(interaction.guild_id, data)
        await interaction.response.send_message(f"🧽 Cleared **{count}** warning(s) for {member.mention}.", ephemeral=True)
        em = base_embed("Clear Warnings", interaction.user, None)
        em.add_field(name="Member", value=f"{member} (`{member.id}`)")
        em.add_field(name="Cleared", value=str(count))
        await send_modlog(interaction.guild, em)

    # ---- Context Menu: Quick 10m mute (registered in __init__) ----
    async def quick_mute_ctx(self, interaction: discord.Interaction, member: discord.Member):
        # Permission check
        if not interaction.user.guild_permissions.moderate_members:
            return await interaction.response.send_message("❌ You need `Moderate Members` for this.", ephemeral=True)
        if not can_manage(interaction.user, member):
            return await interaction.response.send_message("❌ You can’t mute this member (role hierarchy).", ephemeral=True)

        td = timedelta(minutes=10)
        try:
            await member.timeout(discord.utils.utcnow() + td, reason=f"Quick mute by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ I don’t have permission to timeout that member.", ephemeral=True)

        await interaction.response.send_message(f"🔇 {member.mention} muted for **10m**.", ephemeral=True)
        em = base_embed("Quick Mute", interaction.user, "Quick context-menu mute")
        em.add_field(name="Member", value=f"{member} (`{member.id}`)")
        em.add_field(name="Duration", value="10m")
        await send_modlog(interaction.guild, em)

    # ---- Error handling ----
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Ensure these run in guilds only
        if not interaction.guild:
            await interaction.response.send_message("❌ Commands can only be used in servers.", ephemeral=True)
            return False
        return True

    @setmodlog.error
    @purge_messages.error
    @mute.error
    @unmute.error
    @kick.error
    @ban.error
    @unban.error
    @slowmode.error
    @lock.error
    @unlock.error
    @setnick.error
    @warn_add.error
    @warn_list.error
    @warn_clear.error
    async def on_app_cmd_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if interaction.response.is_done():
            send = interaction.followup.send
        else:
            send = interaction.response.send_message

        if isinstance(error, app_commands.MissingPermissions):
            return await send("❌ You’re missing required permissions for that.", ephemeral=True)
        if isinstance(error, app_commands.BotMissingPermissions):
            return await send("❌ I’m missing required permissions to do that.", ephemeral=True)
        if isinstance(error, app_commands.CommandOnCooldown):
            return await send(f"⏳ Slow down. Try again in {error.retry_after:.1f}s.", ephemeral=True)

        # Fallback
        await send("⚠️ Something went wrong running that command.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))

