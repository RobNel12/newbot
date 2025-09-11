# turtle.py
# Requires discord.py 2.x and members intent enabled in your bot.
# In your main bot file, ensure Intents.members = True.
# Example:
# intents = discord.Intents.default()
# intents.members = True
# bot = commands.Bot(command_prefix="!", intents=intents)

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

CONFIG_PATH = "welcome_config.json"


# -----------------------------
# Storage
# -----------------------------
@dataclass
class WelcomeConfig:
    # Back-compat single channel (used as fallback if specific ones aren't set)
    channel_id: Optional[int] = None
    log_join_ping_role_id: Optional[int] = None

    # Separate channels
    join_channel_id: Optional[int] = None
    leave_channel_id: Optional[int] = None

    # Logging toggles + optional separate log channels (basic text, no embed)
    log_join: bool = False
    log_leave: bool = False
    log_join_channel_id: Optional[int] = None
    log_leave_channel_id: Optional[int] = None

    # Content for join
    join_title: str = "Welcome to {guild}!"
    join_message: str = "Hey {member}, you’re member #{count}! Make yourself at home."
    join_image_url: Optional[str] = None

    # Content for leave
    leave_title: str = "Goodbye, {name}"
    leave_message: str = "{name} has left {guild}. We’re now {count} strong."
    leave_image_url: Optional[str] = None

    # --- NEW: Auto-role assignment on join ---
    autorole_on: bool = False
    autorole_role_id: Optional[int] = None
    autorole_ignore_bots: bool = True


@dataclass
class GuildConfig:
    welcome: WelcomeConfig


class Store:
    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self._data: Dict[str, Any] = {}
        self._guild_cache: Dict[int, GuildConfig] = {}
        self.load()

    def load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}
        else:
            self._data = {}

    async def save(self) -> None:
        serializable: Dict[str, Any] = {}
        # Merge cache back into data
        for gid, gc in self._guild_cache.items():
            serializable[str(gid)] = {
                "welcome": {
                    "channel_id": gc.welcome.channel_id,
                    "join_channel_id": gc.welcome.join_channel_id,
                    "leave_channel_id": gc.welcome.leave_channel_id,
                    "log_join": gc.welcome.log_join,
                    "log_leave": gc.welcome.log_leave,
                    "log_join_channel_id": gc.welcome.log_join_channel_id,
                    "log_leave_channel_id": gc.welcome.log_leave_channel_id,
                    "join_title": gc.welcome.join_title,
                    "join_message": gc.welcome.join_message,
                    "join_image_url": gc.welcome.join_image_url,
                    "leave_title": gc.welcome.leave_title,
                    "leave_message": gc.welcome.leave_message,
                    "leave_image_url": gc.welcome.leave_image_url,
                    "autorole_on": gc.welcome.autorole_on,
                    "autorole_role_id": gc.welcome.autorole_role_id,
                    "autorole_ignore_bots": gc.welcome.autorole_ignore_bots,
                }
            }
        # Include anything we didn't touch this session
        for k, v in self._data.items():
            if k not in serializable:
                serializable[k] = v

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        self._data = serializable

    def cfg(self, guild_id: int) -> GuildConfig:
        if guild_id in self._guild_cache:
            return self._guild_cache[guild_id]

        raw = self._data.get(str(guild_id)) or {}
        wraw = raw.get("welcome") or {}
        wc = WelcomeConfig(
            channel_id=wraw.get("channel_id"),
            join_channel_id=wraw.get("join_channel_id"),
            leave_channel_id=wraw.get("leave_channel_id"),
            log_join=bool(wraw.get("log_join", False)),
            log_leave=bool(wraw.get("log_leave", False)),
            log_join_channel_id=wraw.get("log_join_channel_id"),
            log_leave_channel_id=wraw.get("log_leave_channel_id"),
            join_title=wraw.get("join_title", "Welcome to {guild}!"),
            join_message=wraw.get("join_message", "Hey {member}, you’re member #{count}! Make yourself at home."),
            join_image_url=wraw.get("join_image_url"),
            leave_title=wraw.get("leave_title"),
            leave_message=wraw.get("leave_message"),
            leave_image_url=wraw.get("leave_image_url"),
            autorole_on=bool(wraw.get("autorole_on", False)),
            autorole_role_id=wraw.get("autorole_role_id"),
            autorole_ignore_bots=bool(wraw.get("autorole_ignore_bots", True)),
        )
        gc = GuildConfig(welcome=wc)
        self._guild_cache[guild_id] = gc
        return gc


# -----------------------------
# Cog
# -----------------------------
class Turtle(commands.Cog):
    """Welcome / Leave messages with optional basic logging + auto-role on join."""

    # Slash command group: /welcome
    welcome = app_commands.Group(name="welcome", description="Configure welcome (join) & leave messages")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = Store()

    # ------------- Utilities -------------

    @staticmethod
    def _placeholders(member: discord.abc.User | discord.Member, guild: discord.Guild) -> Dict[str, str]:
        name = getattr(member, "display_name", member.name)
        return {
            "member": member.mention,
            "name": name,
            "guild": guild.name,
            "count": str(guild.member_count or 0),
        }

    def _build_embed(
        self,
        title_tmpl: str,
        msg_tmpl: str,
        image_url: Optional[str],
        member: discord.abc.User | discord.Member,
        guild: discord.Guild,
    ) -> discord.Embed:
        ph = self._placeholders(member, guild)
        title = title_tmpl.format_map(ph)
        desc = msg_tmpl.format_map(ph)
        embed = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
        if image_url:
            embed.set_image(url=image_url)
        return embed

    def _choose_join_channel(self, guild: discord.Guild, wc: WelcomeConfig) -> Optional[discord.TextChannel]:
        ch_id = wc.join_channel_id or wc.channel_id
        return guild.get_channel(ch_id) if ch_id else None  # type: ignore

    def _choose_leave_channel(self, guild: discord.Guild, wc: WelcomeConfig) -> Optional[discord.TextChannel]:
        ch_id = wc.leave_channel_id or wc.channel_id
        return guild.get_channel(ch_id) if ch_id else None  # type: ignore

    async def _send_basic_log(self, channel: Optional[discord.TextChannel], text: str) -> None:
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(text)
            except discord.Forbidden:
                pass

    async def _maybe_assign_autorole(self, member: discord.Member) -> None:
        """Assign the configured role on join if enabled and possible."""
        guild = member.guild
        wc = self.store.cfg(guild.id).welcome

        if not wc.autorole_on:
            return
        if member.bot and wc.autorole_ignore_bots:
            return
        if wc.autorole_role_id is None:
            return

        role = guild.get_role(wc.autorole_role_id)
        if role is None:
            return  # role deleted or not visible

        # Check basic permission/hierarchy safety
        me = guild.me
        if me is None:
            return
        if not me.guild_permissions.manage_roles:
            return
        if role >= me.top_role:
            return

        try:
            await member.add_roles(role, reason="Auto-role on member join")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    async def _send_welcome(self, member: discord.Member) -> None:
        guild = member.guild
        wc = self.store.cfg(guild.id).welcome

        # Pretty embed to join channel
        target_ch = self._choose_join_channel(guild, wc)
        if isinstance(target_ch, discord.TextChannel):
            try:
                embed = self._build_embed(wc.join_title, wc.join_message, wc.join_image_url, member, guild)
                await target_ch.send(embed=embed)
            except discord.Forbidden:
                pass

        # inside _send_welcome, in the "Optional basic log" block
        if wc.log_join:
            log_ch = guild.get_channel(wc.log_join_channel_id) if wc.log_join_channel_id else target_ch
            ping = f"<@&{wc.log_join_ping_role_id}>" if getattr(wc, "log_join_ping_role_id", None) else ""
            log_line = f"{ping} JOIN: {getattr(member, 'display_name', member.name)} ({member.id}) joined. Members now: {guild.member_count}."
            await self._send_basic_log(log_ch, log_line)

    async def _send_leave(self, member: discord.Member) -> None:
        guild = member.guild
        wc = self.store.cfg(guild.id).welcome

        # Pretty embed to leave channel
        target_ch = self._choose_leave_channel(guild, wc)
        if isinstance(target_ch, discord.TextChannel):
            try:
                embed = self._build_embed(wc.leave_title, wc.leave_message, wc.leave_image_url, member, guild)
                await target_ch.send(embed=embed)
            except discord.Forbidden:
                pass

        # Optional basic log
        if wc.log_leave:
            log_ch = guild.get_channel(wc.log_leave_channel_id) if wc.log_leave_channel_id else target_ch
            line = f"LEAVE: {getattr(member, 'display_name', member.name)} ({member.id}) left. Members now: {guild.member_count}."
            await self._send_basic_log(log_ch, line)

    # ------------- Events -------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            # Assign role first (so welcome placeholders like {count} reflect post-join state anyway)
            await self._maybe_assign_autorole(member)
        except Exception:
            pass
        try:
            await self._send_welcome(member)
        except Exception:
            # Avoid crashing on unexpected errors
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        try:
            await self._send_leave(member)
        except Exception:
            pass

    # ------------- Slash Commands -------------

    @welcome.command(name="set", description="Configure welcome/leave, logging, and auto-role on join")
    @app_commands.describe(
        join_channel="Channel for WELCOME (join) embeds",
        leave_channel="Channel for LEAVE embeds",
        log_join="Also send a basic log line when someone joins?",
        log_leave="Also send a basic log line when someone leaves?",
        log_join_channel="Channel for JOIN logs (defaults to join_channel if not set)",
        log_leave_channel="Channel for LEAVE logs (defaults to leave_channel if not set)",
        join_title="Title for welcome embed (supports {member}, {name}, {guild}, {count})",
        join_message="Body for welcome embed (supports placeholders)",
        join_image_url="Image URL for welcome embed (optional)",
        leave_title="Title for leave embed (supports placeholders)",
        leave_message="Body for leave embed (supports placeholders)",
        leave_image_url="Image URL for leave embed (optional)",
        autorole_on="Enable automatic role assignment on member join?",
        autorole_role="Which role to assign when a member joins",
        autorole_ignore_bots="If enabled, bots will NOT receive the auto-role",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_set(
        self,
        interaction: discord.Interaction,
        join_channel: Optional[discord.TextChannel] = None,
        leave_channel: Optional[discord.TextChannel] = None,
        log_join: Optional[bool] = None,
        log_leave: Optional[bool] = None,
        log_join_channel: Optional[discord.TextChannel] = None,
        log_leave_channel: Optional[discord.TextChannel] = None,
        log_join_ping_role: Optional[discord.Role] = None,
        join_title: Optional[str] = None,
        join_message: Optional[str] = None,
        join_image_url: Optional[str] = None,
        leave_title: Optional[str] = None,
        leave_message: Optional[str] = None,
        leave_image_url: Optional[str] = None,
        autorole_on: Optional[bool] = None,
        autorole_role: Optional[discord.Role] = None,
        autorole_ignore_bots: Optional[bool] = None,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

        guild = interaction.guild
        gc = self.store.cfg(guild.id)
        wc = gc.welcome

        # Channels
        if join_channel is not None:
            wc.join_channel_id = join_channel.id
        if leave_channel is not None:
            wc.leave_channel_id = leave_channel.id

        # Logging
        if log_join is not None:
            wc.log_join = log_join
        if log_leave is not None:
            wc.log_leave = log_leave
        if log_join_channel is not None:
            wc.log_join_channel_id = log_join_channel.id
        if log_leave_channel is not None:
            wc.log_leave_channel_id = log_leave_channel.id
        if log_join_ping_role is not None:
            wc.log_join_ping_role_id = log_join_ping_role.id

        # Content
        if join_title is not None:
            wc.join_title = join_title
        if join_message is not None:
            wc.join_message = join_message
        if join_image_url is not None:
            wc.join_image_url = join_image_url or None
        if leave_title is not None:
            wc.leave_title = leave_title
        if leave_message is not None:
            wc.leave_message = leave_message
        if leave_image_url is not None:
            wc.leave_image_url = leave_image_url or None

        # Auto-role
        if autorole_on is not None:
            wc.autorole_on = autorole_on
        if autorole_role is not None:
            wc.autorole_role_id = autorole_role.id
        if autorole_ignore_bots is not None:
            wc.autorole_ignore_bots = autorole_ignore_bots

        await self.store.save()

        parts = []
        if wc.join_channel_id:
            parts.append(f"Join → <#{wc.join_channel_id}>")
        if wc.leave_channel_id:
            parts.append(f"Leave → <#{wc.leave_channel_id}>")
        parts.append(f"Log Join: {'ON' if wc.log_join else 'OFF'}" + (f" → <#{wc.log_join_channel_id}>" if wc.log_join_channel_id else ""))
        parts.append(f"Log Leave: {'ON' if wc.log_leave else 'OFF'}" + (f" → <#{wc.log_leave_channel_id}>" if wc.log_leave_channel_id else ""))
        parts.append(
            f"Auto-Role: {'ON' if wc.autorole_on else 'OFF'}"
            + (f" → <@&{wc.autorole_role_id}>" if wc.autorole_role_id else "")
            + (", bots ignored" if wc.autorole_ignore_bots else ", bots included")
        )

        await interaction.response.send_message("✅ Saved. " + " | ".join(parts), ephemeral=True)

    @welcome.command(name="show", description="Show the current welcome/leave configuration")
    async def welcome_show(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
    
        guild = interaction.guild
        wc = self.store.cfg(guild.id).welcome
    
        jch = guild.get_channel(wc.join_channel_id or wc.channel_id) if (wc.join_channel_id or wc.channel_id) else None
        lch = guild.get_channel(wc.leave_channel_id or wc.channel_id) if (wc.leave_channel_id or wc.channel_id) else None
        ljch = guild.get_channel(wc.log_join_channel_id) if wc.log_join_channel_id else None
        llch = guild.get_channel(wc.log_leave_channel_id) if wc.log_leave_channel_id else None
        pr = guild.get_role(wc.log_join_ping_role_id) if getattr(wc, "log_join_ping_role_id", None) else None  # <-- separate line
    
        desc = [
            "**Channels**",
            f"• **Join embeds:** {jch.mention if isinstance(jch, discord.TextChannel) else '*Not set*'}",
            f"• **Leave embeds:** {lch.mention if isinstance(lch, discord.TextChannel) else '*Not set*'}",
            "",
            "**Logging** (basic text, no embeds)",
            f"• **Join logs:** {'ON' if wc.log_join else 'OFF'}"
            + (f" → {ljch.mention}" if isinstance(ljch, discord.TextChannel) else (" → *join channel*" if wc.log_join else "")),
            f"• **Leave logs:** {'ON' if wc.log_leave else 'OFF'}"
            + (f" → {llch.mention}" if isinstance(llch, discord.TextChannel) else (" → *leave channel*" if wc.log_leave else "")),
            f"• **Join log ping:** {pr.mention if pr else '*None*'}",  # <-- now it's a separate list item
            "",
            "**Welcome (join) message**",
            f"• **Title:** {wc.join_title}",
            f"• **Message:** {wc.join_message}",
            f"• **Image:** {wc.join_image_url or '*None*'}",
            "",
            "**Leave message**",
            f"• **Title:** {wc.leave_title}",
            f"• **Message:** {wc.leave_message}",
            f"• **Image:** {wc.leave_image_url or '*None*'}",
            "",
            "_Placeholders: {mention}, {member}, {name}, {guild}, {count}, {id}_",
        ]
    
        embed = discord.Embed(
            title="Welcome/Leave Configuration",
            description="\n".join(desc),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# -----------------------------
# Extension setup
# -----------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(Turtle(bot))
