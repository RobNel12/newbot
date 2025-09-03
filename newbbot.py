import os
import sys
import time
import logging
import signal
import asyncio
from dotenv import load_dotenv
import discord
from discord.ext import commands

load_dotenv()

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("modbot")

EXIT_ON_FATAL_RATELIMIT = os.getenv("EXIT_ON_FATAL_RATELIMIT", "0") in ("1", "true", "True")

# ---------- Intents ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

class ModBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
        )

    async def setup_hook(self):
        # Load cogs
        await self.load_extension("cogs.ticketing")
        await self.load_extension("cogs.applications")

        # Global sync (slower rollout, ~1h but necessary for all guilds)
        await self.tree.sync()
        log.info("App commands synced globally.")

    # ---- Useful lifecycle logs ----
    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    async def on_disconnect(self):
        log.warning("Gateway disconnected. Library will auto-reconnect; supervisor not involved yet.")

    async def on_resumed(self):
        log.info("Gateway session resumed.")

    # ---- Safety net: if an event handler raises and bubbles here ----
    async def on_error(self, event_method, *args, **kwargs):
        log.exception("Unhandled error in event '%s'", event_method)
        # Exit so your process manager restarts us cleanly
        os._exit(1)

bot = ModBot()

# ---------------- Utility Slash Command ----------------
@bot.tree.command(name="syncguild", description="Owner only: instantly sync app commands globally.")
async def syncguild(interaction: discord.Interaction):
    # Verify owner
    app_info = await bot.application_info()
    if interaction.user.id != app_info.owner.id:
        return await interaction.response.send_message("❌ You are not the bot owner.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)  # acknowledge immediately

    try:
        synced = await bot.tree.sync()  # Global sync
        await interaction.followup.send(
            f"✅ Globally synced **{len(synced)}** commands.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"⚠️ Sync failed: `{e}`", ephemeral=True)
        # Optional: treat failed sync as fatal if you prefer
        # os._exit(1)

# -------------------------------------------------------
# Optional helper: wrap risky API calls you write yourself (not needed for normal discord.py calls).
# If a 429 leaks through (shouldn't with discord.py), respect Retry-After. If EXIT_ON_FATAL_RATELIMIT=1,
# bail out so your supervisor restarts you after the cooldown window.
async def safe_call(coro_fn, *args, retries=3, base_delay=0.5, **kwargs):
    for attempt in range(retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except discord.HTTPException as e:
            # 429s are rate limits; discord.py usually handles them internally.
            if e.status == 429:
                retry_after = getattr(e, "retry_after", None)
                # Fall back to exponential backoff if no header exposed
                wait = (retry_after or (base_delay * (2 ** attempt)))
                jitter = min(0.250, wait * 0.1)
                wait += min(jitter, 0.250)

                log.warning("HTTP 429 encountered. Waiting %.2fs (attempt %d/%d).", wait, attempt + 1, retries)
                await asyncio.sleep(wait)

                # If operator wants a clean restart after repeated 429s:
                if EXIT_ON_FATAL_RATELIMIT and attempt == retries:
                    log.error("Repeated 429s. Exiting so the supervisor can restart after cooldown.")
                    os._exit(1)
                continue
            # Retry basic transient 5xxs
            if 500 <= e.status < 600 and attempt < retries:
                wait = base_delay * (2 ** attempt)
                log.warning("HTTP %d transient. Retrying in %.2fs...", e.status, wait)
                await asyncio.sleep(wait)
                continue
            raise
        except Exception:
            # Let unknown exceptions bubble after last retry
            if attempt == retries:
                raise
            wait = base_delay * (2 ** attempt)
            log.warning("Unknown error; retrying in %.2fs...", wait, exc_info=True)
            await asyncio.sleep(wait)

# ---------- Process-level hardening ----------
def _crash_exit(*_):
    log.error("Fatal signal received; exiting so supervisor restarts the bot.")
    os._exit(1)

def _setup_signal_handlers():
    # Exit quickly on fatal signals; your process manager brings it back up.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _crash_exit)

def _loop_exception_handler(loop, context):
    # Anything not awaited / task crash etc.
    msg = context.get("message", "Loop exception")
    err = context.get("exception")
    log.error("AsyncIO loop exception: %s", msg, exc_info=err)
    os._exit(1)

async def main():
    _setup_signal_handlers()
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)

    # Also catch unhandled exceptions / rejections at the process level
    def _handle_async_exception(loop, context):
        _loop_exception_handler(loop, context)
    loop.set_exception_handler(_handle_async_exception)

    try:
        async with bot:
            await bot.start(os.environ["DISCORD_TOKEN"])
    except discord.LoginFailure:
        log.critical("Invalid token; exiting.")
        os._exit(1)
    except discord.HTTPException as e:
        if e.status == 429 and EXIT_ON_FATAL_RATELIMIT:
            # If the library surfaces a global 429 here, treat as fatal to restart fresh after cooldown.
            log.critical("Global rate limit (429) reached at top-level. Exiting for supervisor restart.")
            os._exit(1)
        raise
    except Exception:
        # Any top-level crash should terminate with non-zero exit
        log.exception("Top-level crash. Exiting for supervisor restart.")
        os._exit(1)

if __name__ == "__main__":
    # Run the bot; if it crashes, we exit non-zero so systemd/PM2/Docker auto-restart it.
    asyncio.run(main())