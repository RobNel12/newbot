from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict
from typing import Optional, Dict

import discord
from discord import app_commands
from discord.ext import commands

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "mordhau_tickets_config.json")


# ---------- Persistence Helpers ----------
@dataclass
class GuildConfig:
    category_id: Optional[int] = None
    claim_role_id: Optional[int] = None
    log_channel_id: Optional[int] = None
    counter: int = 0


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Dict] = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                try:
                    self.data = json.load(f)
                except json.JSONDecodeError:
                    self.data = {}
        else:
            self.data = {}

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def get_guild(self, guild_id: int) -> GuildConfig:
        raw = self.data.get(str(guild_id))
        if raw is None:
            return GuildConfig()
        return GuildConfig(**raw)

    def set_guild(self, guild_id: int, cfg: GuildConfig):
        self.data[str(guild_id)] = asdict(cfg)
        self.save()


# ---------- Views ----------
class PanelView(discord.ui.View):
    """Persistent view that lives on the ticket panel message."""
    def __init__(self, cog: "MordhauTickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Open Coaching Ticket", style=discord.ButtonStyle.primary, custom_id="mordhau:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        guild = interaction.guild
        assert guild is not None

        cfg = self.cog.storage.get_guild(guild.id)
        if not (cfg.category_id and cfg.claim_role_id and cfg.log_channel_id):
            return await interaction.response.send_message(
                "Ticketing isn't configured yet. An admin must run /tickets setup.", ephemeral=True
            )

        category = guild.get_channel(cfg.category_id)
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("Configured category not found.", ephemeral=True)

        # Increment counter
        cfg.counter += 1
        self.cog.storage.set_guild(guild.id, cfg)

        # Channel name includes sequence and username
        base_name = f"mordhau-{cfg.counter:03d}-{interaction.user.name.lower()}"
        # Permissions: only opener, claim role, and staff/admin can see
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
        }
        claim_role = guild.get_role(cfg.claim_role_id)
        if claim_role:
            overwrites[claim_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=False)

        channel = await guild.create_text_channel(
            name=base_name,
            category=category,
            overwrites=overwrites,
            topic=f"opener={interaction.user.id}; counter={cfg.counter}; claimed_by=None"
        )

        embed = discord.Embed(
            title="Mordhau Coaching Ticket",
            description=(
                "Thanks for opening a ticket! A coach will be with you shortly.\n\n"
                "Use **Claim** when taking this ticket, **Close** when finished.\n"
                "Only admins can **Delete** the channel."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Opened by", value=interaction.user.mention)
        if claim_role:
            embed.add_field(name="Coaching Role", value=claim_role.mention)

        await channel.send(content=f"{interaction.user.mention} welcome!", embed=embed, view=TicketView(self.cog))
        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)


class TicketView(discord.ui.View):
    """Live on the ticket channel for actions."""
    def __init__(self, cog: "MordhauTickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="mordhau:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        guild = interaction.guild
        channel = interaction.channel
        assert guild is not None and isinstance(channel, discord.TextChannel)

        cfg = self.cog.storage.get_guild(guild.id)
        claim_role = guild.get_role(cfg.claim_role_id) if cfg.claim_role_id else None
        if claim_role is None:
            return await interaction.response.send_message("Claim role isn't configured.", ephemeral=True)

        if claim_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("You must have the coaching role to claim.", ephemeral=True)

        # Update channel topic with claimer
        topic = channel.topic or ""
        # naive update
        parts = {k: v for k, v in (p.split("=") for p in topic.split("; ") if "=" in p)}
        parts["claimed_by"] = str(interaction.user.id)
        new_topic = "; ".join(f"{k}={v}" for k, v in parts.items())
        await channel.edit(topic=new_topic)

        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, custom_id="mordhau:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        guild = interaction.guild
        channel = interaction.channel
        assert guild is not None and isinstance(channel, discord.TextChannel)

        cfg = self.cog.storage.get_guild(guild.id)
        log_channel = guild.get_channel(cfg.log_channel_id) if cfg.log_channel_id else None

        # Authorization: opener, claim-role, or admin
        opener_id = self._extract_kv(channel.topic, "opener")
        claimed_by = self._extract_kv(channel.topic, "claimed_by")

        is_opener = str(interaction.user.id) == opener_id
        is_admin = interaction.user.guild_permissions.administrator
        claim_role = guild.get_role(cfg.claim_role_id) if cfg.claim_role_id else None
        has_claim_role = claim_role in getattr(interaction.user, "roles", []) if claim_role else False

        if not (is_opper := is_opener) and not has_claim_role and not is_admin:
            return await interaction.response.send_message("Only the opener, a coach, or an admin can close this ticket.", ephemeral=True)

        # Lock channel and rename
        try:
            await channel.edit(name=f"closed-{channel.name}")
        except discord.HTTPException:
            pass
        overwrites = channel.overwrites
        # Remove send perms for everyone except admins
        for target, perms in list(overwrites.items()):
            if isinstance(target, discord.Role) and target.is_default():
                continue
            overwrites[target] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        await channel.edit(overwrites=overwrites)

        # Log to configured channel
        if isinstance(log_channel, discord.TextChannel):
            embed = discord.Embed(title="Ticket Closed", color=discord.Color.orange())
            embed.add_field(name="Channel", value=channel.mention)
            if opener_id:
                opener = guild.get_member(int(opener_id))
                embed.add_field(name="Opened by", value=opener.mention if opener else f"<@{opener_id}>")
            if claimed_by and claimed_by != "None":
                claimer = guild.get_member(int(claimed_by))
                embed.add_field(name="Claimed by", value=claimer.mention if claimer else f"<@{claimed_by}>")
            embed.add_field(name="Closed by", value=interaction.user.mention)
            await log_channel.send(embed=embed)

        await interaction.response.send_message("Ticket closed. Admins may delete when ready.")

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, custom_id="mordhau:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        channel = interaction.channel
        assert isinstance(channel, discord.TextChannel)

        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Only admins can delete ticket channels.", ephemeral=True)

        await interaction.response.send_message("Deleting this ticket channel…", ephemeral=True)
        try:
            await channel.delete(reason=f"Ticket deleted by {interaction.user}")
        except discord.HTTPException:
            pass

    # --- helpers ---
    @staticmethod
    def _extract_kv(topic: Optional[str], key: str) -> Optional[str]:
        if not topic:
            return None
        try:
            parts = dict(p.split("=") for p in topic.split("; ") if "=" in p)
            return parts.get(key)
        except Exception:
            return None


# ---------- The Cog ----------
class MordhauTickets(commands.Cog):
    """Ticket panels and ticket management for Mordhau coaching."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.storage = Storage(CONFIG_PATH)

        # Make panel button persistent after restarts
        self.bot.add_view(PanelView(self))

    # --- Admin/Setup commands ---
    tickets = app_commands.Group(name="tickets", description="Mordhau ticketing setup & panel commands")

    @tickets.command(name="setup")
    @app_commands.describe(
        category="Category where tickets will be created",
        claim_role="Role allowed to claim and close tickets",
        log_channel="Channel where closed tickets are logged",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        claim_role: discord.Role,
        log_channel: discord.TextChannel,
    ):
        """Configure ticketing for this server."""
        cfg = self.storage.get_guild(interaction.guild_id)
        cfg.category_id = category.id
        cfg.claim_role_id = claim_role.id
        cfg.log_channel_id = log_channel.id
        self.storage.set_guild(interaction.guild_id, cfg)

        await interaction.response.send_message(
            f"Configured! Category: {category.mention} | Claim role: {claim_role.mention} | Log: {log_channel.mention}",
            ephemeral=True,
        )

    @tickets.command(name="panel")
    @app_commands.describe(channel="Channel to post the ticket panel in")
    @app_commands.checks.has_permissions(administrator=True)
    async def panel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        """Post a ticket-opening panel."""
        target = channel or interaction.channel
        assert isinstance(target, discord.TextChannel)

        embed = discord.Embed(
            title="Mordhau Coaching Tickets",
            description=(
                "Click the button below to open a private ticket with the coaching team.\n\n"
                "• Tickets are enumerated and include your username in the channel name.\n"
                "• Coaches press **Claim** when taking a ticket.\n"
                "• Opener, coaches, or admins can **Close**.\n"
                "• Only admins can **Delete** the channel."
            ),
            color=discord.Color.dark_gold(),
        )

        await target.send(embed=embed, view=PanelView(self))
        await interaction.response.send_message(f"Panel posted in {target.mention}", ephemeral=True)

    # Convenience command to show current config
    @tickets.command(name="show_config")
    @app_commands.checks.has_permissions(administrator=True)
    async def show_config(self, interaction: discord.Interaction):
        cfg = self.storage.get_guild(interaction.guild_id)
        guild = interaction.guild
        assert guild is not None
        parts = []
        parts.append(f"Category: {getattr(guild.get_channel(cfg.category_id or 0), 'mention', '`unset`')}")
        parts.append(f"Claim role: {getattr(guild.get_role(cfg.claim_role_id or 0), 'mention', '`unset`')}")
        parts.append(f"Log channel: {getattr(guild.get_channel(cfg.log_channel_id or 0), 'mention', '`unset`')}")
        parts.append(f"Counter: {cfg.counter}")
        await interaction.response.send_message("\n".join(parts), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MordhauTickets(bot))
