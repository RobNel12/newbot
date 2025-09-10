# -*- coding: utf-8 -*-
"""
Reaction Roles Cog (rewritten, fixed group reference)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

STORAGE_PATH = os.path.join("data", "reaction_roles.json")

@dataclass
class GuildConfig:
    reaction_roles: Dict[str, Dict[str, int]] = field(default_factory=dict)

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
        self.guilds = {int(gid): GuildConfig(**cfg) for gid, cfg in raw.items()}

    async def save(self) -> None:
        self._ensure_dirs()
        raw = {gid: cfg.__dict__ for gid, cfg in self.guilds.items()}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def cfg(self, guild_id: int) -> GuildConfig:
        if guild_id not in self.guilds:
            self.guilds[guild_id] = GuildConfig()
        return self.guilds[guild_id]

class ReactionRoles(commands.Cog):
    # Define the slash group as a CLASS ATTRIBUTE so it exists at class definition time
    reactionroles = app_commands.Group(name="reactionroles", description="Manage reaction role messages")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = Store(STORAGE_PATH)
        self.store.load()

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
                lines.append(f"{emoji_str} ➜ {role.mention} (**{role.name}**)")
            else:
                lines.append(f"{emoji_str} ➜ **Deleted Role** (`{role_id}`)")
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

    # --------------------------- Slash Commands (under /reactionroles) ---------------------------
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

    # --------------------------- Reaction Listeners ---------------------------
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild or not payload.member or payload.member.bot:
            return
        mapping = self.store.cfg(guild.id).get_for_message(payload.message_id)
        role_id = mapping.get(self._norm_emoji(str(payload.emoji)))
        if role_id:
            role = guild.get_role(role_id)
            if role:
                try:
                    await payload.member.add_roles(role)
                except discord.Forbidden:
                    pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        if not member or member.bot:
            return
        mapping = self.store.cfg(guild.id).get_for_message(payload.message_id)
        role_id = mapping.get(self._norm_emoji(str(payload.emoji)))
        if role_id:
            role = guild.get_role(role_id)
            if role:
                try:
                    await member.remove_roles(role)
                except discord.Forbidden:
                    pass

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReactionRoles(bot))
