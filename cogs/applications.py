# cogs/applications.py
from __future__ import annotations
import asyncio
import datetime as dt
from dataclasses import dataclass, asdict
from io import BytesIO
from typing import Optional, List, Dict, Any

import discord
from discord import app_commands, Interaction, ui
from discord.ext import commands
import aiosqlite

# ChatExporter imports
import chat_exporter

GUILD_TABLE = """
CREATE TABLE IF NOT EXISTS app_config(
  guild_id INTEGER PRIMARY KEY,
  accept_role_id INTEGER,
  granted_role_id INTEGER,
  close_role_ids TEXT,              -- CSV of role IDs
  category_id INTEGER,
  log_channel_id INTEGER,
  panel_message TEXT,
  open_template TEXT,
  ticket_counter INTEGER DEFAULT 0,
  panel_message_id INTEGER,
  panel_channel_id INTEGER
);
"""

TICKET_TABLE = """
CREATE TABLE IF NOT EXISTS app_tickets(
  ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER,
  opener_id INTEGER,
  opener_name TEXT,
  channel_id INTEGER,
  created_at TEXT,
  accepted_by_id INTEGER,
  accepted_by_name TEXT,
  closed_by_id INTEGER,
  closed_by_name TEXT,
  deleted_by_id INTEGER,
  deleted_by_name TEXT
);
"""

def csv_join(ids: List[int]) -> str:
    return ",".join(str(i) for i in ids)

def csv_parse(s: Optional[str]) -> List[int]:
    if not s:
        return []
    return [int(x) for x in s.split(",") if x.strip().isdigit()]

def dt_iso() -> str:
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).isoformat()

@dataclass
class GuildConfig:
    guild_id: int
    accept_role_id: int
    granted_role_id: int
    close_role_ids: List[int]
    category_id: int
    log_channel_id: int
    panel_message: str
    open_template: str
    ticket_counter: int = 0
    panel_message_id: Optional[int] = None
    panel_channel_id: Optional[int] = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "GuildConfig":
        return cls(
            guild_id=row["guild_id"],
            accept_role_id=row["accept_role_id"],
            granted_role_id=row["granted_role_id"],
            close_role_ids=csv_parse(row["close_role_ids"]),
            category_id=row["category_id"],
            log_channel_id=row["log_channel_id"],
            panel_message=row["panel_message"],
            open_template=row["open_template"],
            ticket_counter=row["ticket_counter"],
            panel_message_id=row["panel_message_id"],
            panel_channel_id=row["panel_channel_id"],
        )

class Applications(commands.Cog):
    """Application Ticket system with GUI setup & ChatExporter logging."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = "applications.sqlite3"
        self._guild_cache: Dict[int, GuildConfig] = {}

    # ---------- DB helpers ----------
    async def ensure_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute(GUILD_TABLE)
            await db.execute(TICKET_TABLE)
            await db.commit()
            db.row_factory = aiosqlite.Row

    async def get_config(self, guild_id: int) -> Optional[GuildConfig]:
        if guild_id in self._guild_cache:
            return self._guild_cache[guild_id]
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM app_config WHERE guild_id = ?", (guild_id,))
            row = await cur.fetchone()
            await cur.close()
        if row:
            cfg = GuildConfig.from_row(row)
            self._guild_cache[guild_id] = cfg
            return cfg
        return None

    async def upsert_config(self, cfg: GuildConfig):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO app_config(guild_id, accept_role_id, granted_role_id, close_role_ids, category_id,
                                       log_channel_id, panel_message, open_template, ticket_counter, panel_message_id, panel_channel_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                  accept_role_id=excluded.accept_role_id,
                  granted_role_id=excluded.granted_role_id,
                  close_role_ids=excluded.close_role_ids,
                  category_id=excluded.category_id,
                  log_channel_id=excluded.log_channel_id,
                  panel_message=excluded.panel_message,
                  open_template=excluded.open_template,
                  ticket_counter=excluded.ticket_counter,
                  panel_message_id=excluded.panel_message_id,
                  panel_channel_id=excluded.panel_channel_id
                """,
                (
                    cfg.guild_id, cfg.accept_role_id, cfg.granted_role_id,
                    csv_join(cfg.close_role_ids), cfg.category_id, cfg.log_channel_id,
                    cfg.panel_message, cfg.open_template, cfg.ticket_counter,
                    cfg.panel_message_id, cfg.panel_channel_id
                )
            )
            await db.commit()
        self._guild_cache[cfg.guild_id] = cfg

    async def next_ticket_number(self, cfg: GuildConfig) -> int:
        cfg.ticket_counter += 1
        await self.upsert_config(cfg)
        return cfg.ticket_counter

    async def insert_ticket_row(self, guild_id: int, opener: discord.Member, channel: discord.TextChannel) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO app_tickets(guild_id, opener_id, opener_name, channel_id, created_at) "
                "VALUES(?,?,?,?,?)",
                (guild_id, opener.id, str(opener), channel.id, dt_iso())
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            (ticket_id,) = await cur.fetchone()
            await cur.close()
            return ticket_id

    async def update_ticket_meta(self, channel_id: int, **cols: Any):
        if not cols:
            return
        keys = ", ".join([f"{k} = ?" for k in cols.keys()])
        values = list(cols.values())
        values.append(channel_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE app_tickets SET {keys} WHERE channel_id = ?", values)
            await db.commit()

    async def fetch_ticket_row(self, channel_id: int) -> Optional[aiosqlite.Row]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM app_tickets WHERE channel_id = ?", (channel_id,))
            row = await cur.fetchone()
            await cur.close()
            return row

    # ---------- Persistent Submit button ----------
    def make_submit_custom_id(self, guild_id: int) -> str:
        return f"app_submit:{guild_id}"

    async def cog_load(self):
        await self.ensure_db()
        # Rehydrate persistent "Submit Application" buttons if a panel is published
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT guild_id FROM app_config WHERE panel_message_id IS NOT NULL")
            rows = await cur.fetchall()
            for r in rows:
                custom_id = self.make_submit_custom_id(r["guild_id"])
                self.bot.add_view(ApplicationSubmitView(custom_id), message_id=None)  # global persistent
            await cur.close()

    # ---------- Setup command ----------
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.command(name="app_setup", description="Configure the Application Ticket system (multi-step GUI).")
    async def app_setup(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        cfg = await self.get_config(interaction.guild.id) or GuildConfig(
            guild_id=interaction.guild.id,
            accept_role_id=0,
            granted_role_id=0,
            close_role_ids=[],
            category_id=0,
            log_channel_id=0,
            panel_message="Click the button to submit an application.",
            open_template="**Application started!**\nPlease fill out this template:\n1) Age:\n2) Experience:\n3) Why should we accept you?\n",
        )
        await self.upsert_config(cfg)
        view = SetupPager(self, cfg)
        embed = discord.Embed(
            title="Application Tickets — Setup Wizard",
            description="Follow the steps to configure roles, channels, and messages.\nUse **Next** to proceed.",
            color=discord.Color.blurple()
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ---------- Publish Panel ----------
    async def publish_panel(self, guild: discord.Guild, cfg: GuildConfig) -> Optional[discord.Message]:
        channel = guild.get_channel(cfg.panel_channel_id) if cfg.panel_channel_id else None
        if channel is None:
            # Default to current system channel or first text channel
            channel = guild.system_channel or discord.utils.get(guild.text_channels, permissions_for=guild.me).or_none
            if channel is None:
                return None

        embed = discord.Embed(
            title="Apply Here",
            description=cfg.panel_message or "Click the button to submit your application.",
            color=discord.Color.green()
        )
        custom_id = self.make_submit_custom_id(guild.id)
        view = ApplicationSubmitView(custom_id)
        msg = await channel.send(embed=embed, view=view)
        cfg.panel_message_id = msg.id
        cfg.panel_channel_id = channel.id
        await self.upsert_config(cfg)
        # Make persistent
        self.bot.add_view(view, message_id=msg.id)
        return msg

    # ---------- Ticket creation ----------
    async def create_ticket_channel(
        self, interaction: Interaction, cfg: GuildConfig
    ) -> Optional[discord.TextChannel]:
        guild = interaction.guild
        opener: discord.Member = interaction.user  # applicant

        # Counter for channel name e.g. 001-username
        idx = await self.next_ticket_number(cfg)
        ch_name = f"{idx:03d}-{opener.name.lower().replace(' ', '-')[:18]}"

        category = guild.get_channel(cfg.category_id) if cfg.category_id else None
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            opener: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True, read_message_history=True),
        }

        # Allow accept + close roles to see the channel
        if cfg.accept_role_id:
            role = guild.get_role(cfg.accept_role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        for rid in cfg.close_role_ids:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        channel = await guild.create_text_channel(name=ch_name, category=category, overwrites=overwrites, reason="Application ticket opened")

        # DB row
        await self.insert_ticket_row(guild.id, opener, channel)

        # Intro message + actions
        intro = discord.Embed(
            title=f"Application Ticket for {opener}",
            description=cfg.open_template or "Please fill in the template below.",
            color=discord.Color.blurple()
        )
        intro.set_footer(text="Moderators: Use the buttons below to manage this ticket.")

        actions = TicketActionView(self, cfg, opener_id=opener.id)
        await channel.send(content=opener.mention, embed=intro, view=actions)

        await interaction.followup.send(
            f"Ticket created: {channel.mention}", ephemeral=True
        )
        return channel

    # ---------- Permissions guards ----------
    def _is_acceptor(self, member: discord.Member, cfg: GuildConfig) -> bool:
        return bool(cfg.accept_role_id and member.get_role(cfg.accept_role_id))

    def _can_close(self, member: discord.Member, cfg: GuildConfig) -> bool:
        return any(member.get_role(rid) for rid in cfg.close_role_ids) or self._is_acceptor(member, cfg)

    def _is_adminish(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        return perms.manage_guild or perms.administrator or perms.manage_channels

    # ---------- Transcript / logging ----------
    async def export_and_log(
        self,
        channel: discord.TextChannel,
        cfg: GuildConfig,
        deleted_by: discord.Member,
    ) -> Optional[discord.Message]:
        log_channel = channel.guild.get_channel(cfg.log_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            return None

        # Export HTML transcript
        html: Optional[str] = await chat_exporter.export(
            channel=channel,
            limit=None,
            tz_info="UTC",
            military_time=True,
            bot=self.bot
        )
        if not html:
            html = "<html><body><h1>No transcript available.</h1></body></html>"

        file = discord.File(BytesIO(html.encode("utf-8")), filename=f"{channel.name}.html")
        sent = await log_channel.send(file=file)

        # Retrieve ticket meta for embed
        row = await self.fetch_ticket_row(channel.id)
        opener = channel.guild.get_member(row["opener_id"]) if row else None
        accepted_by = row["accepted_by_name"] if row and row["accepted_by_name"] else "—"
        claimed_line = f"{accepted_by}" if accepted_by else "—"

        # "Transcript" button opens the just-uploaded attachment URL
        if sent.attachments:
            url = sent.attachments[0].url
        else:
            url = "https://example.com"

        embed = discord.Embed(
            title=f"Ticket #{channel.name.split('-')[0]} in Applications!",
            color=discord.Color.dark_theme()
        )
        embed.add_field(name="Type", value="from **Application** in " + channel.mention, inline=False)
        embed.add_field(name="Created by", value=f"<@{row['opener_id']}>" if row else "Unknown")
        embed.add_field(name="Deleted by", value=deleted_by.mention, inline=False)
        embed.add_field(name="Claimed by", value=claimed_line, inline=False)

        # Participants (simple: opener + bot)
        embed.add_field(name="Participants", value=f"messages by {self.bot.user.mention}", inline=False)
        embed.set_footer(text=dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))

        view = ui.View()
        view.add_item(ui.Button(style=discord.ButtonStyle.secondary, label="Transcript", url=url))

        return await log_channel.send(embed=embed, view=view)

# ====================== Views & Modals ======================

class SetupPager(ui.View):
    """3-step setup to beat 5-item per-row limitations while staying simple."""

    def __init__(self, cog: Applications, cfg: GuildConfig):
        super().__init__(timeout=600)
        self.cog = cog
        self.cfg = cfg
        self.page = 1

        # Step 1 controls (roles)
        self.accept_role = ui.RoleSelect(placeholder="Select the ACCEPT (moderator) role", min_values=1, max_values=1)
        self.granted_role = ui.RoleSelect(placeholder="Select the role to GRANT on accept", min_values=1, max_values=1)
        self.close_roles = ui.RoleSelect(placeholder="Select roles that can CLOSE tickets (multi)", min_values=0, max_values=5)

        # Step 2 controls (channels)
        self.category = ui.ChannelSelect(placeholder="Select ticket CATEGORY", channel_types=[discord.ChannelType.category], min_values=1, max_values=1)
        self.log_channel = ui.ChannelSelect(placeholder="Select LOG channel", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)

        # Step 3 (messages) via modal
        # Buttons
        self.add_item(ui.Button(label="Next →", style=discord.ButtonStyle.primary, custom_id="next"))
        self.add_item(ui.Button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="cancel"))

    async def interaction_check(self, interaction: Interaction) -> bool:
        return interaction.user.guild_permissions.manage_guild

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    async def _render(self, interaction: Interaction):
        # Clear children then add by page
        self.clear_items()
        if self.page == 1:
            self.add_item(self.accept_role)
            self.add_item(self.granted_role)
            self.add_item(self.close_roles)
            self.add_item(ui.Button(label="Next →", style=discord.ButtonStyle.primary, custom_id="next"))
            self.add_item(ui.Button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="cancel"))
            desc = "### Step 1/3 — Roles\nPick the Accept role (moderators), the role to grant on approval, and any roles allowed to close."
        elif self.page == 2:
            self.add_item(self.category)
            self.add_item(self.log_channel)
            self.add_item(ui.Button(label="← Back", style=discord.ButtonStyle.secondary, custom_id="back"))
            self.add_item(ui.Button(label="Next →", style=discord.ButtonStyle.primary, custom_id="next"))
            self.add_item(ui.Button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="cancel"))
            desc = "### Step 2/3 — Where to create & log\nChoose the ticket category and the logging channel."
        else:
            self.add_item(ui.Button(label="← Back", style=discord.ButtonStyle.secondary, custom_id="back"))
            self.add_item(ui.Button(label="Edit Messages", style=discord.ButtonStyle.primary, custom_id="messages"))
            self.add_item(ui.Button(label="Publish Panel", style=discord.ButtonStyle.success, custom_id="publish"))
            self.add_item(ui.Button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="cancel"))
            desc = "### Step 3/3 — Messages & Publish\nSet the panel message and open-template, then publish the button panel."

        embed = discord.Embed(
            title="Application Tickets — Setup Wizard",
            description=desc,
            color=discord.Color.blurple()
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="hidden", style=discord.ButtonStyle.secondary, custom_id="noop")  # never shown
    async def noop(self, interaction: Interaction, button: ui.Button):
        pass

    async def on_error(self, error: Exception, item: ui.Item, interaction: Interaction):
        await interaction.response.send_message(f"Setup error: `{error}`", ephemeral=True)

    @ui.button(label="hidden_next", style=discord.ButtonStyle.secondary, custom_id="next")
    async def _next(self, interaction: Interaction, button: ui.Button):
        if self.page == 1:
            self.cfg.accept_role_id = self.accept_role.values[0].id
            self.cfg.granted_role_id = self.granted_role.values[0].id
            self.cfg.close_role_ids = [r.id for r in self.close_roles.values]
            await self.cog.upsert_config(self.cfg)
            self.page = 2
        elif self.page == 2:
            self.cfg.category_id = self.category.values[0].id
            self.cfg.log_channel_id = self.log_channel.values[0].id
            # publish target defaults to same as log channel for convenience
            self.cfg.panel_channel_id = self.cfg.panel_channel_id or self.cfg.log_channel_id
            await self.cog.upsert_config(self.cfg)
            self.page = 3
        await self._render(interaction)

    @ui.button(label="hidden_back", style=discord.ButtonStyle.secondary, custom_id="back")
    async def _back(self, interaction: Interaction, button: ui.Button):
        self.page = max(1, self.page - 1)
        await self._render(interaction)

    @ui.button(label="hidden_cancel", style=discord.ButtonStyle.danger, custom_id="cancel")
    async def _cancel(self, interaction: Interaction, button: ui.Button):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="Setup cancelled.", view=self)

    @ui.button(label="hidden_messages", style=discord.ButtonStyle.primary, custom_id="messages")
    async def _messages(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_modal(MessagesModal(self.cog, self.cfg, parent=self))

    @ui.button(label="hidden_publish", style=discord.ButtonStyle.success, custom_id="publish")
    async def _publish(self, interaction: Interaction, button: ui.Button):
        await self.cog.upsert_config(self.cfg)
        msg = await self.cog.publish_panel(interaction.guild, self.cfg)
        if msg:
            await interaction.response.edit_message(content=f"✅ Panel published in {msg.channel.mention}.", view=None, embed=None)
        else:
            await interaction.response.edit_message(content="Couldn't find a channel to publish the panel.", view=None, embed=None)

class MessagesModal(ui.Modal, title="Edit Panel & Open Template"):
    panel_message = ui.TextInput(label="Panel message", style=discord.TextStyle.paragraph, placeholder="Shown above the Submit button", max_length=1500)
    open_template = ui.TextInput(label="Ticket open template", style=discord.TextStyle.paragraph, placeholder="Sent inside the new ticket channel", max_length=2000)

    def __init__(self, cog: Applications, cfg: GuildConfig, parent: SetupPager):
        super().__init__()
        self.cog = cog
        self.cfg = cfg
        self.parent = parent
        self.panel_message.default = cfg.panel_message or ""
        self.open_template.default = cfg.open_template or ""

    async def on_submit(self, interaction: Interaction):
        self.cfg.panel_message = str(self.panel_message.value)
        self.cfg.open_template = str(self.open_template.value)
        await self.cog.upsert_config(self.cfg)
        await interaction.response.send_message("Saved messages.", ephemeral=True)
        # Re-render parent on the ephemeral message
        try:
            await self.parent._render(interaction)
        except Exception:
            pass

# ---------------- Submit Panel Button ----------------

class ApplicationSubmitView(ui.View):
    def __init__(self, custom_id: str):
        super().__init__(timeout=None)
        self.add_item(ApplicationSubmitButton(custom_id))

class ApplicationSubmitButton(ui.Button):
    def __init__(self, custom_id: str):
        super().__init__(style=discord.ButtonStyle.success, label="Submit Application", emoji="📝", custom_id=custom_id)

    async def callback(self, interaction: Interaction):
        cog: Applications = interaction.client.get_cog("Applications")
        cfg = await cog.get_config(interaction.guild.id)
        if not cfg:
            return await interaction.response.send_message("System not configured yet.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await cog.create_ticket_channel(interaction, cfg)

# ---------------- Ticket Actions (Accept / Close / Delete) ----------------

class TicketActionView(ui.View):
    def __init__(self, cog: Applications, cfg: GuildConfig, opener_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.cfg = cfg
        self.opener_id = opener_id

        self.add_item(ui.Button(label="Submit", style=discord.ButtonStyle.primary, emoji="📨", custom_id="noop_submit", disabled=True))
        self.add_item(AcceptButton(cog, cfg))
        self.add_item(CloseButton(cog, cfg))
        self.add_item(DeleteButton(cog, cfg))

class AcceptButton(ui.Button):
    def __init__(self, cog: Applications, cfg: GuildConfig):
        super().__init__(style=discord.ButtonStyle.success, label="Accept", emoji="✅")
        self.cog = cog
        self.cfg = cfg

    async def callback(self, interaction: Interaction):
        if not self.cog._is_acceptor(interaction.user, self.cfg):
            return await interaction.response.send_message("You don't have permission to accept.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        # Assign role to opener
        row = await self.cog.fetch_ticket_row(interaction.channel.id)
        if not row:
            return await interaction.followup.send("Ticket not found in DB.", ephemeral=True)

        opener = interaction.guild.get_member(row["opener_id"])
        role = interaction.guild.get_role(self.cfg.granted_role_id)
        if opener and role:
            try:
                await opener.add_roles(role, reason="Application accepted")
            except discord.Forbidden:
                return await interaction.followup.send("I lack permission to add the configured role.", ephemeral=True)

        await self.cog.update_ticket_meta(
            interaction.channel.id,
            accepted_by_id=interaction.user.id,
            accepted_by_name=str(interaction.user),
        )

        await interaction.followup.send(f"{opener.mention if opener else 'Applicant'} has been **accepted** and granted {role.mention if role else 'the role'}.", ephemeral=True)

class CloseButton(ui.Button):
    def __init__(self, cog: Applications, cfg: GuildConfig):
        super().__init__(style=discord.ButtonStyle.secondary, label="Close", emoji="🔒")
        self.cog = cog
        self.cfg = cfg

    async def callback(self, interaction: Interaction):
        if not self.cog._can_close(interaction.user, self.cfg):
            return await interaction.response.send_message("You don't have permission to close.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        # Lock channel
        ch: discord.TextChannel = interaction.channel
        overwrites = ch.overwrites
        overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        overwrites[interaction.user] = overwrites.get(interaction.user, discord.PermissionOverwrite())
        overwrites[interaction.user].send_messages = False
        try:
            await ch.edit(overwrites=overwrites, reason="Ticket closed")
        except discord.Forbidden:
            pass

        await self.cog.update_ticket_meta(
            ch.id,
            closed_by_id=interaction.user.id,
            closed_by_name=str(interaction.user),
        )
        await interaction.followup.send("Ticket closed.", ephemeral=True)

class DeleteButton(ui.Button):
    def __init__(self, cog: Applications, cfg: GuildConfig):
        super().__init__(style=discord.ButtonStyle.danger, label="Delete & Log", emoji="🗑️")
        self.cog = cog
        self.cfg = cfg

    async def callback(self, interaction: Interaction):
        if not self.cog._is_adminish(interaction.user):
            return await interaction.response.send_message("Admins only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)

        ch: discord.TextChannel = interaction.channel
        await self.cog.update_ticket_meta(
            ch.id,
            deleted_by_id=interaction.user.id,
            deleted_by_name=str(interaction.user),
        )

        try:
            await self.cog.export_and_log(ch, self.cfg, deleted_by=interaction.user)
        except Exception as e:
            await interaction.followup.send(f"Transcript export failed: `{e}`", ephemeral=True)
        else:
            await interaction.followup.send("Transcript logged. Deleting channel…", ephemeral=True)

        try:
            await asyncio.sleep(1.0)
            await ch.delete(reason="Ticket deleted after logging")
        except discord.Forbidden:
            await interaction.followup.send("I lack permission to delete this channel.", ephemeral=True)

# ---------------- Cog setup ----------------

async def setup(bot: commands.Bot):
    await bot.add_cog(Applications(bot))
