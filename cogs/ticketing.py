import discord, json, os, asyncio, time
from discord.ext import commands
from discord import app_commands
from typing import List, Optional

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

        # Send panel message
        embed = discord.Embed(
            title=f"🎫 {self.panel_name.title()} Tickets",
            description="Click below to open a ticket.",
            color=discord.Color.blurple()
        )
        view = TicketPanelView(self.cog, self.guild.id, self.panel_name)
        panel_msg = await interaction.channel.send(embed=embed, view=view)

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

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.blurple, emoji="🎫")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.config.get(self.guild_id, {}).get("panels", {}).get(self.panel_name)
        if not cfg:
            return await interaction.response.send_message("⚠️ Panel not configured anymore.", ephemeral=True)

        guild = interaction.guild
        category = guild.get_channel(cfg["category"])
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("⚠️ Category missing.", ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }

        for rid in cfg["view_roles"]:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        channel = await guild.create_text_channel(
            f"{self.panel_name}-ticket-{interaction.user.name}",
            category=category,
            overwrites=overwrites
        )

        log_channel = guild.get_channel(cfg["log_channel"])
        if log_channel:
            await log_channel.send(f"📩 Ticket opened in `{self.panel_name}` by {interaction.user.mention} → {channel.mention}")

        await channel.send(
            f"{interaction.user.mention} opened a ticket!",
            view=TicketChannelView(interaction.user.id, self.cog, log_channel)
        )
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)


# ---------------- Ticket Channel Controls ----------------
class TicketChannelView(discord.ui.View):
    def __init__(self, opener_id: int, cog: "TicketCog", log_channel: discord.TextChannel):
        super().__init__(timeout=None)
        self.opener_id = opener_id
        self.cog = cog
        self.log_channel = log_channel
        self.claimer_id: Optional[int] = None

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, emoji="🧰")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        g = self.cog.config.get(str(guild.id), {})
        roster = g.get("roster", {})

        if str(interaction.user.id) not in roster:
            return await interaction.response.send_message("⚠️ You are not in the roster and cannot claim.", ephemeral=True)

        self.claimer_id = interaction.user.id
        await interaction.channel.send(f"🧰 Ticket claimed by {interaction.user.mention}")
        await interaction.response.defer()

    @discord.ui.button(label="Close", style=discord.ButtonStyle.red, emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        opener = interaction.guild.get_member(self.opener_id)
        claimer = interaction.guild.get_member(self.claimer_id) if self.claimer_id else interaction.user

        await interaction.channel.send(
            f"{opener.mention}, please leave a review for {claimer.mention}:",
            view=ReviewView(self.cog, self.log_channel, opener, claimer)
        )

        await interaction.channel.send("🔒 Ticket will be deleted in 15s...")
        await discord.utils.sleep_until(discord.utils.utcnow() + discord.utils.timedelta(seconds=15))
        await interaction.channel.delete()


# ---------------- Review ----------------
class ReviewView(discord.ui.View):
    def __init__(self, cog: "TicketCog", log_channel: discord.TextChannel, opener: discord.Member, staff: discord.Member):
        super().__init__(timeout=60)
        self.cog = cog
        self.log_channel = log_channel
        self.opener = opener
        self.staff = staff

    @discord.ui.button(emoji="👍", style=discord.ButtonStyle.success)
    async def thumbs_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.record_review(interaction.guild.id, self.staff.id, positive=True)
        if self.log_channel:
            await self.log_channel.send(f"✅ {self.opener} left a positive review for {self.staff}.")
        await interaction.response.send_message("Thanks for your feedback! ✅", ephemeral=True)
        self.stop()

    @discord.ui.button(emoji="👎", style=discord.ButtonStyle.danger)
    async def thumbs_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.record_review(interaction.guild.id, self.staff.id, positive=False)
        if self.log_channel:
            await self.log_channel.send(f"❌ {self.opener} left a negative review for {self.staff}.")
        await interaction.response.send_message("Thanks for your feedback! ❌", ephemeral=True)
        self.stop()


# ---------------- Cog ----------------
class TicketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = load_config()
        self._autopost_task = self.bot.loop.create_task(self.autopost_loop())

    # ----- Roster management -----
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

    # ----- AutoPost -----
    @app_commands.command(name="ticket_roster_autopost", description="Automatically post roster ratings")
    @app_commands.checks.has_permissions(administrator=True)
    async def roster_autopost(self, interaction: discord.Interaction, channel: discord.TextChannel, interval_minutes: int):
        g = self.config.setdefault(str(interaction.guild.id), {})
        g["roster_autopost"] = {
            "channel_id": channel.id,
            "interval": interval_minutes * 60,
            "last_post": 0,
            "message_id": None
        }
        save_config(self.config)
        await interaction.response.send_message(
            f"✅ Roster ratings will auto-post every {interval_minutes} minutes in {channel.mention}.",
            ephemeral=True
        )

    @app_commands.command(name="ticket_roster_autopost_stop", description="Stop auto-posting roster ratings")
    @app_commands.checks.has_permissions(administrator=True)
    async def roster_autopost_stop(self, interaction: discord.Interaction):
        g = self.config.get(str(interaction.guild.id), {})
        if "roster_autopost" in g:
            del g["roster_autopost"]
            save_config(self.config)
            await interaction.response.send_message("🛑 Auto-post disabled.", ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ No autopost configured.", ephemeral=True)

    async def autopost_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            now = time.time()
            for gid, gdata in self.config.items():
                autopost = gdata.get("roster_autopost")
                if not autopost:
                    continue

                channel = self.bot.get_channel(autopost["channel_id"])
                if not isinstance(channel, discord.TextChannel):
                    continue

                last = autopost.get("last_post", 0)
                if now - last >= autopost["interval"]:
                    embed = self.build_roster_embed(int(gid))

                    try:
                        if autopost.get("message_id"):
                            try:
                                msg = await channel.fetch_message(autopost["message_id"])
                                await msg.edit(embed=embed)
                            except discord.NotFound:
                                msg = await channel.send(embed=embed)
                                autopost["message_id"] = msg.id
                        else:
                            msg = await channel.send(embed=embed)
                            autopost["message_id"] = msg.id

                        autopost["last_post"] = now
                        save_config(self.config)
                    except discord.Forbidden:
                        pass

            await asyncio.sleep(60)

    # ----- Panel setup -----
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
            for panel_name in gdata.get("panels", {}):
                self.bot.add_view(TicketPanelView(self, int(gid), panel_name))


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
