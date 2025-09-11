# modlog.py
# discord.py 2.x Cog for comprehensive moderation logging.
# Features:
# - Message delete (single & bulk) with actor via audit logs
# - Message edit (shows before vs after)
# - Channel/category create/update/rename/delete
# - Role create/update/delete (diffs perms/attributes)
# - Member updates (nick/roles)
# - Emoji & sticker create/update/delete
# - Per-guild log channel via slash command
#
# Notes:
# - To identify "who did it", we check audit logs close in time to the event.
# - Make sure the bot has the "View Audit Log" permission.
# - For message edit/delete content, enable message_content intent.

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Iterable, Dict, Any, Tuple, List

import discord
from discord import app_commands
from discord.ext import commands

# --------------- Helpers ---------------

MAX_FIELD = 1024
MAX_DESC = 4096

def _truncate(text: Optional[str], limit: int = MAX_FIELD) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."

def _fmt_user(u: Optional[discord.abc.User]) -> str:
    if not u:
        return "Unknown"
    return f"{u} (`{u.id}`)"

def _fmt_channel(ch: Optional[discord.abc.GuildChannel]) -> str:
    if not ch:
        return "Unknown"
    prefix = "#" if isinstance(ch, discord.TextChannel) else ""
    return f"{prefix}{ch.name} (`{ch.id}`)"

def _fmt_role(r: Optional[discord.Role]) -> str:
    if not r:
        return "Unknown"
    return f"@{r.name} (`{r.id}`)"

def _fmt_bool(b: Optional[bool]) -> str:
    return "Yes" if b else "No"

def _fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "Unknown"
    return discord.utils.format_dt(dt, style="F")

def _perm_diff(before: discord.Permissions, after: discord.Permissions) -> Tuple[List[str], List[str]]:
    added, removed = [], []
    for name, value in after:
        if getattr(before, name) != value:
            if value:
                added.append(name)
            else:
                removed.append(name)
    return added, removed

def _dict_diff(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Tuple[Any, Any]]:
    keys = set(before) | set(after)
    diff = {}
    for k in keys:
        if before.get(k) != after.get(k):
            diff[k] = (before.get(k), after.get(k))
    return diff

# --------------- Cog ---------------

class ModLog(commands.Cog):
    """Comprehensive moderation & content logger."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # In-memory per-guild log channel storage. Replace with DB/JSON if you want persistence.
        self.log_channels: Dict[int, int] = {}
        # For debouncing audit lookups on spammy events
        self._audit_lock = asyncio.Lock()

    # ---------- Utilities ----------

    def _get_log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        ch_id = self.log_channels.get(guild.id)
        if not ch_id:
            return None
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        return None

    async def _send_embed(self, guild: discord.Guild, embed: discord.Embed, files: Optional[List[discord.File]] = None):
        ch = self._get_log_channel(guild)
        if not ch:
            return
        try:
            await ch.send(embed=embed, files=files or [])
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    async def _find_audit_actor(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        *,
        target_id: Optional[int] = None,
        extra_channel_id: Optional[int] = None,
        within: float = 15.0,
    ) -> Optional[discord.User]:
        """
        Attempt to identify the actor from the guild's audit log.
        Parameters:
          - action: AuditLogAction type to match.
          - target_id: The id of the target entity (user/channel/role/etc), if known.
          - extra_channel_id: For message deletions where 'extra' contains a channel.
          - within: seconds window to accept entries as "matching".
        """
        # Audit logs are rate-limited. Serialize calls lightly.
        async with self._audit_lock:
            try:
                now = datetime.now(timezone.utc)
                async for entry in guild.audit_logs(limit=10, action=action):
                    # time proximity
                    if (now - entry.created_at).total_seconds() > within:
                        continue

                    # Match target if provided
                    if target_id is not None:
                        if getattr(entry.target, "id", None) != target_id:
                            continue

                    # Match extra.channel if provided
                    if extra_channel_id is not None and entry.extra is not None:
                        ch = getattr(entry.extra, "channel", None)
                        if getattr(ch, "id", None) != extra_channel_id:
                            continue

                    # Found plausible actor
                    return entry.user
            except discord.Forbidden:
                return None
            except discord.HTTPException:
                return None
        return None

    def _base_embed(self, guild: discord.Guild, title: str, color: int = 0x2B2D31) -> discord.Embed:
        e = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
        icon_url = guild.icon.url if getattr(guild, "icon", None) else None
        e.set_footer(text=guild.name, icon_url=icon_url)
        return e

    # ---------- Slash commands ----------

    modlog_group = app_commands.Group(name="modlog", description="Configure moderation logging.")

    @modlog_group.command(name="set-channel", description="Set the channel where mod logs will be sent.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        self.log_channels[interaction.guild.id] = channel.id
        await interaction.response.send_message(f"✅ Mod log channel set to {channel.mention}", ephemeral=True)

    @set_channel.error
    async def set_channel_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("You need **Manage Server** to do that.", ephemeral=True)
        else:
            await interaction.response.send_message("Something went wrong setting the channel.", ephemeral=True)

    # ---------- Message events ----------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        ch = self._get_log_channel(message.guild)
        if not ch:
            return

        actor = await self._find_audit_actor(
            message.guild,
            discord.AuditLogAction.message_delete,
            target_id=message.author.id,
            extra_channel_id=message.channel.id,
        )

        e = self._base_embed(message.guild, "🗑️ Message Deleted", color=0xED4245)
        e.add_field(name="Author", value=_fmt_user(message.author), inline=True)
        e.add_field(name="Channel", value=_fmt_channel(message.channel), inline=True)
        e.add_field(name="Deleted by", value=_fmt_user(actor), inline=True)
        if message.content:
            e.add_field(name="Content", value=_truncate(message.content, 2000), inline=False)

        files = []
        # Include first attachment filenames/urls; not re-uploading to avoid large files
        if message.attachments:
            attach_list = "\n".join(f"{a.filename} — {a.url}" for a in message.attachments[:5])
            e.add_field(name="Attachments", value=_truncate(attach_list), inline=False)

        await self._send_embed(message.guild, e, files)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: Iterable[discord.Message]):
        messages = list(messages)
        if not messages:
            return
        guild = messages[0].guild
        if not guild:
            return
        if not self._get_log_channel(guild):
            return

        channel = messages[0].channel
        actor = await self._find_audit_actor(
            guild,
            discord.AuditLogAction.message_bulk_delete,
            extra_channel_id=channel.id,
        )

        e = self._base_embed(guild, "🧹 Bulk Message Delete", color=0xED4245)
        e.add_field(name="Channel", value=_fmt_channel(channel), inline=True)
        e.add_field(name="Deleted by", value=_fmt_user(actor), inline=True)
        e.add_field(name="Count", value=str(len(messages)), inline=True)
        await self._send_embed(guild, e)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or before.author.bot:
            return
        if before.content == after.content:
            return  # Embed edits, pin, etc., ignore
        if not self._get_log_channel(after.guild):
            return

        e = self._base_embed(after.guild, "✏️ Message Edited", color=0xFAA61A)
        e.add_field(name="Author", value=_fmt_user(after.author), inline=True)
        e.add_field(name="Channel", value=_fmt_channel(after.channel), inline=True)
        if before.content:
            e.add_field(name="Before", value=_truncate(before.content, 1024), inline=False)
        if after.content:
            e.add_field(name="After", value=_truncate(after.content, 1024), inline=False)
        e.add_field(name="Jump", value=f"[Jump to message]({after.jump_url})", inline=False)
        await self._send_embed(after.guild, e)

    # ---------- Channel & Category events ----------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        guild = channel.guild
        if not self._get_log_channel(guild):
            return
        actor = await self._find_audit_actor(guild, discord.AuditLogAction.channel_create, target_id=channel.id)
        e = self._base_embed(guild, "📺 Channel Created", color=0x57F287)
        e.add_field(name="Channel", value=_fmt_channel(channel), inline=True)
        e.add_field(name="Created by", value=_fmt_user(actor), inline=True)
        await self._send_embed(guild, e)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        guild = channel.guild
        if not self._get_log_channel(guild):
            return
        actor = await self._find_audit_actor(guild, discord.AuditLogAction.channel_delete, target_id=channel.id)
        e = self._base_embed(guild, "🗑️ Channel Deleted", color=0xED4245)
        e.add_field(name="Channel", value=f"{channel.name} (`{channel.id}`)", inline=True)
        e.add_field(name="Deleted by", value=_fmt_user(actor), inline=True)
        await self._send_embed(guild, e)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        guild = after.guild
        if not self._get_log_channel(guild):
            return

        changes = {}
        if before.name != after.name:
            changes["name"] = (before.name, after.name)
        if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
            if before.topic != after.topic:
                changes["topic"] = (before.topic, after.topic)
            if before.nsfw != after.nsfw:
                changes["nsfw"] = (before.nsfw, after.nsfw)
            if before.slowmode_delay != after.slowmode_delay:
                changes["slowmode"] = (before.slowmode_delay, after.slowmode_delay)

        if not changes:
            return

        actor = await self._find_audit_actor(guild, discord.AuditLogAction.channel_update, target_id=after.id)
        e = self._base_embed(guild, "🔧 Channel Updated", color=0x5865F2)
        e.add_field(name="Channel", value=_fmt_channel(after), inline=True)
        e.add_field(name="Updated by", value=_fmt_user(actor), inline=True)

        for k, (b, a) in changes.items():
            e.add_field(name=k.capitalize(), value=f"`{b}` ➜ `{a}`" if (b or a) else f"{b} ➜ {a}", inline=False)

        await self._send_embed(guild, e)

    # ---------- Role events ----------

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        guild = role.guild
        if not self._get_log_channel(guild):
            return
        actor = await self._find_audit_actor(guild, discord.AuditLogAction.role_create, target_id=role.id)
        e = self._base_embed(guild, "🏷️ Role Created", color=0x57F287)
        e.add_field(name="Role", value=_fmt_role(role), inline=True)
        e.add_field(name="Created by", value=_fmt_user(actor), inline=True)
        await self._send_embed(guild, e)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        guild = role.guild
        if not self._get_log_channel(guild):
            return
        actor = await self._find_audit_actor(guild, discord.AuditLogAction.role_delete, target_id=role.id)
        e = self._base_embed(guild, "🗑️ Role Deleted", color=0xED4245)
        e.add_field(name="Role", value=f"@{role.name} (`{role.id}`)", inline=True)
        e.add_field(name="Deleted by", value=_fmt_user(actor), inline=True)
        await self._send_embed(guild, e)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        guild = after.guild
        if not self._get_log_channel(guild):
            return

        diffs = {}
        if before.name != after.name:
            diffs["name"] = (before.name, after.name)
        if before.color != after.color:
            diffs["color"] = (str(before.color), str(after.color))
        if before.mentionable != after.mentionable:
            diffs["mentionable"] = (_fmt_bool(before.mentionable), _fmt_bool(after.mentionable))
        if before.hoist != after.hoist:
            diffs["hoist"] = (_fmt_bool(before.hoist), _fmt_bool(after.hoist))

        add, rem = _perm_diff(before.permissions, after.permissions)
        if add:
            diffs["permissions_added"] = (None, ", ".join(add))
        if rem:
            diffs["permissions_removed"] = (", ".join(rem), None)

        if not diffs:
            return

        actor = await self._find_audit_actor(guild, discord.AuditLogAction.role_update, target_id=after.id)

        e = self._base_embed(guild, "🔧 Role Updated", color=0x5865F2)
        e.add_field(name="Role", value=_fmt_role(after), inline=True)
        e.add_field(name="Updated by", value=_fmt_user(actor), inline=True)
        for k, (b, a) in diffs.items():
            e.add_field(name=k.replace("_", " ").title(), value=_truncate(f"{b or ''} ➜ {a or ''}", 1024), inline=False)
        await self._send_embed(guild, e)

    # ---------- Member updates (roles/nick) ----------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = after.guild
        if not self._get_log_channel(guild):
            return

        # Nickname changes
        if before.nick != after.nick:
            actor = await self._find_audit_actor(guild, discord.AuditLogAction.member_update, target_id=after.id)
            e = self._base_embed(guild, "🪪 Nickname Changed", color=0x5865F2)
            e.add_field(name="Member", value=_fmt_user(after), inline=True)
            e.add_field(name="Changed by", value=_fmt_user(actor), inline=True)
            e.add_field(name="Before", value=_truncate(before.nick or "None"), inline=True)
            e.add_field(name="After", value=_truncate(after.nick or "None"), inline=True)
            await self._send_embed(guild, e)

        # Role changes
        before_roles = set(before.roles)
        after_roles = set(after.roles)
        gained = list(after_roles - before_roles)
        lost = list(before_roles - after_roles)

        if gained or lost:
            actor = await self._find_audit_actor(guild, discord.AuditLogAction.member_role_update, target_id=after.id)
            e = self._base_embed(guild, "🧩 Roles Updated", color=0x5865F2)
            e.add_field(name="Member", value=_fmt_user(after), inline=True)
            e.add_field(name="Changed by", value=_fmt_user(actor), inline=True)
            if gained:
                e.add_field(name="Added", value=_truncate(", ".join(r.mention for r in gained)), inline=False)
            if lost:
                e.add_field(name="Removed", value=_truncate(", ".join(r.mention for r in lost)), inline=False)
            await self._send_embed(guild, e)

    # ---------- Emoji & Sticker events (optional but “everything”) ----------

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before: List[discord.Emoji], after: List[discord.Emoji]):
        if not self._get_log_channel(guild):
            return
        before_ids = {e.id: e for e in before}
        after_ids = {e.id: e for e in after}
        created = [e for e in after if e.id not in before_ids]
        deleted = [e for e in before if e.id not in after_ids]
        renamed = [a for a in after if a.id in before_ids and a.name != before_ids[a.id].name]

        if created:
            for eobj in created:
                actor = await self._find_audit_actor(guild, discord.AuditLogAction.emoji_create, target_id=eobj.id)
                e = self._base_embed(guild, "😀 Emoji Created", color=0x57F287)
                e.add_field(name="Emoji", value=f"{eobj} `{eobj.name}` (`{eobj.id}`)", inline=False)
                e.add_field(name="Created by", value=_fmt_user(actor), inline=True)
                await self._send_embed(guild, e)

        if deleted:
            for eobj in deleted:
                actor = await self._find_audit_actor(guild, discord.AuditLogAction.emoji_delete, target_id=eobj.id)
                e = self._base_embed(guild, "🗑️ Emoji Deleted", color=0xED4245)
                e.add_field(name="Emoji", value=f":{eobj.name}: (`{eobj.id}`)", inline=False)
                e.add_field(name="Deleted by", value=_fmt_user(actor), inline=True)
                await self._send_embed(guild, e)

        if renamed:
            for eobj in renamed:
                actor = await self._find_audit_actor(guild, discord.AuditLogAction.emoji_update, target_id=eobj.id)
                bname = before_ids[eobj.id].name
                e = self._base_embed(guild, "🔧 Emoji Updated", color=0x5865F2)
                e.add_field(name="Emoji", value=f"{eobj} (`{eobj.id}`)", inline=True)
                e.add_field(name="Updated by", value=_fmt_user(actor), inline=True)
                e.add_field(name="Name", value=f"`{bname}` ➜ `{eobj.name}`", inline=False)
                await self._send_embed(guild, e)

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: discord.Guild, before: List[discord.GuildSticker], after: List[discord.GuildSticker]):
        if not self._get_log_channel(guild):
            return
        before_ids = {s.id: s for s in before}
        after_ids = {s.id: s for s in after}
        created = [s for s in after if s.id not in before_ids]
        deleted = [s for s in before if s.id not in after_ids]
        updated = [a for a in after if a.id in before_ids and (a.name != before_ids[a.id].name or a.description != before_ids[a.id].description)]

        for s in created:
            actor = await self._find_audit_actor(guild, discord.AuditLogAction.sticker_create, target_id=s.id)
            e = self._base_embed(guild, "🩹 Sticker Created", color=0x57F287)
            e.add_field(name="Sticker", value=f"{s.name} (`{s.id}`)", inline=True)
            e.add_field(name="Created by", value=_fmt_user(actor), inline=True)
            await self._send_embed(guild, e)

        for s in deleted:
            actor = await self._find_audit_actor(guild, discord.AuditLogAction.sticker_delete, target_id=s.id)
            e = self._base_embed(guild, "🗑️ Sticker Deleted", color=0xED4245)
            e.add_field(name="Sticker", value=f"{s.name} (`{s.id}`)", inline=True)
            e.add_field(name="Deleted by", value=_fmt_user(actor), inline=True)
            await self._send_embed(guild, e)

        for s in updated:
            actor = await self._find_audit_actor(guild, discord.AuditLogAction.sticker_update, target_id=s.id)
            b = before_ids[s.id]
            e = self._base_embed(guild, "🔧 Sticker Updated", color=0x5865F2)
            e.add_field(name="Sticker", value=f"{s.name} (`{s.id}`)", inline=True)
            e.add_field(name="Updated by", value=_fmt_user(actor), inline=True)
            if b.name != s.name:
                e.add_field(name="Name", value=f"`{b.name}` ➜ `{s.name}`", inline=False)
            if b.description != s.description:
                e.add_field(name="Description", value=f"`{_truncate(b.description or 'None')}` ➜ `{_truncate(s.description or 'None')}`", inline=False)
            await self._send_embed(guild, e)

    # ---------- Cog setup ----------

    async def cog_load(self):
        # Sync app commands for this cog on ready (optional; you might centralize syncing elsewhere)
        # Safeguard: only sync in guilds where the bot is present.
        await asyncio.sleep(1)  # give the tree a tick to register
        try:
            # Global sync (comment out if you only want per-guild)
            await self.bot.tree.sync()
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(ModLog(bot))
    # Register the app command group
    try:
        bot.tree.add_command(ModLog.modlog_group)
    except app_commands.CommandAlreadyRegistered:
        pass
