# -*- coding: utf-8 -*-
"""
Reaction Roles Cog (rewritten)

Features
- /reactionroles create: posts an embed with a "React for Roles" field
- /reactionroles add: maps emoji -> role for a specific message and updates the embed
- /reactionroles remove: removes a mapping and updates the embed
- /reactionroles list: shows the mappings exactly as the embed does
- on_raw_reaction_add/on_raw_reaction_remove: grants/removes roles based on mappings

Notes
- Designed for discord.py 2.3+ with app_commands (slash commands)
- Keep your bot intents enabled for members and message content as appropriate.
- Storage is a simple JSON on disk (per-guild mappings)

Install
- Save this file as cogs/reaction_roles.py
- Load with: bot.load_extension("cogs.reaction_roles")
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

STORAGE_PATH = os.path.join("data", "reaction_roles.json")


# --------------------------- Config & Storage ---------------------------
@dataclass
class GuildConfig:
    reaction_roles: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # mapping: message_id (str) -> { emoji_str: role_id }

    def set_mapping(self, message_id: int, emoji_str: str, role_id: int) -> None:
        key = str(message_id)
        if key not in self.reaction_roles:
            self.reaction_roles[key] = {}
        self.reaction_roles[key][emoji_str] = role_id

    def remove_mapping(self, message_id: int, emoji_str: str) -> bool:
        key = str(message_id)
        if key not in self.reaction_roles:
            return False
        removed = self.reaction_roles[key].pop(emoji_str, None)
        # prune empty dicts
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
        self.guilds = {
            int(gid): GuildConfig(**cfg) for gid, cfg in raw.items()
        }

    async def save(self) -> None:
        self._ensure_dirs()
        raw = {gid: cfg.__dict__ for gid, cfg in self.guilds.items()}
        # write atomically
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
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = Store(STORAGE_PATH)
        self.store.load()

        # slash command group
        self.group = app_commands.Group(name="reactionroles", description="Manage reaction role messages")
        self.bot.tree.add_command(self.group)

    # ---------- Utilities ----------
    @staticmethod
    def _norm_emoji(emoji: str) -> str:
        """Return a canonical string for an emoji usable as a key.
        Accepts unicode emoji or custom emoji like <:_name_:id> or :name:id.
        """
        try:
            pe = discord.PartialEmoji.from_str(emoji)
            # PartialEmoji.str gives "<:name:id>" for custom, or the unicode itself
            return str(pe)
        except Exception:
            return emoji.strip()

    async def _fetch_message(self, channel: discord.abc.MessageableChannel, message_id: int) -> Optional[discord.Message]:
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                return await channel.fetch_message(message_id)
            except discord.NotFound:
                return None
        return None

    def _rr_lines(self, guild: discord.Guild, cfg: GuildConfig, message_id: int) -> str:
        mappings = cfg.get_for_message(message_id)
        if not mappings:
            return "*(No emoji → role mappings yet. Use `/reactionroles add`.)*"
        lines = []
        for emoji_str, role_id in mappings.items():
            role = guild.get_role(role_id)
            if role is None:
                lines.append(f"{emoji_str} ➜ **Deleted Role** (`{role_id}`)")
            else:
                lines.append(f"{emoji_str} ➜ {role.mention} (**{role.name}**)")
        text = "\n".join(lines)
        return (text[:1019] + "…") if len(text) > 1024 else text

    async def _update_rr_embed_view(self, guild: discord.Guild, message: discord.Message, cfg: GuildConfig) -> None:
        base = message.embeds[0] if message.embeds else discord.Embed(
            title="Reaction Roles", description="React to this message to get roles.", color=discord.Color.blurple()
        )
        new = discord.Embed(title=base.title, description=base.description, color=base.color or discord.Color.blurple())
        if base.url:
            new.url = base.url
        if base.footer and base.footer.text:
            new.set_footer(text=base.footer.text, icon_url=getattr(base.footer, "icon_url", discord.Embed.Empty))
        if base.author and base.author.name:
            new.set_author(name=base.author.name, icon_url=getattr(base.author, "icon_url", discord.Embed.Empty), url=getattr(base.author, "url", discord.Embed.Empty))
        if base.thumbnail and base.thumbnail.url:
            new.set_thumbnail(url=base.thumbnail.url)
        if base.image and base.image.url:
            new.set_image(url=base.image.url)

        # copy non-RR fields
        for f in base.fields:
            if f.name.strip().lower() != "react for roles":
                new.add_field(name=f.name, value=f.value, inline=f.inline)

        # add/replace the RR field
        new.add_field(name="React for Roles", value=self._rr_lines(guild, cfg, message.id), inline=False)
        await message.edit(embed=new)

    async def _ensure_reaction(self, message: discord.Message, emoji_str: str) -> None:
        """Ensure the message has the given emoji reaction present."""
        try:
            pe = discord.PartialEmoji.from_str(emoji_str)
        except Exception:
            return
        try:
            await message.add_reaction(pe)
        except discord.HTTPException:
            pass

    # --------------------------- Slash Commands ---------------------------
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Channel to post the reaction-role embed in", title="Embed title", description="Embed description")
    @commands.hybrid_command(name="rr_create", with_app_command=False)
    async def rr_create_message(self, ctx: commands.Context, channel: discord.TextChannel, *, title: str = "Reaction Roles", description: str = "React to this message to get roles."):
        """(Text) Create a reaction-role embed message. Alias for the slash command."""
        await self._create_impl(ctx.author, ctx.guild, channel, title, description)

    @self_group_property = property
    def group_prop(self):
        return self.group

    @group_prop.setter
    def group_prop(self, value):
        self.group = value

    @group.command(name="create", description="Create a new reaction-role embed and post it.")
    @app_commands.describe(channel="Channel to post in", title="Embed title", description="Embed description")
    @app_commands.default_permissions(manage_guild=True)
    async def rr_create(self, interaction: discord.Interaction, channel: discord.TextChannel, title: str, description: str):
        await self._create_impl(interaction.user, interaction.guild, channel, title, description)
        await interaction.response.send_message("✅ Created.", ephemeral=True)

    async def _create_impl(self, user: discord.abc.User, guild: discord.Guild, channel: discord.TextChannel, title: str, description: str) -> None:
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        embed.add_field(name="React for Roles", value="*(No emoji → role mappings yet. Use `/reactionroles add`.)*", inline=False)
        await channel.send(embed=embed)

    @group.command(name="add", description="Map an emoji to a role for a specific message and update the embed.")
    @app_commands.describe(channel="Channel containing the message", message_id="ID of the target message", emoji="Emoji (unicode or custom)", role="Role to grant")
    @app_commands.default_permissions(manage_guild=True)
    async def rr_add(self, interaction: discord.Interaction, channel: discord.TextChannel, message_id: str, emoji: str, role: discord.Role):
        assert interaction.guild is not None
        guild = interaction.guild
        mid = int(message_id)

        message = await self._fetch_message(channel, mid)
        if message is None:
            return await interaction.response.send_message("❌ Couldn't find that message in the selected channel.", ephemeral=True)

        emoji_key = self._norm_emoji(emoji)
        cfg = self.store.cfg(guild.id)
        cfg.set_mapping(mid, emoji_key, role.id)
        await self.store.save()

        await self._ensure_reaction(message, emoji_key)
        await self._update_rr_embed_view(guild, message, cfg)

        await interaction.response.send_message(f"✅ Mapped {emoji} ➜ **{role.name}** on message `{mid}`.", ephemeral=True)

    @group.command(name="remove", description="Remove an emoji mapping for a message and update the embed.")
    @app_commands.describe(channel="Channel containing the message", message_id="ID of the target message", emoji="Emoji to unmap")
    @app_commands.default_permissions(manage_guild=True)
    async def rr_remove(self, interaction: discord.Interaction, channel: discord.TextChannel, message_id: str, emoji: str):
        assert interaction.guild is not None
        guild = interaction.guild
        mid = int(message_id)
        emoji_key = self._norm_emoji(emoji)

        cfg = self.store.cfg(guild.id)
        ok = cfg.remove_mapping(mid, emoji_key)
        await self.store.save()
        if not ok:
            return await interaction.response.send_message("⚠️ No mapping found for that emoji/message.", ephemeral=True)

        message = await self._fetch_message(channel, mid)
        if message:
            await self._update_rr_embed_view(guild, message, cfg)

        await interaction.response.send_message(f"✅ Removed mapping for {emoji} on message `{mid}`.", ephemeral=True)

    @group.command(name="list", description="Show the emoji → role mappings for a message.")
    @app_commands.describe(channel="Channel containing the message", message_id="ID of the target message")
    @app_commands.default_permissions(manage_guild=True)
    async def rr_list(self, interaction: discord.Interaction, channel: discord.TextChannel, message_id: str):
        assert interaction.guild is not None
        guild = interaction.guild
        mid = int(message_id)

        cfg = self.store.cfg(guild.id)
        text = self._rr_lines(guild, cfg, mid)
        embed = discord.Embed(title="Reaction-role Mappings", description=text, color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --------------------------- Reaction Listeners ---------------------------
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.member is None:
            return
        if payload.member.bot:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        cfg = self.store.cfg(guild.id)
        mapping = cfg.get_for_message(payload.message_id)
        if not mapping:
            return

        emoji_key = self._norm_emoji(str(payload.emoji))
        role_id = mapping.get(emoji_key)
        if not role_id:
            return
        role = guild.get_role(role_id)
        if role is None:
            return
        try:
            await payload.member.add_roles(role, reason="Reaction role opt-in")
        except discord.Forbidden:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return
        cfg = self.store.cfg(guild.id)
        mapping = cfg.get_for_message(payload.message_id)
        if not mapping:
            return

        emoji_key = self._norm_emoji(str(payload.emoji))
        role_id = mapping.get(emoji_key)
        if not role_id:
            return
        role = guild.get_role(role_id)
        if role is None:
            return
        try:
            await member.remove_roles(role, reason="Reaction role opt-out")
        except discord.Forbidden:
            pass


# --------------------------- Setup ---------------------------
async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReactionRoles(bot))
