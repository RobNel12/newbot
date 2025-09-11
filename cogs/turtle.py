# -*- coding: utf-8 -*-
"""
Reaction Roles + Welcome/Leave Cog
- Keeps your Reaction Roles functionality
- Adds configurable Welcome/Leave embeds with /welcome commands and testing
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

STORAGE_PATH = os.path.join("data", "reaction_roles.json")

# --------------------------- Data Models & Store ---------------------------

@dataclass
class WelcomeConfig:
    # Common
    channel_id: Optional[int] = None

    # Join
    join_title: str = "Welcome to {guild}!"
    join_message: str = "Hey {member}, you’re member #{count}! Make yourself at home."
    join_image_url: Optional[str] = None

    # Leave
    leave_title: str = "Goodbye, {name}"
    leave_message: str = "{name} has left {guild}. We’re now {count} strong."
    leave_image_url: Optional[str] = None

@dataclass
class GuildConfig:
    # Existing reaction roles mapping
    reaction_roles: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # New welcome/leave configuration
    welcome: WelcomeConfig = field(default_factory=WelcomeConfig)

    # ---- Reaction-roles helpers ----
    def set_mapping(self, message_id: int, emoji_str: str, role_id: int) -> None:
        self.reaction_roles.setdefault(str(message_id), {})[emoji_str] = role_id

    def remove_mapping(self, message_id: int, emoji_str: str) -> bool:
        key = str(message_id)
        if key not in self.reaction_roles:
            return False
        removed = self.reaction_roles[key].pop(emoji_str, None)
        if not self.reaction_roles[key]:
            self.reaction_roles.pop(key, None)
        return removed is not None

    def get_for_message(self, message_id: int) -> Dict[str, int]:
        return self.reaction_roles.get(str(message_id), {})

class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self.guilds: Dict[int, GuildConfig] = {}

    def _ensure_dirs(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def load(self) -> None:
        self._ensure_dirs()
        if not os.path.exists(self.path):
            self.guilds = {}
            return
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Backward/forward compatible load
        converted: Dict[int, GuildConfig] = {}
        for gid_str, cfg in raw.items():
            gid = int(gid_str)
            rr = cfg.get("reaction_roles", {})
            w_raw = cfg.get("welcome", {})
            welcome = WelcomeConfig(**w_raw) if isinstance(w_raw, dict) else WelcomeConfig()
            converted[gid] = GuildConfig(reaction_roles=rr, welcome=welcome)
        self.guilds = converted

    async def save(self) -> None:
        self._ensure_dirs()
        # Dataclasses to dicts (and ensure keys are strings for JSON)
        raw = {}
        for gid, cfg in self.guilds.items():
            raw[str(gid)] = {
                "reaction_roles": cfg.reaction_roles,
                "welcome": asdict(cfg.welcome),
            }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def cfg(self, guild_id: int) -> GuildConfig:
        if guild_id not in self.guilds:
            self.guilds[guild_id] = GuildConfig()
        return self.guilds[guild_id]

# --------------------------- Cog ---------------------------

class ReactionRoles(commands.Cog):
    # Existing slash group
    reactionroles = app_commands.Group(name="reactionroles", description="Manage reaction role messages")
    # New slash group
    welcome = app_commands.Group(name="welcome", description="Configure welcome/leave messages")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = Store(STORAGE_PATH)
        self.store.load()

    # ======== Utility (shared) ========

    @staticmethod
    def _norm_emoji(emoji: str) -> str:
        try:
            pe = discord.PartialEmoji.from_str(emoji)
            return str(pe)
        except Exception:
            return emoji.strip()

    async def _fetch_message(self, channel: discord.TextChannel, message_id: int) -> Optional[discord.Message]:
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound:
            return None

    def _rr_lines(self, guild: discord.Guild, cfg: GuildConfig, message_id: int) -> str:
        mappings = cfg.get_for_message(message_id)
        if not mappings:
            return "*(No emoji → role mappings yet. Use `/reactionroles add`.)*"
        lines = []
        for emoji_str, role_id in mappings.items():
            role = guild.get_role(role_id)
            if role:
                lines.append(f"{emoji_str} ⟶ {role.mention} (**{role.name}**)")
            else:
                lines.append(f"{emoji_str} ⟶ **Deleted Role** (`{role_id}`)")
        text = "\n".join(lines)
        return (text[:1019] + "…") if len(text) > 1024 else text

    async def _update_rr_embed_view(self, guild: discord.Guild, message: discord.Message, cfg: GuildConfig) -> None:
        base = message.embeds[0] if message.embeds else discord.Embed(
            title="Reaction Roles", description="React to this message to get roles.", color=discord.Color.blurple()
        )
        new = discord.Embed(title=base.title, description=base.description, color=base.color or discord.Color.blurple())
        for f in base.fields:
            if f.name.strip().lower() != "react for roles":
                new.add_field(name=f.name, value=f.value, inline=f.inline)
        new.add_field(name="React for Roles", value=self._rr_lines(guild, cfg, message.id), inline=False)
        await message.edit(embed=new)

    async def _ensure_reaction(self, message: discord.Message, emoji_str: str) -> None:
        try:
            await message.add_reaction(discord.PartialEmoji.from_str(emoji_str))
        except Exception:
            pass

    # ======== Welcome/Leave helpers ========

    def _format(self, template: str, member: discord.abc.User, guild: discord.Guild) -> str:
        # Safely format placeholders
        values = {
            "member": member.mention,
            "name": getattr(member, "display_name", member.name),
            "guild": guild.name,
            "count": guild.member_count,
        }
        try:
            return template.format(**values)
        except Exception:
            # If user provides malformed braces, fall back to raw
            return template

    def _build_embed(self, title: str, message: str, image_url: Optional[str], member: discord.abc.User, guild: discord.Guild) -> discord.Embed:
        embed = discord.Embed(
            title=self._format(title, member, guild),
            description=self._format(message, member, guild),
            color=discord.Color.blurple(),
        )
        if image_url:
            embed.set_image(url=image_url)
        embed.set_thumbnail(url=member.display_avatar.url if hasattr(member, "display_avatar") else discord.Embed.Empty)
        return embed

    async def _send_welcome(self, member: discord.Member) -> None:
        guild = member.guild
        cfg = self.store.cfg(guild.id).welcome
        if not cfg.channel_id:
            return
        channel = guild.get_channel(cfg.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        embed = self._build_embed(cfg.join_title, cfg.join_message, cfg.join_image_url, member, guild)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    async def _send_leave(self, member: discord.Member) -> None:
        guild = member.guild
        cfg = self.store.cfg(guild.id).welcome
        if not cfg.channel_id:
            return
        channel = guild.get_channel(cfg.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        embed = self._build_embed(cfg.leave_title, cfg.leave_message, cfg.leave_image_url, member, guild)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    # ======== Slash Commands: Reaction Roles (unchanged) ========

    @reactionroles.command(name="create", description="Create a new reaction-role embed")
    async def rr_create(self, interaction: discord.Interaction, channel: discord.TextChannel, title: str, description: str):
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        embed.add_field(name="React for Roles", value="*(No emoji → role mappings yet. Use `/reactionroles add`.)*", inline=False)
        await channel.send(embed=embed)
        await interaction.response.send_message("✅ Created reaction-role message.", ephemeral=True)

    @reactionroles.command(name="add", description="Map an emoji to a role")
    async def rr_add(self, interaction: discord.Interaction, channel: discord.TextChannel, message_id: str, emoji: str, role: discord.Role):
        guild = interaction.guild
        mid = int(message_id)
        message = await self._fetch_message(channel, mid)
        if not message:
            return await interaction.response.send_message("❌ Message not found.", ephemeral=True)
        emoji_key = self._norm_emoji(emoji)
        cfg = self.store.cfg(guild.id)
        cfg.set_mapping(mid, emoji_key, role.id)
        await self.store.save()
        await self._ensure_reaction(message, emoji_key)
        await self._update_rr_embed_view(guild, message, cfg)
        await interaction.response.send_message(f"✅ Mapped {emoji} to {role.name}.", ephemeral=True)

    @reactionroles.command(name="remove", description="Remove an emoji mapping")
    async def rr_remove(self, interaction: discord.Interaction, channel: discord.TextChannel, message_id: str, emoji: str):
        guild = interaction.guild
        mid = int(message_id)
        emoji_key = self._norm_emoji(emoji)
        cfg = self.store.cfg(guild.id)
        ok = cfg.remove_mapping(mid, emoji_key)
        await self.store.save()
        message = await self._fetch_message(channel, mid)
        if message:
            await self._update_rr_embed_view(guild, message, cfg)
        if ok:
            await interaction.response.send_message(f"✅ Removed mapping for {emoji}.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ No mapping found.", ephemeral=True)

    @reactionroles.command(name="list", description="List mappings for a message")
    async def rr_list(self, interaction: discord.Interaction, channel: discord.TextChannel, message_id: str):
        guild = interaction.guild
        mid = int(message_id)
        cfg = self.store.cfg(guild.id)
        text = self._rr_lines(guild, cfg, mid)
        embed = discord.Embed(title="Reaction-role Mappings", description=text, color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ======== Slash Commands: Welcome/Leave ========

    @welcome.command(name="set", description="Configure welcome (join) & leave channel and messages")
    @app_commands.describe(
        channel="Channel for welcome/leave messages",
        join_title="Title for welcome embed (supports {member}, {name}, {guild}, {count})",
        join_message="Body for welcome embed (supports placeholders)",
        join_image_url="Image URL for welcome embed (optional)",
        leave_title="Title for leave embed (supports placeholders)",
        leave_message="Body for leave embed (supports placeholders)",
        leave_image_url="Image URL for leave embed (optional)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_set(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        join_title: Optional[str] = None,
        join_message: Optional[str] = None,
        join_image_url: Optional[str] = None,
        leave_title: Optional[str] = None,
        leave_message: Optional[str] = None,
        leave_image_url: Optional[str] = None,
    ):
        guild = interaction.guild
        gc = self.store.cfg(guild.id)
        wc = gc.welcome

        wc.channel_id = channel.id
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

        await self.store.save()

        await interaction.response.send_message(
            f"✅ Welcome/Leave settings saved for {channel.mention}.",
            ephemeral=True,
        )

    @welcome.command(name="show", description="Show the current welcome/leave configuration")
    async def welcome_show(self, interaction: discord.Interaction):
        guild = interaction.guild
        wc = self.store.cfg(guild.id).welcome
        ch = guild.get_channel(wc.channel_id) if wc.channel_id else None

        desc = [
            f"**Channel:** {ch.mention if isinstance(ch, discord.TextChannel) else '*Not set*'}",
            "",
            "**Welcome (join)**",
            f"• **Title:** {wc.join_title}",
            f"• **Message:** {wc.join_message}",
            f"• **Image:** {wc.join_image_url or '*None*'}",
            "",
            "**Leave**",
            f"• **Title:** {wc.leave_title}",
            f"• **Message:** {wc.leave_message}",
            f"• **Image:** {wc.leave_image_url or '*None*'}",
        ]
        embed = discord.Embed(
            title="Welcome/Leave Configuration",
            description="\n".join(desc),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @welcome.command(name="test", description="Send a test welcome and leave message to the configured channel")
    async def welcome_test(self, interaction: discord.Interaction):
        guild = interaction.guild
        wc = self.store.cfg(guild.id).welcome
        if not wc.channel_id:
            return await interaction.response.send_message("⚠️ No channel configured. Use `/welcome set` first.", ephemeral=True)

        # Use the command invoker as the target for preview
        member = guild.get_member(interaction.user.id)
        if not member:
            # Fallback to user if not cached as Member (DM/testing edge cases)
            member = interaction.user  # type: ignore

        # Send both join and leave previews
        ch = guild.get_channel(wc.channel_id)
        if isinstance(ch, discord.TextChannel):
            join_embed = self._build_embed(wc.join_title, wc.join_message, wc.join_image_url, member, guild)
            leave_embed = self._build_embed(wc.leave_title, wc.leave_message, wc.leave_image_url, member, guild)
            try:
                await ch.send(content="**[TEST]** Welcome preview:", embed=join_embed)
                await ch.send(content="**[TEST]** Leave preview:", embed=leave_embed)
                await interaction.response.send_message("✅ Sent test welcome & leave messages.", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message("❌ I don't have permission to send messages in the configured channel.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Configured channel is invalid. Set it again with `/welcome set`.", ephemeral=True)

    # ======== Event Listeners: Welcome/Leave ========

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._send_welcome(member)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        # Note: member.guild.member_count already reflects the updated count after removal
        await self._send_leave(member)

# --------------------------- Setup ---------------------------

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReactionRoles(bot))