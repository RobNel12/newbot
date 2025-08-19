import discord, json, os, asyncio, datetime, io
from discord.ext import commands
from discord import app_commands
from typing import List, Optional, Dict, Tuple
import chat_exporter

CONFIG_FILE = "ticket_config.json"

# ---------------- Persistence ----------------
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

# ---------------- Helpers ----------------
def slugify(name: str, max_len: int = 90) -> str:
    name = name.lower()
    cleaned = []
    last_sep = False
    for ch in name:
        if ch.isalnum() or ch in "_-":
            cleaned.append(ch)
            last_sep = False
        else:
            if not last_sep:
                cleaned.append("-")
            last_sep = True
    slug = "".join(cleaned).strip("-_")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-_")
    return slug or "user"

# ---------------- Setup UI ----------------
class TicketSetupView(discord.ui.View):
    def __init__(self, cog, guild: discord.Guild, panel_name: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.panel_name = panel_name

        self.category: Optional[int] = None
        self.view_roles: List[int] = []
        self.delete_roles: List[int] = []
        self.log_channel: Optional[int] = None

        self.add_item(CategorySelect(self))
        self.add_item(ViewRolesSelect(self))
        self.add_item(DeleteRolesSelect(self))
        self.add_item(LogChannelSelect(self))

    @discord.ui.button(label="✅ Save Panel", style=discord.ButtonStyle.green)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.category or not self.view_roles or not self.log_channel:
            return await interaction.response.send_message(
                "❌ You must select a category, at least one support role, and a log channel.",
                ephemeral=True,
            )

        gid = str(self.guild.id)
        gdata = self.cog.config.setdefault(gid, {})
        panels = gdata.setdefault("panels", {})
        panels[self.panel_name] = {
            "category": self.category,
            "view_roles": self.view_roles,
            "delete_roles": self.delete_roles,
            "log_channel": self.log_channel,
        }
        save_config(self.cog.config)

        embed = discord.Embed(
            title=f"🎫 {self.panel_name.title()} Tickets",
            description="Click below to open a ticket.",
            color=discord.Color.blurple()
        )
        view = TicketPanelView(self.cog, self.guild.id, self.panel_name)
        await interaction.channel.send(embed=embed, view=view)

        await interaction.response.send_message(
            f"✅ Panel `{self.panel_name}` configured and posted in {interaction.channel.mention}",
            ephemeral=True
        )
        self.stop()

class CategorySelect(discord.ui.ChannelSelect):
    def __init__(self, view: TicketSetupView):
        super().__init__(placeholder="Select category", channel_types=[discord.ChannelType.category], min_values=1, max_values=1)
        self.view_ref = view
    async def callback(self, interaction: discord.Interaction):
        self.view_ref.category = self.values[0].id
        await interaction.response.defer()

class ViewRolesSelect(discord.ui.RoleSelect):
    def __init__(self, view: TicketSetupView):
        super().__init__(placeholder="Select support roles", min_values=1, max_values=5)
        self.view_ref = view
    async def callback(self, interaction: discord.Interaction):
        self.view_ref.view_roles = [r.id for r in self.values]
        await interaction.response.defer()

class DeleteRolesSelect(discord.ui.RoleSelect):
    def __init__(self, view: TicketSetupView):
        super().__init__(placeholder="Select delete roles", min_values=0, max_values=5)
        self.view_ref = view
    async def callback(self, interaction: discord.Interaction):
        self.view_ref.delete_roles = [r.id for r in self.values]
        await interaction.response.defer()

class LogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, view: TicketSetupView):
        super().__init__(placeholder="Select log channel", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
        self.view_ref = view
    async def callback(self, interaction: discord.Interaction):
        self.view_ref.log_channel = self.values[0].id
        await interaction.response.defer()

# ---------------- Ticket Panel ----------------
class TicketPanelView(discord.ui.View):
    def __init__(self, cog, guild_id: int, panel_name: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = str(guild_id)
        self.panel_name = panel_name

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.blurple, emoji="🎫", custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.config.get(self.guild_id, {}).get("panels", {}).get(self.panel_name)
        if not cfg:
            return await interaction.response.send_message("⚠️ Panel not configured anymore.", ephemeral=True)

        guild = interaction.guild
        category = guild.get_channel(cfg["category"])
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("⚠️ Category missing.", ephemeral=True)

        guild_cfg = self.cog.config.setdefault(self.guild_id, {})
        counter = guild_cfg.setdefault("ticket_counter", 1)
        ticket_number = counter
        guild_cfg["ticket_counter"] = counter + 1
        save_config(self.cog.config)

        opener_slug = slugify(interaction.user.name)
        chan_name = f"{ticket_number:03d}-{opener_slug}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        for rid in cfg["view_roles"]:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        channel = await guild.create_text_channel(chan_name, category=category, overwrites=overwrites)

        self.cog.channel_meta[str(channel.id)] = {
            "ticket_number": ticket_number,
            "panel_name": self.panel_name,
            "opener_id": interaction.user.id,
            "opened_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        save_config(self.cog.config)

        log_channel = guild.get_channel(cfg["log_channel"])
        log_msg = None
        if log_channel:
            embed = discord.Embed(
                title=f"Ticket #{ticket_number:03d} in {self.panel_name}!",
                description=f"Created by {interaction.user.mention} in {channel.mention}",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow(),
            )
            log_msg = await log_channel.send(embed=embed)

        await channel.send(
            f"{interaction.user.mention} opened a ticket!",
            view=TicketChannelView(
                opener_id=interaction.user.id,
                cog=self.cog,
                log_channel=log_channel,
                log_msg=log_msg,
                channel_id=channel.id
            )
        )
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)

# ---------------- Ticket Channel Controls ----------------
class TicketChannelView(discord.ui.View):
    def __init__(self, opener_id: int, cog: "TicketCog", log_channel: Optional[discord.TextChannel], log_msg: Optional[discord.Message], channel_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id
        self.cog = cog
        self.log_channel = log_channel
        self.log_msg = log_msg
        self.claimer_id: Optional[int] = None
        self.closed: bool = False
        self.channel_id = channel_id

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, emoji="🎟️", custom_id="ticket:claim", row=0)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = self.cog.config.get(str(interaction.guild.id), {})
        roster = g.get("roster", {})
        if str(interaction.user.id) not in roster:
            return await interaction.response.send_message("⚠️ You are not in the roster and cannot claim.", ephemeral=True)
        self.claimer_id = interaction.user.id
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.", ephemeral=False)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, emoji="🔒", custom_id="ticket:close", row=0)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.closed:
            return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
        await self._lock_channel(interaction.channel, lock=True)
        self.closed = True
        await interaction.response.send_message("🔒 Ticket closed. Use **Reopen** to unlock or **Delete** to archive.", ephemeral=False)

        claimer = interaction.guild.get_member(self.claimer_id) or interaction.user
        opener = interaction.guild.get_member(self.opener_id)
        await interaction.channel.send(
            (f"{opener.mention}" if opener else "The opener") + f", please leave a review for {claimer.mention}:",
            view=ReviewView(self.cog, self.log_channel, opener_id=self.opener_id,
                            staff_id=claimer.id if isinstance(claimer, discord.Member) else interaction.user.id,
                            log_msg=self.log_msg)
        )

    @discord.ui.button(label="Reopen", style=discord.ButtonStyle.success, emoji="🔓", custom_id="ticket:reopen", row=0)
    async def reopen_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.closed:
            return await interaction.response.send_message("This ticket is not closed.", ephemeral=True)
        await self._lock_channel(interaction.channel, lock=False)
        self.closed = False
        await interaction.response.send_message("🔓 Ticket reopened.", ephemeral=False)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="ticket:delete", row=0)
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.config.get(str(interaction.guild.id), {}).get("panels", {}).get(
            self.cog.channel_meta.get(str(self.channel_id), {}).get("panel_name", ""), {}
        )
        allowed = False
        if interaction.user.guild_permissions.administrator:
            allowed = True
        else:
            for rid in cfg.get("delete_roles", []):
                role = interaction.guild.get_role(rid)
                if role in interaction.user.roles:
                    allowed = True
                    break
        if not allowed:
            return await interaction.response.send_message("You don't have permission to delete this ticket.", ephemeral=True)

        await interaction.response.send_message("Archiving and deleting ticket in 5 seconds…", ephemeral=False)
        await asyncio.sleep(5)
        await self._log_and_delete(interaction.channel, interaction.user)

    async def _lock_channel(self, channel: discord.TextChannel, lock: bool):
        overwrites = channel.overwrites
        for target, perms in list(overwrites.items()):
            if isinstance(target, (discord.Role, discord.Member)):
                if perms.send_messages is not None:
                    perms.send_messages = not lock
                    overwrites[target] = perms
        await channel.edit(overwrites=overwrites)

    async def _log_and_delete(self, channel: discord.TextChannel, deleted_by: discord.Member):
        counts: Dict[int, int] = {}
        async for msg in channel.history(limit=None, oldest_first=True):
            if msg.author.bot:
                if any(s in (msg.content or "").lower() for s in ["opened a ticket!", "leave a review", "ticket closed", "archiving"]):
                    continue
            counts[msg.author.id] = counts.get(msg.author.id, 0) + 1

        transcript_html = await chat_exporter.quick_export(channel)
        file = discord.File(io.BytesIO(transcript_html.encode("utf-8")), filename=f"ticket_{self.cog.channel_meta[str(channel.id)]['ticket_number']:03d}.html")
        transcript_url = None
        if self.log_channel:
            sent = await self.log_channel.send(file=file)
            if sent.attachments:
                transcript_url = sent.attachments[0].url

        meta = self.cog.channel_meta.get(str(channel.id), {})
        opener = channel.guild.get_member(meta.get("opener_id", 0))
        embed = discord.Embed(
            title=f"Ticket #{meta.get('ticket_number', 0):03d} in {meta.get('panel_name','?')}!",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Created by", value=(opener.mention if opener else f"<@{meta.get('opener_id')}>"), inline=True)
        embed.add_field(name="Deleted by", value=deleted_by.mention, inline=True)

        if counts:
            lines = []
            for uid, c in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]:
                mem = channel.guild.get_member(uid)
                name = mem.mention if mem else f"<@{uid}>"
                lines.append(f"{c} messages by {name}")
            embed.add_field(name="Participants", value="\\n".join(lines), inline=False)

        view = None
        if transcript_url:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Transcript", url=transcript_url))

        if self.log_channel:
            await self.log_channel.send(embed=embed, view=view)

        await channel.delete()

# ---------------- Review ----------------
class ReviewView(discord.ui.View):
    def __init__(self, cog: "TicketCog", log_channel: Optional[discord.TextChannel], opener_id: int, staff_id: int, log_msg: Optional[discord.Message]):
        super().__init__(timeout=None)
        self.cog = cog
        self.log_channel = log_channel
        self.opener_id = opener_id
        self.staff_id = staff_id
        self.log_msg = log_msg
        self._used = False

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener_id:
            await interaction.response.send_message("Only the ticket opener can leave a review.", ephemeral=True)
            return False
        if self._used:
            await interaction.response.send_message("This review has already been submitted.", ephemeral=True)
            return False
        return True

    async def _finalize(self, interaction: discord.Interaction, positive: bool):
        await self.cog.record_review(self.guild_id, self.staff_id, positive)
        for child in self.children:
            child.disabled = True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Thanks for your feedback! ✅" if positive else "Thanks for your feedback! ❌",
                ephemeral=True
            )
        else:
            await interaction.channel.send(
                "Thanks for your feedback! ✅" if positive else "Thanks for your feedback! ❌",
                delete_after=5
            )
        if interaction.message:
            await interaction.message.edit(view=self)


    @discord.ui.button(emoji="👍", style=discord.ButtonStyle.success, custom_id="ticket:review_up")
    async def thumbs_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await self._finalize(interaction, positive=True)

    @discord.ui.button(emoji="👎", style=discord.ButtonStyle.danger, custom_id="ticket:review_down")
    async def thumbs_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await self._finalize(interaction, positive=False)

# ---------------- Cog ----------------
class TicketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = load_config()
        self.channel_meta: Dict[str, Dict] = self.config.setdefault("_channel_meta", {})
        save_config(self.config)
        self._autopost_task = self.bot.loop.create_task(self.autopost_loop())

    @app_commands.command(name="ticket_roster_add", description="Add a member to the roster")
    @app_commands.checks.has_permissions(administrator=True)
    async def roster_add(self, interaction: discord.Interaction, member: discord.Member):
        g = self.config.setdefault(str(interaction.guild.id), {})
        roster = g.setdefault("roster", {})
        if str(member.id) not in roster:
            roster[str(member.id)] = {"name": member.display_name, "good": 0, "bad": 0}
            save_config(self.config)
            await interaction.response.send_message(f"✅ Added {member.mention} to the roster.")
        else:
            await interaction.response.send_message("⚠️ That member is already in the roster.", ephemeral=True)

    @app_commands.command(name="ticket_roster_remove", description="Remove a member from the roster")
    @app_commands.checks.has_permissions(administrator=True)
    async def roster_remove(self, interaction: discord.Interaction, member: discord.Member):
        g = self.config.setdefault(str(interaction.guild.id), {})
        roster = g.setdefault("roster", {})
        if str(member.id) in roster:
            del roster[str(member.id)]
            save_config(self.config)
            await interaction.response.send_message(f"❌ Removed {member.mention} from the roster.")
        else:
            await interaction.response.send_message("⚠️ That member is not in the roster.", ephemeral=True)

    @app_commands.command(name="ticket_roster", description="View the public roster with ratings")
    async def roster_view(self, interaction: discord.Interaction):
        embed = self.build_roster_embed(interaction.guild.id)
        await interaction.response.send_message(embed=embed)

    def build_roster_embed(self, guild_id: int) -> discord.Embed:
        g = self.config.get(str(guild_id), {})
        roster = g.get("roster", {})
        embed = discord.Embed(title="🎟️ Ticket Staff Roster", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        embed.set_footer(text="Last updated")
        if not roster:
            embed.description = "No one in roster."
        else:
            for uid, data in roster.items():
                total = data["good"] + data["bad"]
                if total > 0:
                    percent = (data["good"] / total) * 100
                    rating = f"{percent:.1f}% 👍 ({data['good']} / {total})"
                else:
                    rating = "No reviews yet"
                embed.add_field(name=data["name"], value=rating, inline=False)
        return embed

    async def record_review(self, guild_id: int, staff_id: int, positive: bool):
        g = self.config.setdefault(str(guild_id), {})
        roster = g.setdefault("roster", {})
        entry = roster.setdefault(str(staff_id), {"name": "Unknown", "good": 0, "bad": 0})
        if positive:
            entry["good"] += 1
        else:
            entry["bad"] += 1
        save_config(self.config)

    async def autopost_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60)

    @app_commands.command(name="ticket_setup", description="Create a ticket panel")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_setup(self, interaction: discord.Interaction, panel_name: str):
        view = TicketSetupView(self, interaction.guild, panel_name)
        await interaction.response.send_message(
            f"Configuring panel `{panel_name}` — choose options below:", view=view, ephemeral=True
        )

    async def cog_load(self):
        # Restore persistent views
        for gid, gdata in self.config.items():
            if gid == "_channel_meta":
                continue
            for panel_name in gdata.get("panels", {}):
                self.bot.add_view(TicketPanelView(self, int(gid), panel_name))
        self.bot.add_view(TicketChannelView(0, self, None, None, 0))
        self.bot.add_view(ReviewView(self, None, 0, 0, None))

async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))