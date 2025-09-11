# cogs/reaction_roles.py
# Requires discord.py 2.x
from __future__ import annotations

import json
import os
from typing import Dict, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

RR_PATH = "reaction_roles.json"


def _load_rr() -> Dict[str, Any]:
    if os.path.exists(RR_PATH):
        try:
            with open(RR_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_rr(data: Dict[str, Any]) -> None:
    with open(RR_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _emoji_key(emoji: discord.PartialEmoji | str) -> str:
    # Store unicode as the char, custom as <:name:id>
    if isinstance(emoji, discord.PartialEmoji):
        return emoji.name if emoji.id is None else f"<:{emoji.name}:{emoji.id}>"
    return str(emoji)


class ReactionRoles(commands.Cog):
    """Basic reaction roles with raw reaction events and simple JSON storage."""

    rr = app_commands.Group(name="reactionroles", description="Manage reaction roles")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.data: Dict[str, Any] = _load_rr()

    # ------------------ helpers ------------------

    def _guild(self, guild_id: int) -> Dict[str, Any]:
        g = self.data.get(str(guild_id))
        if not g:
            g = {"messages": {}}  # message_id -> {emoji_key: role_id}
            self.data[str(guild_id)] = g
        return g

    def _msgmap(self, guild_id: int, message_id: int) -> Dict[str, int]:
        g = self._guild(guild_id)
        msgs = g["messages"]
        m = msgs.get(str(message_id))
        if not m:
            m = {}
            msgs[str(message_id)] = m
        return m

    async def _resolve_role(self, guild: discord.Guild, role_id: int) -> Optional[discord.Role]:
        return guild.get_role(role_id)

    # ------------------ events ------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.member is None:
            return
        if payload.member.bot:
            return

        g = self.data.get(str(payload.guild_id))
        if not g:
            return

        msgcfg = g.get("messages", {}).get(str(payload.message_id))
        if not msgcfg:
            return

        emoji_k = _emoji_key(payload.emoji)
        role_id = msgcfg.get(emoji_k)
        if not role_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role = guild.get_role(int(role_id))
        if role is None:
            return

        me = guild.me
        if not me or not me.guild_permissions.manage_roles or role >= me.top_role:
            return

        try:
            await payload.member.add_roles(role, reason="Reaction roles: add")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return

        g = self.data.get(str(payload.guild_id))
        if not g:
            return

        msgcfg = g.get("messages", {}).get(str(payload.message_id))
        if not msgcfg:
            return

        emoji_k = _emoji_key(payload.emoji)
        role_id = msgcfg.get(emoji_k)
        if not role_id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role = guild.get_role(int(role_id))
        if role is None:
            return

        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception:
                return

        if member.bot:
            return

        me = guild.me
        if not me or not me.guild_permissions.manage_roles or role >= me.top_role:
            return

        try:
            await member.remove_roles(role, reason="Reaction roles: remove")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    # ------------------ commands ------------------

    @rr.command(name="create", description="Post a reaction-roles message here and (optionally) seed it with emoji → role pairs")
    @app_commands.describe(
        title="Top line of the embed",
        description="Body of the embed",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rr_create(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
    ):
        if interaction.guild is None or interaction.channel is None:
            return await interaction.response.send_message("Use this in a server text channel.", ephemeral=True)

        embed = discord.Embed(title=title, description=description, color=discord.Color.gold())
        await interaction.response.defer(ephemeral=True)
        msg = await interaction.channel.send(embed=embed)

        # Track this message (empty mapping for now)
        self._msgmap(interaction.guild.id, msg.id)
        _save_rr(self.data)

        await interaction.followup.send(f"✅ Reaction-roles message created: {msg.jump_url}\nUse `/reactionroles bind` to add emoji → role.", ephemeral=True)

    @rr.command(name="bind", description="Bind an emoji to a role on an existing message")
    @app_commands.describe(
        message_id="Target message ID (right-click → Copy Message Link; the last digits are the ID)",
        emoji="Emoji (unicode like 😀 or custom like <:name:id>)",
        role="Role to grant/remove when users react",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rr_bind(
        self,
        interaction: discord.Interaction,
        message_id: str,
        emoji: str,
        role: discord.Role,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message("Invalid message ID.", ephemeral=True)

        # Persist mapping
        mapping = self._msgmap(interaction.guild.id, mid)
        mapping[_emoji_key(emoji)] = role.id
        _save_rr(self.data)

        # Add the reaction to that message if possible (best-effort)
        try:
            channel = interaction.channel
            if channel and isinstance(channel, (discord.TextChannel, discord.Thread)):
                try:
                    msg = await channel.fetch_message(mid)
                    await msg.add_reaction(emoji)
                except Exception:
                    pass
        except Exception:
            pass

        await interaction.response.send_message(f"✅ Bound {emoji} → {role.mention} on message `{mid}`.", ephemeral=True)

    @rr.command(name="list", description="List emoji → role bindings for a message")
    @app_commands.describe(message_id="Message ID to inspect")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rr_list(self, interaction: discord.Interaction, message_id: str):
        if interaction.guild is None:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message("Invalid message ID.", ephemeral=True)

        g = self._guild(interaction.guild.id)
        m = g["messages"].get(str(mid))
        if not m:
            return await interaction.response.send_message("No bindings for that message.", ephemeral=True)

        lines = [f"**Message `{mid}` mappings:**"]
        for ek, rid in m.items():
            role = interaction.guild.get_role(int(rid))
            lines.append(f"• {ek} → {role.mention if role else f'<@&{rid}>'}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @rr.command(name="unbind", description="Remove an emoji binding from a message")
    @app_commands.describe(message_id="Message ID", emoji="Emoji to unbind")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rr_unbind(self, interaction: discord.Interaction, message_id: str, emoji: str):
        if interaction.guild is None:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message("Invalid message ID.", ephemeral=True)

        g = self._guild(interaction.guild.id)
        m = g["messages"].get(str(mid))
        if not m or _emoji_key(emoji) not in m:
            return await interaction.response.send_message("That emoji isn’t bound on that message.", ephemeral=True)

        del m[_emoji_key(emoji)]
        _save_rr(self.data)
        await interaction.response.send_message(f"✅ Unbound {emoji} from message `{mid}`.", ephemeral=True)

    @rr.command(name="remove", description="Stop tracking a reaction-roles message (does not delete the message)")
    @app_commands.describe(message_id="Message ID to stop tracking")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rr_remove(self, interaction: discord.Interaction, message_id: str):
        if interaction.guild is None:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        g = self._guild(interaction.guild.id)
        if message_id in g.get("messages", {}):
            del g["messages"][message_id]
            _save_rr(self.data)
            return await interaction.response.send_message(f"✅ No longer tracking message `{message_id}`.", ephemeral=True)

        await interaction.response.send_message("That message isn’t tracked.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))