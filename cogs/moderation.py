import asyncio
import json
import logging
import os
import re
from datetime import timedelta
from typing import Optional  # <-- important for Python 3.9

import discord
from discord import app_commands
from discord.ext import commands

LOG = logging.getLogger(__name__)

DATA_PATH = "automod_data.json"

DEFAULT_CONFIG = {
    "enabled": False,
    "banned_words": [],
    "offense_threshold": 3,
    "penalty": "none",   # "none" | "kick" | "ban"
    "offenses": {}
}

def load_data():
    if not os.path.exists(DATA_PATH):
        return {}
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_data(data: dict):
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_PATH)

def get_guild_config(data: dict, guild_id: int) -> dict:
    str_gid = str(guild_id)
    if str_gid not in data:
        data[str_gid] = {
            "enabled": DEFAULT_CONFIG["enabled"],
            "banned_words": [],
            "offense_threshold": DEFAULT_CONFIG["offense_threshold"],
            "penalty": DEFAULT_CONFIG["penalty"],
            "offenses": {}
        }
    return data[str_gid]

# ---- Duration parsing "1d2h30m15s" ----
DUR_PATTERN = re.compile(
    r"^\s*(?:(?P<d>\d+)\s*d)?\s*(?:(?P<h>\d+)\s*h)?\s*(?:(?P<m>\d+)\s*m)?\s*(?P<s>\d+)?\s*s?\s*$",
    re.IGNORECASE
)

def parse_duration(s: str) -> Optional[timedelta]:
    m = DUR_PATTERN.match(s or "")
    if not m:
        return None
    d = int(m.group("d") or 0)
    h = int(m.group("h") or 0)
    mi = int(m.group("m") or 0)
    se = int(m.group("s") or 0)
    if d == h == mi == se == 0:
        return None
    return timedelta(days=d, hours=h, minutes=mi, seconds=se)

async def ensure_muted_role(guild: discord.Guild) -> discord.Role:
    """Create or fetch a 'Muted' role and apply channel overwrites to limit sending/speaking."""
    role = discord.utils.get(guild.roles, name="Muted")
    if role is None:
        LOG.info(f"Creating Muted role in guild {guild.id}")
        role = await guild.create_role(
            name="Muted",
            permissions=discord.Permissions.none(),
            reason="Create muted role for moderation"
        )
    # Apply basic overwrites
    overwrite_kwargs = {
        "send_messages": False,
        "add_reactions": False,
        "speak": False,
        "stream": False
    }
    for channel in guild.channels:
        try:
            overwrites = channel.overwrites_for(role)
            changed = False
            for key, value in overwrite_kwargs.items():
                if getattr(overwrites, key) is None or getattr(overwrites, key) is True:
                    setattr(overwrites, key, value)
                    changed = True
            if changed:
                await channel.set_permissions(role, overwrite=overwrites,
                                              reason="Apply mute role channel overwrites")
        except (discord.Forbidden, discord.HTTPException):
            continue
    return role

class ConfirmView(discord.ui.View):
    """Buttons to confirm or cancel a sensitive action, restricted to the command invoker."""
    def __init__(self, author: discord.abc.User, *, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.author_id = author.id
        self.value: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the command invoker can use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm purge", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="✅ Confirmed. Purging…", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❎ Cancelled.", view=self)
        self.stop()

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data = load_data()

    async def _dm_member(self, member: discord.Member, text: str):
        try:
            await member.send(text)
        except discord.Forbidden:
            pass

    # ---------------------- Automod Listener ----------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        cfg = get_guild_config(self.data, message.guild.id)
        if not cfg["enabled"]:
            return

        banned = cfg.get("banned_words", [])
        if not banned:
            return

        content_lower = message.content.lower()
        hit = next((w for w in banned if w and w.lower() in content_lower), None)
        if hit is None:
            return

        # Delete & warn
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            return

        try:
            await message.channel.send(
                f"{message.author.mention} Your message contained a banned word and was removed. Please follow the server rules.",
                delete_after=12
            )
        except discord.HTTPException:
            pass

        # Track offenses
        offenses = cfg.setdefault("offenses", {})
        uid = str(message.author.id)
        offenses[uid] = offenses.get(uid, 0) + 1
        save_data(self.data)

        # Threshold penalty
        threshold = int(cfg.get("offense_threshold", 3))
        penalty = cfg.get("penalty", "none")
        if offenses[uid] >= threshold and penalty in {"kick", "ban"}:
            offenses[uid] = 0
            save_data(self.data)
            reason = f"Automod: reached {threshold} offenses for banned words."
            if penalty == "kick":
                try:
                    await self._dm_member(message.author, f"You were kicked from **{message.guild.name}**. Reason: {reason}")
                    await message.guild.kick(message.author, reason=reason)
                except discord.Forbidden:
                    pass
            elif penalty == "ban":
                try:
                    await self._dm_member(message.author, f"You were banned from **{message.guild.name}**. Reason: {reason}")
                    await message.guild.ban(message.author, reason=reason, delete_message_days=0)
                except discord.Forbidden:
                    pass

    # ---------------------- Slash Commands ----------------------
    group = app_commands.Group(name="mod", description="Moderation commands")

    # Purge with safety confirmation if no user is specified
    @group.command(name="purge", description="Delete messages in this channel (optionally by user).")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(
        user="(Optional) Whose messages to delete. If omitted, deletes all.",
        limit="How many recent messages to scan (max 1000)"
    )
    async def purge(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
        limit: app_commands.Range[int, 1, 1000] = 200
    ):
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message(
                "This command can only be used in text channels or threads.",
                ephemeral=True
            )

        if user:
            await interaction.response.defer(ephemeral=True, thinking=True)
            def check(m: discord.Message):
                return m.author.id == user.id
            deleted = await channel.purge(limit=limit, check=check, reason=f"Purged by {interaction.user}")
            return await interaction.followup.send(
                f"Deleted {len(deleted)} messages by {user.mention} in {channel.mention}.",
                ephemeral=True
            )

        # Safety confirmation for ALL messages purge
        view = ConfirmView(interaction.user, timeout=30)
        content = (
            f"⚠️ This will delete up to **{limit}** recent messages from **everyone** in {channel.mention} "
            "(messages older than 14 days cannot be bulk-deleted). Are you sure?"
        )
        await interaction.response.send_message(content, view=view, ephemeral=True)
        await view.wait()

        if view.value is not True:
            if view.value is None:
                try:
                    await interaction.followup.send("⏳ Timed out. No messages were deleted.", ephemeral=True)
                except discord.HTTPException:
                    pass
            return

        # Confirmed
        try:
            deleted = await channel.purge(limit=limit, reason=f"Purged by {interaction.user}")
            await interaction.followup.send(
                f"Deleted {len(deleted)} messages in {channel.mention}.",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send("I lack permissions to purge messages here.", ephemeral=True)

    # Mute (timed)
    @group.command(name="mute", description="Mute a member for a specified duration (e.g. 10m, 2h, 1d2h).")
    @app_commands.checks.has_permissions(moderate_members=True, manage_roles=True, manage_channels=True)
    @app_commands.describe(member="Member to mute", duration="e.g. 10m, 2h, 1d2h", reason="Optional reason")
    async def mute(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: Optional[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
            return await interaction.followup.send("You can't mute someone with an equal or higher role.", ephemeral=True)

        td = parse_duration(duration)
        if not td:
            return await interaction.followup.send("Invalid duration. Try formats like `10m`, `2h`, `1d2h`.", ephemeral=True)

        muted_role = await ensure_muted_role(interaction.guild)
        try:
            await member.add_roles(muted_role, reason=reason or "Muted by moderation")
        except discord.Forbidden:
            return await interaction.followup.send("I lack permissions to add the Muted role.", ephemeral=True)

        await interaction.followup.send(f"{member.mention} has been muted for **{duration}**.", ephemeral=True)

        async def unmute_later():
            try:
                await asyncio.sleep(td.total_seconds())
                if muted_role in member.roles:
                    await member.remove_roles(muted_role, reason="Timed mute expired")
            except Exception as e:
                LOG.warning(f"Unmute task error: {e}")

        asyncio.create_task(unmute_later())

    # Temporary Ban
    @group.command(name="tempban", description="Temporarily ban a member, then unban after the duration.")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.describe(member="Member to ban", duration="e.g. 30m, 12h, 3d", reason="Reason to DM & log")
    async def tempban(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: str):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
            return await interaction.followup.send("You can't ban someone with an equal or higher role.", ephemeral=True)

        td = parse_duration(duration)
        if not td:
            return await interaction.followup.send("Invalid duration. Use formats like `30m`, `12h`, `3d`.", ephemeral=True)

        await self._dm_member(member, f"You have been temporarily banned from **{interaction.guild.name}** for **{duration}**.\nReason: {reason}")

        try:
            await interaction.guild.ban(member, reason=reason, delete_message_days=0)
        except discord.Forbidden:
            return await interaction.followup.send("I lack permissions to ban that member.", ephemeral=True)

        await interaction.followup.send(f"{member} banned for **{duration}**. They will be unbanned automatically.", ephemeral=True)

        async def unban_later():
            try:
                await asyncio.sleep(td.total_seconds())
                await interaction.guild.unban(discord.Object(id=member.id), reason="Temporary ban expired")
            except discord.NotFound:
                pass
            except Exception as e:
                LOG.warning(f"Unban task error: {e}")

        asyncio.create_task(unban_later())

    # Kick
    @group.command(name="kick", description="Kick a member and DM them the reason.")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.describe(member="Member to kick", reason="Reason to DM & log")
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        await interaction.response.defer(ephemeral=True, thinking=True)

        if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
            return await interaction.followup.send("You can't kick someone with an equal or higher role.", ephemeral=True)

        await self._dm_member(member, f"You have been kicked from **{interaction.guild.name}**.\nReason: {reason}")
        try:
            await interaction.guild.kick(member, reason=reason)
        except discord.Forbidden:
            return await interaction.followup.send("I lack permissions to kick that member.", ephemeral=True)

        await interaction.followup.send(f"{member} has been kicked.", ephemeral=True)

    # ---------------------- Automod Config ----------------------
    automod = app_commands.Group(name="automod", description="Automod configuration")

    # (automod toggle, addword, removeword, list, setpenalty, setthreshold, resetuser are unchanged)
    # ... keep all those commands the same as before ...

async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))