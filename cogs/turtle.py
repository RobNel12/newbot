# cogs/greet_roles.py
from __future__ import annotations

import asyncio
import json
import os
from typing import Dict, Any, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

CONFIG_PATH = "data/greetrole_config.json"


def ensure_data_file():
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f)


class GuildConfig:
    """Convenience wrapper around a dict for typed access."""
    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self.data = data or {}

    @property
    def welcome_enabled(self) -> bool:
        return bool(self.data.get("welcome_enabled", True))

    @welcome_enabled.setter
    def welcome_enabled(self, v: bool):
        self.data["welcome_enabled"] = v

    @property
    def welcome_channel_id(self) -> Optional[int]:
        v = self.data.get("welcome_channel_id")
        return int(v) if v else None

    @welcome_channel_id.setter
    def welcome_channel_id(self, v: Optional[int]):
        self.data["welcome_channel_id"] = int(v) if v else None

    @property
    def welcome_message(self) -> str:
        # Supports {member}, {guild}, {mention}, {count}
        return self.data.get(
            "welcome_message",
            "👋 Welcome {mention} to **{guild}**! You’re member #{count}."
        )

    @welcome_message.setter
    def welcome_message(self, v: str):
        self.data["welcome_message"] = str(v)

    @property
    def leave_enabled(self) -> bool:
        return bool(self.data.get("leave_enabled", True))

    @leave_enabled.setter
    def leave_enabled(self, v: bool):
        self.data["leave_enabled"] = v

    @property
    def leave_channel_id(self) -> Optional[int]:
        v = self.data.get("leave_channel_id")
        return int(v) if v else None

    @leave_channel_id.setter
    def leave_channel_id(self, v: Optional[int]):
        self.data["leave_channel_id"] = int(v) if v else None

    @property
    def leave_message(self) -> str:
        # Supports {member}, {guild}, {count}
        return self.data.get(
            "leave_message",
            "👋 {member} has left **{guild}**. We’re now {count} strong."
        )

    @leave_message.setter
    def leave_message(self, v: str):
        self.data["leave_message"] = str(v)

    @property
    def autorole_id(self) -> Optional[int]:
        v = self.data.get("autorole_id")
        return int(v) if v else None

    @autorole_id.setter
    def autorole_id(self, v: Optional[int]):
        self.data["autorole_id"] = int(v) if v else None

    @property
    def reaction_roles(self) -> Dict[str, Dict[str, int]]:
        """
        Mapping:
        {
          "message_id": {
              "emoji_str": role_id,
              ...
          },
          ...
        }
        """
        return self.data.setdefault("reaction_roles", {})

    def set_reaction_role(self, message_id: int, emoji_str: str, role_id: int):
        msg = self.reaction_roles.setdefault(str(message_id), {})
        msg[emoji_str] = int(role_id)

    def remove_reaction_role(self, message_id: int, emoji_str: str) -> bool:
        msg = self.reaction_roles.get(str(message_id))
        if not msg:
            return False
        if emoji_str in msg:
            del msg[emoji_str]
            if not msg:
                del self.reaction_roles[str(message_id)]
            return True
        return False

    def get_role_for_reaction(self, message_id: int, emoji_str: str) -> Optional[int]:
        return self.reaction_roles.get(str(message_id), {}).get(emoji_str)


class GreetRoles(commands.Cog):
    """Greetings, Leaves, Auto-role, and Reaction Roles with slash commands & menus."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        ensure_data_file()
        self._cfg_lock = asyncio.Lock()
        self._cache: Dict[int, GuildConfig] = {}
        self._load_all()

    # ---------- Persistence ----------
    def _load_all(self):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)  # {guild_id: data}
        for gid, data in raw.items():
            self._cache[int(gid)] = GuildConfig(data)

    async def _save(self):
        async with self._cfg_lock:
            raw = {str(gid): cfg.data for gid, cfg in self._cache.items()}
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2)

    def _get_cfg(self, guild_id: int) -> GuildConfig:
        if guild_id not in self._cache:
            self._cache[guild_id] = GuildConfig()
        return self._cache[guild_id]

    # ---------- Helpers ----------
    @staticmethod
    def _fmt(msg: str, member: Optional[discord.Member] = None) -> str:
        if member and member.guild:
            safe = msg.format(
                member=str(member),
                guild=member.guild.name,
                mention=member.mention if member else "",
                count=member.guild.member_count if member else 0,
            )
            return safe
        return msg

    @staticmethod
    def _embed(title: str, desc: str) -> discord.Embed:
        e = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
        return e

    # ---------- Listeners ----------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = self._get_cfg(member.guild.id)
        # Auto-role
        if cfg.autorole_id:
            role = member.guild.get_role(cfg.autorole_id)
            if role and member.guild.me and role < member.guild.me.top_role:
                try:
                    await member.add_roles(role, reason="Auto-role on join")
                except discord.Forbidden:
                    pass

        # Welcome Embed
        if cfg.welcome_enabled and cfg.welcome_channel_id:
            channel = member.guild.get_channel(cfg.welcome_channel_id)
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                try:
                    desc = self._fmt(cfg.welcome_message, member)
                    await channel.send(embed=self._embed("Welcome!", desc))
                except discord.Forbidden:
                    pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.guild is None:
            return
        cfg = self._get_cfg(member.guild.id)
        if cfg.leave_enabled and cfg.leave_channel_id:
            channel = member.guild.get_channel(cfg.leave_channel_id)
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                try:
                    # member might be a PartialMember; format safely
                    desc = cfg.leave_message.format(
                        member=str(member),
                        guild=member.guild.name,
                        count=member.guild.member_count,
                    )
                    await channel.send(embed=self._embed("Farewell!", desc))
                except discord.Forbidden:
                    pass

    # Use RAW events to handle uncached messages
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or not self.bot.user or payload.user_id == self.bot.user.id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        cfg = self._get_cfg(guild.id)
        emoji_str = str(payload.emoji)
        role_id = cfg.get_role_for_reaction(payload.message_id, emoji_str)
        if not role_id:
            return
        role = guild.get_role(role_id)
        if not role:
            return
        member = guild.get_member(payload.user_id)
        if not member or member.bot:
            return
        try:
            await member.add_roles(role, reason=f"Reaction role via {emoji_str}")
        except discord.Forbidden:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        cfg = self._get_cfg(guild.id)
        emoji_str = str(payload.emoji)
        role_id = cfg.get_role_for_reaction(payload.message_id, emoji_str)
        if not role_id:
            return
        role = guild.get_role(role_id)
        if not role:
            return
        member = guild.get_member(payload.user_id)
        if not member or member.bot:
            return
        try:
            await member.remove_roles(role, reason=f"Reaction role removed via {emoji_str}")
        except discord.Forbidden:
            pass

    # ---------- Slash Command Group: /welcome ----------
    welcome = app_commands.Group(name="welcome", description="Configure welcome messages")

    @welcome.command(name="set_channel", description="Set the channel for welcome messages.")
    @app_commands.describe(channel="Channel to send welcome embeds to")
    @app_commands.default_permissions(manage_guild=True)
    async def welcome_set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = self._get_cfg(interaction.guild_id)
        cfg.welcome_channel_id = channel.id
        await self._save()
        await interaction.response.send_message(f"✅ Welcome channel set to {channel.mention}", ephemeral=True)

    @welcome.command(name="set_message", description="Set the welcome message template.")
    @app_commands.describe(message="Use placeholders: {member}, {mention}, {guild}, {count}")
    @app_commands.default_permissions(manage_guild=True)
    async def welcome_set_message(self, interaction: discord.Interaction, message: str):
        cfg = self._get_cfg(interaction.guild_id)
        cfg.welcome_message = message
        await self._save()
        await interaction.response.send_message("✅ Welcome message updated.", ephemeral=True)

    @welcome.command(name="toggle", description="Enable/disable welcome messages.")
    @app_commands.describe(enabled="Turn welcome messages on or off")
    @app_commands.default_permissions(manage_guild=True)
    async def welcome_toggle(self, interaction: discord.Interaction, enabled: bool):
        cfg = self._get_cfg(interaction.guild_id)
        cfg.welcome_enabled = enabled
        await self._save()
        await interaction.response.send_message(f"✅ Welcome messages {'enabled' if enabled else 'disabled'}.", ephemeral=True)

    @welcome.command(name="test", description="Send a test welcome embed to the configured channel.")
    @app_commands.default_permissions(manage_guild=True)
    async def welcome_test(self, interaction: discord.Interaction):
        cfg = self._get_cfg(interaction.guild_id)
        ch = interaction.guild.get_channel(cfg.welcome_channel_id) if cfg.welcome_channel_id else None
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("⚠️ Welcome channel not set.", ephemeral=True)
        desc = self._fmt(cfg.welcome_message, interaction.user if isinstance(interaction.user, discord.Member) else None)
        await ch.send(embed=self._embed("Welcome!", desc))
        await interaction.response.send_message("✅ Sent test welcome embed.", ephemeral=True)

    # ---------- Slash Command Group: /leave ----------
    leave = app_commands.Group(name="leave", description="Configure leave messages")

    @leave.command(name="set_channel", description="Set the channel for leave messages.")
    @app_commands.describe(channel="Channel to send leave embeds to")
    @app_commands.default_permissions(manage_guild=True)
    async def leave_set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        cfg = self._get_cfg(interaction.guild_id)
        cfg.leave_channel_id = channel.id
        await self._save()
        await interaction.response.send_message(f"✅ Leave channel set to {channel.mention}", ephemeral=True)

    @leave.command(name="set_message", description="Set the leave message template.")
    @app_commands.describe(message="Use placeholders: {member}, {guild}, {count}")
    @app_commands.default_permissions(manage_guild=True)
    async def leave_set_message(self, interaction: discord.Interaction, message: str):
        cfg = self._get_cfg(interaction.guild_id)
        cfg.leave_message = message
        await self._save()
        await interaction.response.send_message("✅ Leave message updated.", ephemeral=True)

    @leave.command(name="toggle", description="Enable/disable leave messages.")
    @app_commands.describe(enabled="Turn leave messages on or off")
    @app_commands.default_permissions(manage_guild=True)
    async def leave_toggle(self, interaction: discord.Interaction, enabled: bool):
        cfg = self._get_cfg(interaction.guild_id)
        cfg.leave_enabled = enabled
        await self._save()
        await interaction.response.send_message(f"✅ Leave messages {'enabled' if enabled else 'disabled'}.", ephemeral=True)

    # ---------- Slash Command Group: /autorole ----------
    autorole = app_commands.Group(name="autorole", description="Configure auto-assign role on member join")

    @autorole.command(name="set", description="Set the role to automatically assign on join.")
    @app_commands.describe(role="Role to assign to new members")
    @app_commands.default_permissions(manage_guild=True)
    async def autorole_set(self, interaction: discord.Interaction, role: discord.Role):
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("❌ That role is higher than my top role.", ephemeral=True)
        cfg = self._get_cfg(interaction.guild_id)
        cfg.autorole_id = role.id
        await self._save()
        await interaction.response.send_message(f"✅ Auto-role set to **{role.name}**.", ephemeral=True)

    @autorole.command(name="clear", description="Clear the auto-role.")
    @app_commands.default_permissions(manage_guild=True)
    async def autorole_clear(self, interaction: discord.Interaction):
        cfg = self._get_cfg(interaction.guild_id)
        cfg.autorole_id = None
        await self._save()
        await interaction.response.send_message("✅ Auto-role cleared.", ephemeral=True)

    # ---------- Slash Command Group: /reactionroles ----------
    rr = app_commands.Group(name="reactionroles", description="Create and manage reaction-role messages")

    @rr.command(name="create", description="Create a new reaction-role embed and post it.")
    @app_commands.describe(channel="Channel to post in", title="Embed title", description="Embed description")
    @app_commands.default_permissions(manage_guild=True)
    async def rr_create(self, interaction: discord.Interaction, channel: discord.TextChannel, title: str, description: str):
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        msg = await channel.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Reaction-role message created in {channel.mention} (ID: `{msg.id}`). Use `/reactionroles add` to map emoji ➜ role.",
            ephemeral=True
        )

    @rr.command(name="add", description="Map an emoji to a role for a reaction-role message.")
    @app_commands.describe(message_id="Target message ID", emoji="Emoji (custom or unicode)", role="Role to grant")
    @app_commands.default_permissions(manage_guild=True)
    async def rr_add(self, interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
        # Validate message
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)

        channel, message = await self._fetch_message_in_guild(interaction.guild, mid)
        if not message:
            return await interaction.response.send_message("❌ Could not find that message in this server.", ephemeral=True)

        # Try to add the reaction to ensure it's valid/usable
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            return await interaction.response.send_message("❌ I couldn't add that emoji as a reaction.", ephemeral=True)

        cfg = self._get_cfg(interaction.guild_id)
        cfg.set_reaction_role(mid, str(discord.PartialEmoji.from_str(emoji)), role.id)
        await self._save()

        await interaction.response.send_message(
            f"✅ Mapped {emoji} ➜ **{role.name}** on message `{mid}` in {channel.mention}.",
            ephemeral=True
        )

    @rr.command(name="remove", description="Remove an emoji mapping from a reaction-role message.")
    @app_commands.describe(message_id="Target message ID", emoji="Emoji to remove")
    @app_commands.default_permissions(manage_guild=True)
    async def rr_remove(self, interaction: discord.Interaction, message_id: str, emoji: str):
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)

        cfg = self._get_cfg(interaction.guild_id)
        ok = cfg.remove_reaction_role(mid, str(discord.PartialEmoji.from_str(emoji)))
        await self._save()
        if not ok:
            return await interaction.response.send_message("⚠️ No mapping found for that emoji/message.", ephemeral=True)

        await interaction.response.send_message(f"✅ Removed mapping for {emoji} on message `{mid}`.", ephemeral=True)

    @rr.command(name="list", description="List emoji ➜ role mappings for a reaction-role message.")
    @app_commands.describe(message_id="Target message ID")
    @app_commands.default_permissions(manage_guild=True)
    async def rr_list(self, interaction: discord.Interaction, message_id: str):
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)

        cfg = self._get_cfg(interaction.guild_id)
        mappings = cfg.reaction_roles.get(str(mid), {})
        if not mappings:
            return await interaction.response.send_message("ℹ️ No mappings for that message.", ephemeral=True)

        lines = []
        for emoji_str, role_id in mappings.items():
            role = interaction.guild.get_role(role_id)
            rname = role.name if role else f"Deleted Role ({role_id})"
            lines.append(f"{emoji_str} ➜ **{rname}** (`{role_id}`)")
        await interaction.response.send_message(
            embed=self._embed("Reaction-role Mappings", "\n".join(lines)), ephemeral=True
        )

    # ---------- Interactive Config Panel (menus) ----------
    @app_commands.command(name="greetroles_panel", description="Open an interactive config panel (menus & modals).")
    @app_commands.default_permissions(manage_guild=True)
    async def open_panel(self, interaction: discord.Interaction):
        cfg = self._get_cfg(interaction.guild_id)
        view = ConfigPanel(self, cfg)
        await interaction.response.send_message(embed=self._embed("Greet & Roles Panel", "Use the buttons below."), view=view, ephemeral=True)

    # ---------- Utilities ----------
    async def _fetch_message_in_guild(self, guild: discord.Guild, message_id: int) -> Tuple[Optional[discord.TextChannel], Optional[discord.Message]]:
        # Search likely channels quickly; avoids needing message content intent
        for channel in guild.text_channels:
            try:
                msg = await channel.fetch_message(message_id)
                return channel, msg
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        return None, None


# ---------- UI Components ----------
class ConfigPanel(discord.ui.View):
    def __init__(self, cog: GreetRoles, cfg: GuildConfig):
        super().__init__(timeout=300)
        self.cog = cog
        self.cfg = cfg

    @discord.ui.button(label="Set Welcome Channel", style=discord.ButtonStyle.primary)
    async def set_welcome_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetChannelModal(self.cog, self.cfg, kind="welcome"))

    @discord.ui.button(label="Set Welcome Message", style=discord.ButtonStyle.secondary)
    async def set_welcome_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetMessageModal(self.cog, self.cfg, kind="welcome"))

    @discord.ui.button(label="Toggle Welcome", style=discord.ButtonStyle.success)
    async def toggle_welcome(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cfg.welcome_enabled = not self.cfg.welcome_enabled
        await self.cog._save()
        await interaction.response.send_message(f"Welcome is now **{'ENABLED' if self.cfg.welcome_enabled else 'DISABLED'}**.", ephemeral=True)

    @discord.ui.button(label="Set Leave Channel", style=discord.ButtonStyle.primary)
    async def set_leave_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetChannelModal(self.cog, self.cfg, kind="leave"))

    @discord.ui.button(label="Set Leave Message", style=discord.ButtonStyle.secondary)
    async def set_leave_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetMessageModal(self.cog, self.cfg, kind="leave"))

    @discord.ui.button(label="Toggle Leave", style=discord.ButtonStyle.success)
    async def toggle_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cfg.leave_enabled = not self.cfg.leave_enabled
        await self.cog._save()
        await interaction.response.send_message(f"Leave is now **{'ENABLED' if self.cfg.leave_enabled else 'DISABLED'}**.", ephemeral=True)

    # 🚫 Removed placeholder select that had options=[]
    # Use the dynamic picker instead:
   # inside class ConfigPanel(discord.ui.View):

    @discord.ui.button(label="Pick Auto-role", style=discord.ButtonStyle.blurple)
    async def pick_autorole(self, interaction: discord.Interaction, button: discord.ui.Button):
        roles = [
            r for r in interaction.guild.roles
            if not r.managed and r < interaction.guild.me.top_role
        ]
        roles = sorted(roles, key=lambda r: r.position, reverse=True)[:25]
    
        if not roles:
            return await interaction.response.send_message("ℹ️ No eligible roles to select.", ephemeral=True)
    
        options = [discord.SelectOption(label=r.name[:100], value=str(r.id)) for r in roles]
    
        class AutoRoleView(discord.ui.View):
            def __init__(self, cog, cfg):
                super().__init__(timeout=120)
                self.cog = cog
                self.cfg = cfg
    
        class AutoRolePicker(discord.ui.Select):
            def __init__(self):
                super().__init__(placeholder="Choose an auto-role…", min_values=1, max_values=1, options=options)
    
            async def callback(self, itx: discord.Interaction):
                rid = int(self.values[0])
                # save selection on the panel's config
                self.view.cfg.autorole_id = rid
                await self.view.cog._save()
                await itx.response.send_message(f"✅ Auto-role set to <@&{rid}>.", ephemeral=True)
    
        v = AutoRoleView(self.cog, self.cfg)  # <-- carries cfg/cog
        v.add_item(AutoRolePicker())
        await interaction.response.send_message("Select a role:", view=v, ephemeral=True)



class SetChannelModal(discord.ui.Modal, title="Set Channel"):
    channel_id = discord.ui.TextInput(label="Channel ID", placeholder="123456789012345678", required=True)

    def __init__(self, cog: GreetRoles, cfg: GuildConfig, kind: str):
        super().__init__()
        self.cog = cog
        self.cfg = cfg
        self.kind = kind

    async def on_submit(self, interaction: discord.Interaction):
        try:
            cid = int(self.channel_id.value)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid channel ID.", ephemeral=True)
        if self.kind == "welcome":
            self.cfg.welcome_channel_id = cid
            label = "Welcome"
        else:
            self.cfg.leave_channel_id = cid
            label = "Leave"
        await self.cog._save()
        await interaction.response.send_message(f"✅ {label} channel set to <#{cid}>.", ephemeral=True)


class SetMessageModal(discord.ui.Modal, title="Set Message Template"):
    message = discord.ui.TextInput(
        label="Template",
        style=discord.TextStyle.paragraph,
        placeholder="Use {member}, {mention}, {guild}, {count}",
        required=True,
        max_length=1500,
    )

    def __init__(self, cog: GreetRoles, cfg: GuildConfig, kind: str):
        super().__init__()
        self.cog = cog
        self.cfg = cfg
        self.kind = kind

    async def on_submit(self, interaction: discord.Interaction):
        txt = self.message.value
        if self.kind == "welcome":
            self.cfg.welcome_message = txt
            label = "Welcome"
        else:
            self.cfg.leave_message = txt
            label = "Leave"
        await self.cog._save()
        await interaction.response.send_message(f"✅ {label} message updated.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GreetRoles(bot))
