# cogs/applications.py
from __future__ import annotations
import asyncio
import datetime as dt
from dataclasses import dataclass
from io import BytesIO
from typing import Optional, List, Dict, Any

import discord
from discord import app_commands, Interaction, ui
from discord.ext import commands
import aiosqlite
import chat_exporter

# ======================= DB SCHEMA =======================

GUILD_TABLE = """
CREATE TABLE IF NOT EXISTS app_config(
  guild_id INTEGER PRIMARY KEY,
  accept_role_id INTEGER,
  granted_role_id INTEGER,
  close_role_ids TEXT,
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

# ======================= HELPERS =======================

def csv_join(ids: List[int]) -> str:
    return ",".join(str(i) for i in ids)

def csv_parse(s: Optional[str]) -> List[int]:
    if not s:
        return []
    return [int(x) for x in s.split(",") if x.strip().isdigit()]

def now_utc_str() -> str:
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

# ======================= COG =======================

class Applications(commands.Cog):
    """Application Tickets with GUI setup & ChatExporter transcript logging."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = "applications.sqlite3"
        self._guild_cache: Dict[int, GuildConfig] = {}

    # ---------- DB ----------
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
                "INSERT INTO app_tickets(guild_id, opener_id, opener_name, channel_id, created_at) VALUES(?,?,?,?,?)",
                (guild_id, opener.id, str(opener), channel.id, now_utc_str())
            )
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            (ticket_id,) = await cur.fetchone()
            await cur.close()
            return ticket_id

    async def update_ticket_meta(self, channel_id: int, **cols: Any):
        if not cols:
            return
        keys = ", ".join(f"{k} = ?" for k in cols.keys())
        values = list(cols.values()) + [channel_id]
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

    # ---------- Persistent button id ----------
    def make_submit_custom_id(self, guild_id: int) -> str:
        return f"app_submit:{guild_id}"

    async def cog_load(self):
        await self.ensure_db()
        # Rehydrate persistent submit buttons for configured guilds
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT guild_id FROM app_config WHERE panel_message_id IS NOT NULL")
            rows = await cur.fetchall()
            for r in rows:
                self.bot.add_view(ApplicationSubmitView(self.make_submit_custom_id(r["guild_id"])))
            await cur.close()

    # ======================= COMMANDS =======================

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
            open_template=(
                "**Application started!**\n"
                "Please fill out this template:\n"
                "1) Age:\n2) Experience:\n3) Why should we accept you?\n"
            ),
        )
        await self.upsert_config(cfg)

        view = SetupPager(self, cfg)
        await view.build_initial()  # ensure Step 1 appears immediately

        embed = discord.Embed(
            title="Application Tickets — Setup Wizard",
            description="Follow the steps to configure roles, channels, and messages.\nUse **Next** to proceed.",
            color=discord.Color.blurple()
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ======================= PANEL / TICKETS =======================

    async def publish_panel(self, guild: discord.Guild, cfg: GuildConfig) -> Optional[discord.Message]:
        channel = guild.get_channel(cfg.panel_channel_id) if cfg.panel_channel_id else None
        if channel is None:
            # Pick first text channel we can talk in
            channel = None
            for ch in guild.text_channels:
                perms = ch.permissions_for(guild.me)
                if perms.send_messages and perms.embed_links:
                    channel = ch
                    break
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

        # Persistent across restarts
        self.bot.add_view(view, message_id=msg.id)
        return msg

    async def create_ticket_channel(self, interaction: Interaction, cfg: GuildConfig) -> Optional[discord.TextChannel]:
        guild = interaction.guild
        opener: discord.Member = interaction.user

        idx = await self.next_ticket_number(cfg)
        ch_name = f"{idx:03d}-{opener.name.lower().replace(' ', '-')[:18]}"

        category = guild.get_channel(cfg.category_id) if cfg.category_id else None
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            opener: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True, read_message_history=True),
        }
        if cfg.accept_role_id:
            role = guild.get_role(cfg.accept_role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        for rid in cfg.close_role_ids:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        channel = await guild.create_text_channel(
            name=ch_name, category=category, overwrites=overwrites, reason="Application ticket opened"
        )

        await self.insert_ticket_row(guild.id, opener, channel)

        intro = discord.Embed(
            title=f"Application Ticket for {opener}",
            description=cfg.open_template or "Please fill in the template below.",
            color=discord.Color.blurple()
        )
        intro.set_footer(text="Moderators: Use the buttons below to manage this ticket.")

        actions = TicketActionView(self, cfg, opener_id=opener.id)
        await channel.send(content=opener.mention, embed=intro, view=actions)

        await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)
        return channel

    # ---------- permission helpers ----------
    def _is_acceptor(self, member: discord.Member, cfg: GuildConfig) -> bool:
        return bool(cfg.accept_role_id and member.get_role(cfg.accept_role_id))

    def _can_close(self, member: discord.Member, cfg: GuildConfig) -> bool:
        return any(member.get_role(rid) for rid in cfg.close_role_ids) or self._is_acceptor(member, cfg)

    def _is_adminish(self, member: discord.Member) -> bool:
        p = member.guild_permissions
        return p.manage_guild or p.manage_channels or p.administrator

    # ---------- transcript logging ----------
    async def export_and_log(self, channel: discord.TextChannel, cfg: GuildConfig, deleted_by: discord.Member) -> Optional[discord.Message]:
        log_channel = channel.guild.get_channel(cfg.log_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            return None

        # Export HTML via ChatExporter
        html = await chat_exporter.export(
            channel=channel,
            limit=None,
            tz_info="UTC",
            military_time=True,
            bot=self.bot,
        )
        if not html:
            html = "<html><body><h1>No transcript available.</h1></body></html>"

        file = discord.File(BytesIO(html.encode("utf-8")), filename=f"{channel.name}.html")
        sent_file_msg = await log_channel.send(file=file)
        url = sent_file_msg.attachments[0].url if sent_file_msg.attachments else "https://example.com"

        row = await self.fetch_ticket_row(channel.id)
        created_by = f"<@{row['opener_id']}>" if row else "Unknown"
        accepted_by = row["accepted_by_name"] if row and row["accepted_by_name"] else "—"

        embed = discord.Embed(
            title=f"Ticket #{channel.name.split('-')[0]} in Applications!",
            color=discord.Color.dark_theme()
        )
        embed.add_field(name="Type", value=f"from **Application** in {channel.mention}", inline=False)
        embed.add_field(name="Created by", value=created_by)
        embed.add_field(name="Deleted by", value=deleted_by.mention, inline=False)
        embed.add_field(name="Claimed by", value=accepted_by, inline=False)
        embed.add_field(name="Participants", value=f"messages by {self.bot.user.mention}", inline=False)
        embed.set_footer(text=dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))

        view = ui.View()
        view.add_item(ui.Button(style=discord.ButtonStyle.secondary, label="Transcript", url=url))
        return await log_channel.send(embed=embed, view=view)

# ======================= SETUP WIZARD =======================

class SetupPager(ui.View):
    """3-step setup; unique custom_ids; selects ack to avoid 'Interaction failed'."""

    def __init__(self, cog: Applications, cfg: GuildConfig):
        super().__init__(timeout=600)
        self.cog = cog
        self.cfg = cfg
        self.page = 1

        # Step 1 selects
        self.accept_role = ui.RoleSelect(placeholder="Select the ACCEPT (moderator) role", min_values=1, max_values=1)
        self.granted_role = ui.RoleSelect(placeholder="Select the role to GRANT on accept", min_values=1, max_values=1)
        self.close_roles = ui.RoleSelect(placeholder="Select roles that can CLOSE tickets (multi)", min_values=0, max_values=5)

        # Step 2 selects
        self.category = ui.ChannelSelect(
            placeholder="Select ticket CATEGORY", channel_types=[discord.ChannelType.category], min_values=1, max_values=1
        )
        self.log_channel = ui.ChannelSelect(
            placeholder="Select LOG channel", channel_types=[discord.ChannelType.text], min_values=1, max_values=1
        )
        self.panel_channel = ui.ChannelSelect(
            placeholder="Select PANEL channel (where the button lives)",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1
        )


    # ---------- select acks ----------
    def _ack_select(self, select: ui.Select):
        async def _cb(i: Interaction):
            try:
                await i.response.defer_update()
            except Exception:
                pass
        select.callback = _cb

    def _wire_current_selects(self):
        for child in self.children:
            if isinstance(child, (ui.RoleSelect, ui.ChannelSelect)):
                self._ack_select(child)

    # ---------- initial build (first message) ----------
    async def build_initial(self):
        self.clear_items()

        def make_btn(label, style, cid, cb):
            btn = ui.Button(label=label, style=style, custom_id=cid)
            async def _cb(i: Interaction):
                await cb(i)
            btn.callback = _cb
            self.add_item(btn)

        # Step 1 widgets
        self.add_item(self.accept_role)
        self.add_item(self.granted_role)
        self.add_item(self.close_roles)
        self._wire_current_selects()

        # Buttons
        make_btn("Next →", discord.ButtonStyle.primary, "pager:next", self._next)
        make_btn("Cancel", discord.ButtonStyle.danger, "pager:cancel", self._cancel)

    async def interaction_check(self, interaction: Interaction) -> bool:
        return interaction.user.guild_permissions.manage_guild

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True

    async def _render(self, interaction: Interaction):
        self.clear_items()

        def make_btn(label, style, cid, cb):
            btn = ui.Button(label=label, style=style, custom_id=cid)
            async def _cb(i: Interaction):
                await cb(i)
            btn.callback = _cb
            self.add_item(btn)

        if self.page == 1:
            self.add_item(self.accept_role)
            self.add_item(self.granted_role)
            self.add_item(self.close_roles)
            make_btn("Next →", discord.ButtonStyle.primary, "pager:next", self._next)
            make_btn("Cancel", discord.ButtonStyle.danger, "pager:cancel", self._cancel)
            desc = "### Step 1/3 — Roles\nPick the Accept role (moderators), the role to grant on approval, and any roles allowed to close."
        elif self.page == 2:
            self.add_item(self.category)
            self.add_item(self.log_channel)
            self.add_item(self.panel_channel)
          
            make_btn("← Back", discord.ButtonStyle.secondary, "pager:back", self._back)
            make_btn("Next →", discord.ButtonStyle.primary, "pager:next", self._next)
            make_btn("Cancel", discord.ButtonStyle.danger, "pager:cancel", self._cancel)
            desc = "### Step 2/3 — Where to create & log\nChoose the ticket category and the logging channel."
        else:
            make_btn("← Back", discord.ButtonStyle.secondary, "pager:back", self._back)
            make_btn("Edit Messages", discord.ButtonStyle.primary, "pager:messages", self._messages)
            make_btn("Publish Panel", discord.ButtonStyle.success, "pager:publish", self._publish)
            make_btn("Cancel", discord.ButtonStyle.danger, "pager:cancel", self._cancel)
            desc = "### Step 3/3 — Messages & Publish\nSet the panel message and open-template, then publish the button panel."

        # wire selects so user selections are acknowledged
        self._wire_current_selects()

        embed = discord.Embed(
            title="Application Tickets — Setup Wizard",
            description=desc,
            color=discord.Color.blurple()
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    # ---------- nav callbacks ----------
    async def _next(self, interaction: Interaction):
        if self.page == 1:
            if not self.accept_role.values or not self.granted_role.values:
                return await interaction.response.send_message(
                    "Please select both the **Accept** role and the **Granted** role.", ephemeral=True
                )
            self.cfg.accept_role_id = self.accept_role.values[0].id
            self.cfg.granted_role_id = self.granted_role.values[0].id
            self.cfg.close_role_ids = [r.id for r in (self.close_roles.values or [])]
            await self.cog.upsert_config(self.cfg)
            self.page = 2

        elif self.page == 2:
            if not self.category.values or not self.log_channel.values:
                return await interaction.response.send_message(
                    "Please select a **Category** and a **Log channel**.", ephemeral=True
                )
            self.cfg.category_id = self.category.values[0].id
            self.cfg.log_channel_id = self.log_channel.values[0].id
            self.cfg.panel_channel_id = self.panel_channel.values[0].id

            await self.cog.upsert_config(self.cfg)
            self.page = 3

        await self._render(interaction)

    async def _back(self, interaction: Interaction):
        self.page = max(1, self.page - 1)
        await self._render(interaction)

    async def _cancel(self, interaction: Interaction):
        for c in self.children:
            c.disabled = True
        if interaction.response.is_done():
            await interaction.edit_original_response(content="Setup cancelled.", view=self, embed=None)
        else:
            await interaction.response.edit_message(content="Setup cancelled.", view=self, embed=None)

    async def _messages(self, interaction: Interaction):
        await interaction.response.send_modal(MessagesModal(self.cog, self.cfg, parent=self))

    async def _publish(self, interaction: Interaction):
        await self.cog.upsert_config(self.cfg)
        msg = await self.cog.publish_panel(interaction.guild, self.cfg)
        text = f"✅ Panel published in {msg.channel.mention}." if msg else "Couldn't find a channel to publish the panel."
        if interaction.response.is_done():
            await interaction.edit_original_response(content=text, view=None, embed=None)
        else:
            await interaction.response.edit_message(content=text, view=None, embed=None)

class MessagesModal(ui.Modal, title="Edit Panel & Open Template"):
    panel_message = ui.TextInput(
        label="Panel message",
        style=discord.TextStyle.paragraph,
        placeholder="Shown above the Submit button",
        max_length=1500
    )
    open_template = ui.TextInput(
        label="Ticket open template",
        style=discord.TextStyle.paragraph,
        placeholder="Sent inside the new ticket channel",
        max_length=2000
    )

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
        try:
            await self.parent._render(interaction)
        except Exception:
            pass

# ======================= PANEL VIEW =======================

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

# ======================= TICKET ACTIONS =======================

class TicketActionView(ui.View):
    def __init__(self, cog: Applications, cfg: GuildConfig, opener_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.cfg = cfg
        self.opener_id = opener_id

        self.add_item(SubmitButton(cog, cfg, opener_id))
        self.add_item(AcceptButton(cog, cfg))
        self.add_item(CloseButton(cog, cfg))
        self.add_item(DeleteButton(cog, cfg))

class SubmitButton(ui.Button):
    def __init__(self, cog: Applications, cfg: GuildConfig, opener_id: int):
        super().__init__(style=discord.ButtonStyle.primary, label="Submit", emoji="📨")
        self.cog = cog
        self.cfg = cfg
        self.opener_id = opener_id

    async def callback(self, interaction: Interaction):
        if interaction.user.id != self.opener_id:
            return await interaction.response.send_message("Only the applicant can submit this ticket.", ephemeral=True)

        await interaction.response.send_message(
            f"{interaction.user.mention} has submitted their application! {interaction.guild.get_role(self.cfg.accept_role_id).mention if self.cfg.accept_role_id else ''}",
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

        # Optionally update DB
        await self.cog.update_ticket_meta(
            interaction.channel.id,
            submitted_by_id=interaction.user.id,
            submitted_by_name=str(interaction.user),
        )

        # Disable the button after use
        self.disabled = True
        await interaction.message.edit(view=self.view)


class AcceptButton(ui.Button):
    def __init__(self, cog: Applications, cfg: GuildConfig):
        super().__init__(style=discord.ButtonStyle.success, label="Accept", emoji="✅")
        self.cog = cog
        self.cfg = cfg

    async def callback(self, interaction: Interaction):
        if not self.cog._is_acceptor(interaction.user, self.cfg):
            return await interaction.response.send_message("You don't have permission to accept.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

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
        await interaction.followup.send(
            f"{opener.mention if opener else 'Applicant'} has been **accepted** and granted {role.mention if role else 'the role'}.",
            ephemeral=True
        )

class CloseButton(ui.Button):
    def __init__(self, cog: Applications, cfg: GuildConfig):
        super().__init__(style=discord.ButtonStyle.secondary, label="Close", emoji="🔒")
        self.cog = cog
        self.cfg = cfg

    async def callback(self, interaction: Interaction):
        if not self.cog._can_close(interaction.user, self.cfg):
            return await interaction.response.send_message("You don't have permission to close.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        ch: discord.TextChannel = interaction.channel
        overwrites = ch.overwrites

        # lock channel for @everyone and opener; keep staff visibility
        overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        row = await self.cog.fetch_ticket_row(ch.id)
        if row:
            opener = interaction.guild.get_member(row["opener_id"])
            if opener:
                current = overwrites.get(opener, discord.PermissionOverwrite())
                current.send_messages = False
                overwrites[opener] = current

        try:
            await ch.edit(overwrites=overwrites, reason="Ticket closed")
        except discord.Forbidden:
            pass

        await self.cog.update_ticket_meta(
            ch.id,
            closed_by_id=interaction.user.id,
            closed_by_name=str(interaction.user),
        )
        await interaction.followup.send("Ticket closed.", ephemeral=False)

class DeleteButton(ui.Button):
    def __init__(self, cog: Applications, cfg: GuildConfig):
        super().__init__(style=discord.ButtonStyle.danger, label="Delete", emoji="🗑️")
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

# ======================= EXTENSION SETUP =======================

async def setup(bot: commands.Bot):
    await bot.add_cog(Applications(bot))
