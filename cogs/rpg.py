# cogs/rpg.py
import asyncio
import json
import random
import sqlite3
import time
from typing import Optional, List, Dict, Any, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# =========================
# Config
# =========================
DB_FILE = "rpg.db"

# Cooldowns (seconds)
MINE_COOLDOWN = 60
TRAIN_COOLDOWN = 45
ADVENTURE_COOLDOWN = 60
GAMBLE_COOLDOWN = 10

# AI clamps / behavior
MAX_ITEM_BONUS = 5          # per stat per item
MAX_ENEMY_STAT = 18         # enemy atk/def cap
MAX_ENEMY_HP = 60
MIN_ENEMY_HP = 8
SHOP_ITEMS_PER_DAY = (3, 5) # inclusive range

# =========================
# DB Setup
# =========================
CREATE_USERS = """
CREATE TABLE IF NOT EXISTS rpg_users (
    user_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    coins INTEGER NOT NULL DEFAULT 120,
    hp INTEGER NOT NULL DEFAULT 20,
    atk INTEGER NOT NULL DEFAULT 5,
    def INTEGER NOT NULL DEFAULT 3,
    lvl INTEGER NOT NULL DEFAULT 1,
    xp INTEGER NOT NULL DEFAULT 0,
    last_mine INTEGER DEFAULT 0,
    last_train INTEGER DEFAULT 0,
    last_adventure INTEGER DEFAULT 0,
    last_gamble INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, guild_id)
);
"""

CREATE_INV = """
CREATE TABLE IF NOT EXISTS rpg_inventory (
    user_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    item TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id, item)
);
"""

CREATE_SHOP_CACHE = """
CREATE TABLE IF NOT EXISTS rpg_shop_cache (
    guild_id TEXT NOT NULL,
    yyyymmdd TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (guild_id, yyyymmdd)
);
"""

def _connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    with _connect() as c:
        c.execute(CREATE_USERS)
        c.execute(CREATE_INV)
        c.execute(CREATE_SHOP_CACHE)
_init_db()

def _now() -> int:
    return int(time.time())

def _today_key() -> str:
    return time.strftime("%Y%m%d", time.gmtime())

# =========================
# Cog
# =========================
class RPGCog(commands.Cog):
    """AI-powered mini-RPG with menu: Profile, Inventory, Shop, Training, Mine, Gambling, Adventure, Leaderboard, Reset."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- Core utils ----------
    def _client(self):
        # Provide an OpenAI Chat Completions compatible client at bot.openai_client
        return getattr(self.bot, "openai_client", None)

    def ensure_user(self, user_id: int, guild_id: int):
        with _connect() as c:
            cur = c.execute("SELECT 1 FROM rpg_users WHERE user_id=? AND guild_id=?", (str(user_id), str(guild_id)))
            if not cur.fetchone():
                c.execute("INSERT INTO rpg_users (user_id, guild_id) VALUES (?, ?)", (str(user_id), str(guild_id)))
                c.commit()

    def get_user(self, user_id: int, guild_id: int) -> sqlite3.Row:
        self.ensure_user(user_id, guild_id)
        with _connect() as c:
            return c.execute("SELECT * FROM rpg_users WHERE user_id=? AND guild_id=?", (str(user_id), str(guild_id))).fetchone()

    def set_user(self, user_id: int, guild_id: int, **updates):
        if not updates:
            return
        keys = ", ".join(f"{k}=?" for k in updates.keys())
        vals = list(updates.values()) + [str(user_id), str(guild_id)]
        with _connect() as c:
            c.execute(f"UPDATE rpg_users SET {keys} WHERE user_id=? AND guild_id=?", vals)
            c.commit()

    def inv_add(self, user_id: int, guild_id: int, item: str, qty: int = 1):
        with _connect() as c:
            row = c.execute(
                "SELECT qty FROM rpg_inventory WHERE user_id=? AND guild_id=? AND item=?",
                (str(user_id), str(guild_id), item)
            ).fetchone()
            if row:
                c.execute("UPDATE rpg_inventory SET qty=qty+? WHERE user_id=? AND guild_id=? AND item=?",
                          (qty, str(user_id), str(guild_id), item))
            else:
                c.execute("INSERT INTO rpg_inventory (user_id, guild_id, item, qty) VALUES (?, ?, ?, ?)",
                          (str(user_id), str(guild_id), item, qty))
            c.commit()

    def inv_all(self, user_id: int, guild_id: int) -> List[Tuple[str, int]]:
        with _connect() as c:
            rows = c.execute(
                "SELECT item, qty FROM rpg_inventory WHERE user_id=? AND guild_id=? ORDER BY item",
                (str(user_id), str(guild_id))
            ).fetchall()
        return [(r["item"], r["qty"]) for r in rows]

    # ---------- Embeds ----------
    def embed_profile(self, user_id: int, guild_id: int) -> discord.Embed:
        u = self.get_user(user_id, guild_id)
        e = discord.Embed(title="🧙 Your Profile", color=discord.Color.blurple())
        e.add_field(name="Level", value=str(u["lvl"]))
        e.add_field(name="XP", value=str(u["xp"]))
        e.add_field(name="HP", value=str(u["hp"]))
        e.add_field(name="ATK", value=str(u["atk"]))
        e.add_field(name="DEF", value=str(u["def"]))
        e.add_field(name="Coins", value=str(u["coins"]))
        e.set_footer(text="Use the menu below.")
        return e

    def embed_inventory(self, user_id: int, guild_id: int) -> discord.Embed:
        items = self.inv_all(user_id, guild_id)
        desc = "_Empty._" if not items else "\n".join([f"• **{n}** ×{q}" for n, q in items])
        return discord.Embed(title="🎒 Inventory", description=desc, color=discord.Color.dark_teal())

    # ---------- AI glue ----------
    async def _ai_chat_json(self, sys_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
        """
        Calls your openai_client.chat.completions.create and asks for a JSON object.
        Returns parsed dict or None on failure.
        """
        client = self._client()
        if not client:
            return None
        try:
            resp = await asyncio.to_thread(
                lambda: client.chat.completions.create(
                    model="gpt-4o-mini",
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.9,
                    max_tokens=700,
                )
            )
            content = resp.choices[0].message.content
            return json.loads(content)
        except Exception:
            return None

    # ---------- Shop (AI rotating per-guild per-day) ----------
    def _shop_cache_get(self, guild_id: int) -> Optional[List[Dict[str, Any]]]:
        k = _today_key()
        with _connect() as c:
            row = c.execute("SELECT data_json FROM rpg_shop_cache WHERE guild_id=? AND yyyymmdd=?",
                            (str(guild_id), k)).fetchone()
            if row:
                try:
                    return json.loads(row["data_json"])
                except Exception:
                    return None
        return None

    def _shop_cache_set(self, guild_id: int, items: List[Dict[str, Any]]):
        k = _today_key()
        with _connect() as c:
            c.execute("INSERT OR REPLACE INTO rpg_shop_cache (guild_id, yyyymmdd, data_json) VALUES (?, ?, ?)",
                      (str(guild_id), k, json.dumps(items)))
            c.commit()

    async def get_ai_shop(self, guild_id: int, avg_player_lvl: int) -> List[Dict[str, Any]]:
        cached = self._shop_cache_get(guild_id)
        if cached:
            return cached

        n_items = random.randint(*SHOP_ITEMS_PER_DAY)
        sys_p = (
            "You design balanced, whimsical RPG shop items for a text RPG. "
            "Return JSON: {items:[{name:str, description:str<=120, cost:int(20..160), "
            "effects:[{stat:'hp'|'atk'|'def'|'xp', amount:int(1..5)}]}]}"
        )
        user_p = (
            f"Create {n_items} items for average player level {avg_player_lvl}. "
            "Keep effects small, fair, and interesting. Prefer 1-2 effects. Avoid pure XP items."
        )

        data = await self._ai_chat_json(sys_p, user_p)
        items: List[Dict[str, Any]] = []
        if data and isinstance(data.get("items"), list):
            for it in data["items"]:
                name = str(it.get("name", "Mysterious Trinket"))[:50]
                desc = str(it.get("description", "An odd curio."))[:120]
                cost = int(it.get("cost", random.randint(30, 100)))
                cost = max(10, min(cost, 300))
                effects = []
                for eff in it.get("effects", []):
                    stat = str(eff.get("stat", "hp"))
                    if stat not in ("hp", "atk", "def", "xp"):
                        continue
                    amt = int(eff.get("amount", 1))
                    amt = max(1, min(amt, MAX_ITEM_BONUS))
                    effects.append({"stat": stat, "amount": amt})
                if not effects:
                    effects = [{"stat": "hp", "amount": 2}]
                items.append({"name": name, "description": desc, "cost": cost, "effects": effects})

        # --- Guarantee essentials ---
        has_hp = any(any(e["stat"] == "hp" for e in it["effects"]) for it in items)
        has_atk = any(any(e["stat"] == "atk" for e in it["effects"]) for it in items)

        if not has_hp:
            items.append({
                "name": "Health Potion",
                "description": "A simple red potion that restores vitality.",
                "cost": 25,
                "effects": [{"stat": "hp", "amount": 3}]
            })
        if not has_atk:
            items.append({
                "name": "Iron Sword",
                "description": "A sturdy blade to improve your strikes.",
                "cost": 70,
                "effects": [{"stat": "atk", "amount": 2}]
            })

        # --- Fallback if AI completely failed ---
        if not items:
            items = [
                {"name": "Health Potion", "description": "A simple red potion that restores vitality.", "cost": 25, "effects": [{"stat":"hp","amount":3}]},
                {"name": "Iron Sword", "description": "A sturdy blade to improve your strikes.", "cost": 70, "effects": [{"stat":"atk","amount":2}]},
                {"name": "Leather Vest", "description": "Worn but comfy (+1 DEF).", "cost": 60, "effects": [{"stat":"def","amount":1}]},
            ]

        self._shop_cache_set(guild_id, items)
        return items

    def embed_shop(self, user_id: int, guild_id: int, items: List[Dict[str, Any]]) -> discord.Embed:
        u = self.get_user(user_id, guild_id)
        lines = []
        for idx, it in enumerate(items, start=1):
            eff_txt = ", ".join([f"{e['stat'].upper()}+{e['amount']}" for e in it["effects"]])
            lines.append(f"**{idx}. {it['name']}** — {it['cost']}c\n*{it['description']}*\n_{eff_txt}_")
        e = discord.Embed(
            title="🛒 The Goblin Curio (AI Rotating Shop)",
            description="\n\n".join(lines) if lines else "_Empty today. Try again later._",
            color=discord.Color.green()
        )
        e.set_footer(text=f"You have {u['coins']} coins. Shop rotates daily.")
        return e

    # ---------- XP/Level ----------
    async def add_xp_and_level(self, user_id: int, guild_id: int, gained: int) -> str:
        u = self.get_user(user_id, guild_id)
        xp = u["xp"] + gained
        lvl = u["lvl"]
        ding = False
        while xp >= 100 * lvl:
            xp -= 100 * lvl
            lvl += 1
            ding = True
        self.set_user(user_id, guild_id, xp=xp, lvl=lvl)
        return f"**+{gained} XP**" + (" — **LEVEL UP!** 🎉" if ding else "")

    # ---------- Effects ----------
    def apply_effects(self, user_id: int, guild_id: int, effects: List[Dict[str, int]]):
        u = self.get_user(user_id, guild_id)
        new_hp = u["hp"]
        new_atk = u["atk"]
        new_def = u["def"]
        for e in effects:
            stat = e.get("stat")
            amt = int(e.get("amount", 0))
            if stat == "hp":
                new_hp += amt
            elif stat == "atk":
                new_atk += amt
            elif stat == "def":
                new_def += amt
        self.set_user(user_id, guild_id, hp=max(1, new_hp), atk=new_atk, def=new_def)

    # ---------- Activities ----------
    async def do_mine(self, user_id: int, guild_id: int) -> discord.Embed:
        u = self.get_user(user_id, guild_id)
        now = _now()
        cd = max(0, u["last_mine"] + MINE_COOLDOWN - now)
        if cd > 0:
            return discord.Embed(title="⛏️ Resting", description=f"Try again in **{cd}s**.", color=discord.Color.red())
        payout = random.randint(12, 28)
        self.set_user(user_id, guild_id, coins=u["coins"] + payout, last_mine=now)

        line = "You chip away at a glittering seam and pocket a few nuggets."
        data = await self._ai_chat_json(
            "You write one short vivid line for a fantasy mine/work action. Return JSON {line:str}.",
            "Give one line describing the scene."
        )
        if data and "line" in data:
            line = str(data["line"])[:200]

        return discord.Embed(title="⛏️ Mine", description=f"{line}\n\nYou earn **{payout}** coins.", color=discord.Color.dark_teal())

    async def do_train(self, user_id: int, guild_id: int) -> discord.Embed:
        u = self.get_user(user_id, guild_id)
        now = _now()
        cd = max(0, u["last_train"] + TRAIN_COOLDOWN - now)
        if cd > 0:
            return discord.Embed(title="🥵 Rest Up", description=f"Training in **{cd}s**.", color=discord.Color.red())
        if u["coins"] < 15:
            return discord.Embed(title="🥊 Training", description="You need **15** coins.", color=discord.Color.red())

        coins = u["coins"] - 15
        stat = random.choice(["hp", "atk", "def"])
        gain = random.randint(1, 3)
        updates = {"coins": coins, "last_train": now}
        if stat == "hp":
            updates["hp"] = u["hp"] + gain
        elif stat == "atk":
            updates["atk"] = u["atk"] + gain
        else:
            updates["def"] = u["def"] + gain
        self.set_user(user_id, guild_id, **updates)
        xp_text = await self.add_xp_and_level(user_id, guild_id, random.randint(8, 15))

        coach = "The grizzled coach nods with approval."
        data = await self._ai_chat_json(
            "You are a colorful RPG trainer. Return JSON {line:str}.",
            f"Player increased {stat.upper()} by {gain}. Give one energetic line."
        )
        if data and "line" in data:
            coach = str(data["line"])[:200]

        return discord.Embed(title="🏋️ Training Complete", description=f"{coach}\n{xp_text}", color=discord.Color.orange())

    async def do_roll(self, user_id: int, guild_id: int) -> discord.Embed:
        u = self.get_user(user_id, guild_id)
        now = _now()
        cd = max(0, u["last_gamble"] + GAMBLE_COOLDOWN - now)
        if cd > 0:
            return discord.Embed(title="⏱️ Cooldown", description=f"Gambling in **{cd}s**.", color=discord.Color.red())
        if u["coins"] < 10:
            return discord.Embed(title="🎲 Roll d20", description="Need **10** coins.", color=discord.Color.red())

        roll = random.randint(1, 20)
        coins = u["coins"] - 10
        payout = 0
        if roll == 20:
            payout = 50
        elif roll >= 15:
            payout = 25
        coins += payout
        self.set_user(user_id, guild_id, coins=coins, last_gamble=now)

        dealer = "The dealer taps the table, unreadable."
        data = await self._ai_chat_json(
            "You are a dry, witty casino dealer NPC. Return JSON {line:str}.",
            f"Player rolled {roll}. Emote a short one-liner."
        )
        if data and "line" in data:
            dealer = str(data["line"])[:200]

        desc = f"You rolled **d20 = {roll}**.\n"
        desc += f"{'Winner! You receive **' + str(payout) + '** coins.' if payout > 0 else 'No luck this time.'}\n\n{dealer}"
        return discord.Embed(title="🎲 d20 Result", description=desc, color=discord.Color.purple())

    async def do_coinflip(self, user_id: int, guild_id: int) -> discord.Embed:
        u = self.get_user(user_id, guild_id)
        now = _now()
        cd = max(0, u["last_gamble"] + GAMBLE_COOLDOWN - now)
        if cd > 0:
            return discord.Embed(title="⏱️ Cooldown", description=f"Gambling in **{cd}s**.", color=discord.Color.red())
        if u["coins"] < 10:
            return discord.Embed(title="🪙 Coinflip", description="Need **10** coins.", color=discord.Color.red())

        side = random.choice(["Heads", "Tails"])
        win = random.choice([True, False])
        coins = u["coins"] - 10 + (20 if win else 0)
        self.set_user(user_id, guild_id, coins=coins, last_gamble=now)

        quip = "The coin dances end over end."
        data = await self._ai_chat_json(
            "You narrate coinflips wryly. Return JSON {line:str}.",
            f"The coin shows {side}. Player {'wins' if win else 'loses'}."
        )
        if data and "line" in data:
            quip = str(data["line"])[:200]

        return discord.Embed(
            title="🪙 Coinflip",
            description=f"The coin shows **{side}** — you **{'WIN' if win else 'lose'}**.\n{quip}",
            color=discord.Color.purple()
        )

    async def do_adventure(self, user_id: int, guild_id: int) -> discord.Embed:
        u = self.get_user(user_id, guild_id)
        now = _now()
        cd = max(0, u["last_adventure"] + ADVENTURE_COOLDOWN - now)
        if cd > 0:
            return discord.Embed(title="🗺️ Resting", description=f"Adventure in **{cd}s**.", color=discord.Color.red())

        sys_p = (
            "You are a balanced encounter generator for a text RPG. "
            "Return JSON: {enemy:{name:str, hp:int, atk:int, def:int, description:str<=180}, scene:str<=140}"
        )
        user_p = f"Player stats: HP {u['hp']}, ATK {u['atk']}, DEF {u['def']}, LVL {u['lvl']}."
        data = await self._ai_chat_json(sys_p, user_p)

        enemy = {"name":"Mischief Slime","hp":14,"atk":4,"def":2,"description":"A gelatinous prankster wobbles into view."}
        scene = "A crooked path through mossy stones."
        if data:
            if isinstance(data.get("enemy"), dict):
                e = data["enemy"]
                enemy["name"] = str(e.get("name", enemy["name"]))[:60]
                enemy["hp"] = max(MIN_ENEMY_HP, min(int(e.get("hp", enemy["hp"])), MAX_ENEMY_HP))
                enemy["atk"] = max(1, min(int(e.get("atk", enemy["atk"])), MAX_ENEMY_STAT))
                enemy["def"] = max(0, min(int(e.get("def", enemy["def"])), MAX_ENEMY_STAT))
                enemy["description"] = str(e.get("description", enemy["description"]))[:180]
            if isinstance(data.get("scene"), str):
                scene = str(data["scene"])[:140]

        p_roll = random.randint(1, 20) + u["atk"]
        e_roll = random.randint(1, 20) + enemy["atk"]
        p_score = max(1, p_roll - enemy["def"])
        e_score = max(1, e_roll - u["def"])

        lines = [
            f"**Scene:** {scene}",
            f"You encounter **{enemy['name']}** — {enemy['description']}",
            f"Your strike total: **{p_roll} - {enemy['def']} = {p_score}**",
            f"{enemy['name']} strike total: **{e_roll} - {u['def']} = {e_score}**",
        ]

        coins = u["coins"]
        if p_score >= e_score:
            xp_reward = random.randint(16, 26)
            coin_gain = random.randint(12, 26)
            coins += coin_gain
            self.set_user(user_id, guild_id, coins=coins, last_adventure=now)
            xp_text = await self.add_xp_and_level(user_id, guild_id, xp_reward)
            lines.append(f"**Victory!** +**{coin_gain}** coins. {xp_text}")
            color = discord.Color.brand_green()
        else:
            hp_loss = random.randint(1, 5)
            self.set_user(user_id, guild_id, hp=max(1, u["hp"] - hp_loss), last_adventure=now)
            lines.append(f"**Defeat.** You lose **{hp_loss} HP** (non-lethal).")
            color = discord.Color.red()

        return discord.Embed(title="🗺️ Adventure", description="\n".join(lines), color=color)

    # ---------- Leaderboard / Reset helpers ----------
    def top_players(self, guild_id: int, metric: str = "level", limit: int = 10):
        order = {
            "level": "lvl DESC, xp DESC, coins DESC",
            "xp":    "lvl DESC, xp DESC, coins DESC",
            "coins": "coins DESC, lvl DESC, xp DESC",
        }.get(metric, "lvl DESC, xp DESC, coins DESC")
        with _connect() as c:
            return c.execute(
                f"SELECT user_id, coins, hp, atk, def, lvl, xp FROM rpg_users WHERE guild_id=? ORDER BY {order} LIMIT ?",
                (str(guild_id), limit)
            ).fetchall()

    def embed_leaderboard(self, guild_id: int, metric: str = "level") -> discord.Embed:
        label = {"level":"Level","xp":"XP","coins":"Coins"}.get(metric, "Level")
        rows = self.top_players(guild_id, metric, 10)
        if not rows:
            return discord.Embed(title=f"🏆 Leaderboard — {label}", description="_No players yet._", color=discord.Color.gold())
        lines = []
        for i, r in enumerate(rows, start=1):
            mention = f"<@{r['user_id']}>"
            if metric == "coins":
                stat = f"{r['coins']}c • Lv{r['lvl']} ({r['xp']}xp)"
            elif metric == "xp":
                stat = f"Lv{r['lvl']} • {r['xp']}xp • {r['coins']}c"
            else:
                stat = f"Lv{r['lvl']} • {r['xp']}xp • {r['coins']}c"
            lines.append(f"**{i}.** {mention} — {stat}")
        e = discord.Embed(title=f"🏆 Leaderboard — {label}", description="\n".join(lines), color=discord.Color.gold())
        e.set_footer(text="Use the buttons to switch metric.")
        return e

    def reset_user_progress(self, user_id: int, guild_id: int):
        with _connect() as c:
            c.execute("DELETE FROM rpg_inventory WHERE user_id=? AND guild_id=?", (str(user_id), str(guild_id)))
            c.execute("""
                UPDATE rpg_users SET coins=120, hp=20, atk=5, def=3, lvl=1, xp=0,
                    last_mine=0, last_train=0, last_adventure=0, last_gamble=0
                WHERE user_id=? AND guild_id=?
            """, (str(user_id), str(guild_id)))
            c.commit()

    def reset_server_progress(self, guild_id: int):
        with _connect() as c:
            c.execute("DELETE FROM rpg_inventory WHERE guild_id=?", (str(guild_id),))
            c.execute("""
                UPDATE rpg_users SET coins=120, hp=20, atk=5, def=3, lvl=1, xp=0,
                    last_mine=0, last_train=0, last_adventure=0, last_gamble=0
                WHERE guild_id=?
            """, (str(guild_id),))
            c.commit()

    # =========================
    # Views (Menu / Shop / Train / Gamble / Leaderboard / Reset)
    # =========================
    class MainView(discord.ui.View):
        def __init__(self, cog: "RPGCog", user_id: int):
            super().__init__(timeout=180)
            self.cog = cog
            self.user_id = str(user_id)

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            return str(interaction.user.id) == self.user_id

        @discord.ui.select(
            placeholder="Choose an activity…",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="Profile", description="View your stats & wallet", emoji="🧙"),
                discord.SelectOption(label="Inventory", description="Your items", emoji="🎒"),
                discord.SelectOption(label="Shop", description="AI-rotating stock", emoji="🛒"),
                discord.SelectOption(label="Training Ring", description="Boost a stat", emoji="🥊"),
                discord.SelectOption(label="Mine / Work", description="Earn wages", emoji="⛏️"),
                discord.SelectOption(label="Gambling", description="d20 / coinflip", emoji="🎲"),
                discord.SelectOption(label="Adventure", description="AI encounter", emoji="🗺️"),
                discord.SelectOption(label="Leaderboard", description="Top players (Level/XP/Coins)", emoji="🏆"),
                discord.SelectOption(label="Reset (Self)", description="Reset your stats & inventory", emoji="🗑️"),
            ]
        )
        async def select_menu(self, interaction: discord.Interaction, select: discord.ui.Select):
            choice = select.values[0]

            if choice == "Profile":
                await interaction.response.edit_message(embed=self.cog.embed_profile(interaction.user.id, interaction.guild_id), view=self)

            elif choice == "Inventory":
                await interaction.response.edit_message(embed=self.cog.embed_inventory(interaction.user.id, interaction.guild_id), view=self)

            elif choice == "Shop":
                # Defer first so AI/cache can load without timeout
                await interaction.response.defer(thinking=True)
                u = self.cog.get_user(interaction.user.id, interaction.guild_id)
                items = await self.cog.get_ai_shop(interaction.guild_id, avg_player_lvl=u["lvl"])
                shop_view = RPGCog.ShopView(self.cog, self.user_id, items)
                await interaction.edit_original_response(
                    embed=self.cog.embed_shop(interaction.user.id, interaction.guild_id, items),
                    view=shop_view
                )

            elif choice == "Training Ring":
                await interaction.response.edit_message(
                    embed=discord.Embed(
                        title="🥊 Training Ring",
                        description="Pay **15** coins to train (+1~+3 to a random stat). 45s cooldown.",
                        color=discord.Color.orange(),
                    ),
                    view=RPGCog.TrainView(self.cog, self.user_id)
                )

            elif choice == "Mine / Work":
                await interaction.response.defer(thinking=True)
                result = await self.cog.do_mine(interaction.user.id, interaction.guild_id)
                await interaction.edit_original_response(embed=result, view=self)

            elif choice == "Gambling":
                embed = discord.Embed(
                    title="🎲 Gambling Hall",
                    description="• Roll d20 (bet 10): 15+ pays 25, 20 pays 50.\n• Coinflip (bet 10): Win pays 20.\n10s cooldown.",
                    color=discord.Color.purple()
                )
                await interaction.response.edit_message(embed=embed, view=RPGCog.GambleView(self.cog, self.user_id))

            elif choice == "Adventure":
                await interaction.response.defer(thinking=True)
                result = await self.cog.do_adventure(interaction.user.id, interaction.guild_id)
                await interaction.edit_original_response(embed=result, view=self)

            elif choice == "Leaderboard":
                await interaction.response.defer(thinking=False)
                embed = self.cog.embed_leaderboard(interaction.guild_id, "level")
                await interaction.edit_original_response(embed=embed, view=RPGCog.LeaderboardView(self.cog, self.user_id, "level"))

            elif choice == "Reset (Self)":
                await interaction.response.edit_message(
                    embed=discord.Embed(
                        title="⚠️ Reset Your Progress?",
                        description="This will reset your stats and inventory. **This cannot be undone.**",
                        color=discord.Color.red()
                    ),
                    view=RPGCog.ResetSelfView(self.cog, self.user_id)
                )

    class ShopView(discord.ui.View):
        def __init__(self, cog: "RPGCog", user_id: str, items: List[Dict[str, Any]]):
            super().__init__(timeout=150)
            self.cog = cog
            self.user_id = user_id
            self.items = items or []

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            return str(interaction.user.id) == self.user_id

        async def _buy(self, interaction: discord.Interaction, idx: int):
            if not self.items:
                await interaction.response.send_message("Shop is empty today. Try again later.", ephemeral=True)
                return
            if idx >= len(self.items):
                await interaction.response.send_message("That slot is empty today.", ephemeral=True)
                return

            item = self.items[idx]
            u = self.cog.get_user(interaction.user.id, interaction.guild_id)
            if u["coins"] < item["cost"]:
                await interaction.response.send_message("You don't have enough coins.", ephemeral=True)
                return

            self.cog.set_user(interaction.user.id, interaction.guild_id, coins=u["coins"] - item["cost"])
            self.cog.inv_add(interaction.user.id, interaction.guild_id, item["name"], 1)
            self.cog.apply_effects(interaction.user.id, interaction.guild_id, item["effects"])

            embed = self.cog.embed_inventory_after_buy(interaction.user.id, interaction.guild_id, item["name"], item["cost"], item["effects"])
            await interaction.response.edit_message(embed=embed, view=self)

        @discord.ui.button(label="Buy #1", style=discord.ButtonStyle.primary)
        async def buy1(self, interaction: discord.Interaction, _): await self._buy(interaction, 0)
        @discord.ui.button(label="Buy #2", style=discord.ButtonStyle.primary)
        async def buy2(self, interaction: discord.Interaction, _): await self._buy(interaction, 1)
        @discord.ui.button(label="Buy #3", style=discord.ButtonStyle.primary)
        async def buy3(self, interaction: discord.Interaction, _): await self._buy(interaction, 2)
        @discord.ui.button(label="Buy #4", style=discord.ButtonStyle.secondary)
        async def buy4(self, interaction: discord.Interaction, _): await self._buy(interaction, 3)
        @discord.ui.button(label="Buy #5", style=discord.ButtonStyle.secondary)
        async def buy5(self, interaction: discord.Interaction, _): await self._buy(interaction, 4)
        @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
        async def back(self, interaction: discord.Interaction, _):
            await interaction.response.edit_message(
                embed=self.cog.embed_profile(interaction.user.id, interaction.guild_id),
                view=RPGCog.MainView(self.cog, interaction.user.id)
            )

    def embed_inventory_after_buy(self, user_id: int, guild_id: int, item_name: str, cost: int, effs: List[Dict[str,int]]) -> discord.Embed:
        eff_txt = ", ".join([f"{e['stat'].upper()}+{e['amount']}" for e in effs])
        u = self.get_user(user_id, guild_id)
        desc = f"Purchased **{item_name}** for **{cost}** coins.\nApplied: _{eff_txt}_\n\nCoins left: **{u['coins']}**"
        return discord.Embed(title="🛒 Purchase Complete", description=desc, color=discord.Color.green())

    class TrainView(discord.ui.View):
        def __init__(self, cog: "RPGCog", user_id: str):
            super().__init__(timeout=120)
            self.cog = cog
            self.user_id = user_id

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            return str(interaction.user.id) == self.user_id

        @discord.ui.button(label="Train (15c)", style=discord.ButtonStyle.success, emoji="🏋️")
        async def train(self, interaction: discord.Interaction, _):
            await interaction.response.defer(thinking=True)
            embed = await self.cog.do_train(interaction.user.id, interaction.guild_id)
            await interaction.edit_original_response(embed=embed, view=self)

        @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
        async def back(self, interaction: discord.Interaction, _):
            await interaction.response.edit_message(
                embed=self.cog.embed_profile(interaction.user.id, interaction.guild_id),
                view=RPGCog.MainView(self.cog, interaction.user.id)
            )

    class GambleView(discord.ui.View):
        def __init__(self, cog: "RPGCog", user_id: str):
            super().__init__(timeout=120)
            self.cog = cog
            self.user_id = user_id

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            return str(interaction.user.id) == self.user_id

        @discord.ui.button(label="Roll d20 (10c)", style=discord.ButtonStyle.primary, emoji="🎲")
        async def d20(self, interaction: discord.Interaction, _):
            await interaction.response.defer(thinking=False)
            embed = await self.cog.do_roll(interaction.user.id, interaction.guild_id)
            await interaction.edit_original_response(embed=embed, view=self)

        @discord.ui.button(label="Coinflip (10c)", style=discord.ButtonStyle.primary, emoji="🪙")
        async def coinflip(self, interaction: discord.Interaction, _):
            await interaction.response.defer(thinking=False)
            embed = await self.cog.do_coinflip(interaction.user.id, interaction.guild_id)
            await interaction.edit_original_response(embed=embed, view=self)

        @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
        async def back(self, interaction: discord.Interaction, _):
            await interaction.response.edit_message(
                embed=self.cog.embed_profile(interaction.user.id, interaction.guild_id),
                view=RPGCog.MainView(self.cog, interaction.user.id)
            )

    class LeaderboardView(discord.ui.View):
        def __init__(self, cog: "RPGCog", user_id: int, metric: str = "level"):
            super().__init__(timeout=120)
            self.cog = cog
            self.user_id = str(user_id)
            self.metric = metric

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            return True  # anyone can switch metric

        @discord.ui.button(label="Level", style=discord.ButtonStyle.primary)
        async def lb_level(self, interaction: discord.Interaction, _):
            await interaction.response.edit_message(embed=self.cog.embed_leaderboard(interaction.guild_id, "level"), view=self)

        @discord.ui.button(label="XP", style=discord.ButtonStyle.secondary)
        async def lb_xp(self, interaction: discord.Interaction, _):
            await interaction.response.edit_message(embed=self.cog.embed_leaderboard(interaction.guild_id, "xp"), view=self)

        @discord.ui.button(label="Coins", style=discord.ButtonStyle.secondary)
        async def lb_coins(self, interaction: discord.Interaction, _):
            await interaction.response.edit_message(embed=self.cog.embed_leaderboard(interaction.guild_id, "coins"), view=self)

        @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
        async def back(self, interaction: discord.Interaction, _):
            await interaction.response.edit_message(
                embed=self.cog.embed_profile(interaction.user.id, interaction.guild_id),
                view=RPGCog.MainView(self.cog, interaction.user.id)
            )

    class ResetSelfView(discord.ui.View):
        def __init__(self, cog: "RPGCog", user_id: int):
            super().__init__(timeout=60)
            self.cog = cog
            self.user_id = str(user_id)

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            return str(interaction.user.id) == self.user_id

        @discord.ui.button(label="Confirm Reset", style=discord.ButtonStyle.danger, emoji="🗑️")
        async def confirm(self, interaction: discord.Interaction, _):
            self.cog.reset_user_progress(interaction.user.id, interaction.guild_id)
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="✅ Reset Complete",
                    description="Your stats and inventory were reset to defaults.",
                    color=discord.Color.green()
                ),
                view=None
            )

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, _):
            await interaction.response.edit_message(
                embed=self.cog.embed_profile(interaction.user.id, interaction.guild_id),
                view=RPGCog.MainView(self.cog, interaction.user.id)
            )

    # =========================
    # Slash Commands
    # =========================
    @app_commands.command(name="rpg", description="Open the RPG menu.")
    @app_commands.guild_only()
    async def rpg(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = self.embed_profile(interaction.user.id, interaction.guild_id)
        await interaction.followup.send(
            embed=embed,
            view=RPGCog.MainView(self, interaction.user.id),
            allowed_mentions=interaction.client.allowed_mentions
        )

    @app_commands.command(name="rpg_leaderboard", description="Show the RPG leaderboard.")
    @app_commands.describe(metric="Sort by: level, xp, or coins")
    async def rpg_leaderboard(self, interaction: discord.Interaction, metric: str = "level"):
        metric = metric.lower().strip()
        if metric not in ("level", "xp", "coins"):
            metric = "level"
        await interaction.response.defer()
        embed = self.embed_leaderboard(interaction.guild_id, metric)
        await interaction.followup.send(
            embed=embed,
            view=RPGCog.LeaderboardView(self, interaction.user.id, metric),
            allowed_mentions=interaction.client.allowed_mentions
        )

    @app_commands.command(name="rpg_reset", description="Reset YOUR RPG progress (confirmation required).")
    async def rpg_reset(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Reset Your Progress?",
                description="This will reset your stats and inventory. **This cannot be undone.**",
                color=discord.Color.red()
            ),
            view=RPGCog.ResetSelfView(self, interaction.user.id),
            ephemeral=True
        )

    @app_commands.command(name="rpg_admin_reset", description="Admin: reset a user or the whole server.")
    @app_commands.describe(scope="Choose 'user' or 'server'", member="User to reset (required if scope=user)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rpg_admin_reset(self, interaction: discord.Interaction, scope: str, member: Optional[discord.Member] = None):
        scope = scope.lower().strip()
        if scope not in ("user", "server"):
            await interaction.response.send_message("Scope must be 'user' or 'server'.", ephemeral=True)
            return
        if scope == "user":
            if not member:
                await interaction.response.send_message("Please specify a member to reset.", ephemeral=True)
                return
            self.reset_user_progress(member.id, interaction.guild_id)
            await interaction.response.send_message(f"✅ Reset **{member.mention}**.", ephemeral=True)
        else:
            self.reset_server_progress(interaction.guild_id)
            await interaction.response.send_message("✅ Reset **all** players in this server.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(RPGCog(bot))