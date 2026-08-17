"""Discord layer: gateway client, slash commands, and the poll loop.

One process runs both the Discord client and the poller — a separate poller
would have no gateway connection to DM through. The four constraints this file
is built on:

1. A COMMAND defer()s BEFORE IT TOUCHES THE DATABASE.
   Discord's interaction deadline is ~3 seconds. POSTGRES_CONNECT_TIMEOUT is
   10. An unreachable database therefore blows the deadline every time, and the
   user sees "The application did not respond" rather than an error we wrote.
   defer() buys 15 minutes. There is no case where skipping it is correct.

2. BLOCKING CALLS GO THROUGH asyncio.to_thread.
   psycopg and requests are both synchronous. Calling them directly on the
   event loop stalls the gateway heartbeat, and a stalled heartbeat gets the
   bot disconnected. Every database call in this file is blocking.

3. THE POLLER RETURNS ALERTS, IT DOES NOT EMIT THEM.
   check_all returns list[UserAlert], run_poll_cycle returns that list (or []
   when the poll list was unavailable), and neither calls emit_user_alert.
   Sending a DM is an `await`, and run_poll_cycle runs in a worker thread —
   emitting from there would need run_coroutine_threadsafe and a loop
   reference. Returning the alerts keeps every await on the event loop.

4. NO CONNECTION IS SHARED BETWEEN THREADS.
   That is already true for free, because every function in database.py opens
   and closes its own connection. Do not "optimise" that into a module-level
   connection — the connection-per-call limitation is what makes to_thread safe
   here. If you ever want pooling, it has to be psycopg_pool's thread-safe pool.
"""

import asyncio
import os
import sys

import discord
import psycopg
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from database import (
    connection_args,
    create_watch,
    delete_watch,
    get_user_watches,
)
from output import (
    UserAlert,
    build_watch_line,
    format_price,
    log_operation,
)
from steampricingproject import run_poll_cycle

load_dotenv()


# 15 minutes, matching the poller's INTERVAL. tasks.loop waits for the
# previous iteration to finish before starting the next, so the real period is
# (cycle runtime + POLL_INTERVAL_SECONDS), not a fixed 900. With the
# 1s-per-appid rate limit that drift grows with the watch list. Expect it; it
# is documented as a known limitation, not a bug to fix.
POLL_INTERVAL_SECONDS = 900


def require_bot_token() -> str:
    """Return DISCORD_BOT_TOKEN or raise RuntimeError.

    Mirrors database.py::connection_args(): a missing secret is a
    misconfiguration, not a transient failure — there is nothing to retry, so
    it raises rather than defaulting or warning.
    """
    token = os.getenv("DISCORD_BOT_TOKEN")

    if token is None:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")

    return token


def preflight() -> None:
    """Prove the database is reachable BEFORE the gateway connection opens.

    This is the whole database error taxonomy in one function: connection-time
    failures carry NO SQLSTATE (measured — wrong password, unknown user and
    unknown database all give OperationalError with sqlstate=None), so the
    class-28 check that would normally handle auth failure cannot fire.
    Without this, a wrong password degrades into the runtime transient branch
    and logs the same line every cycle forever. Retrying a wrong password is
    not resilience — exiting non-zero so an operator corrects the secret is.

    Deployment consequence: fail-fast plus Restart=always is a crash loop that
    logs once per restart. Pair it with a restart limit.
    """
    try:
        with psycopg.connect(**connection_args()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
    except Exception as error:
        log_operation(f"Startup preflight failed: {error}")
        sys.exit(1)

    log_operation("Startup preflight passed: database reachable")


class SteamNotifierClient(discord.Client):
    """One process runs both the gateway client and the poller.

    Two processes were rejected: a standalone poller has no gateway connection
    and so cannot send DMs, which would force a message queue between the two —
    the transactional outbox this milestone explicitly deferred.
    """

    def __init__(self) -> None:
        # Intents: this bot reads no message content and needs no member list.
        # discord.Intents.default() is enough, and asking for more means
        # verification you do not need. Slash commands are delivered as
        # interactions, which require no privileged intent at all.
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        """Sync the command tree and start the poll loop.

        A GLOBAL sync can take up to an hour to propagate; a guild sync is
        instant. DISCORD_GUILD_ID in .env selects the fast path for
        development — unset, the commands sync globally for real use.
        """
        guild_id = os.getenv("DISCORD_GUILD_ID")

        if guild_id is not None:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log_operation(f"Command tree synced to guild {guild_id}")
        else:
            await self.tree.sync()
            log_operation("Command tree synced globally")

        poll_prices.start()


client = SteamNotifierClient()


@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def poll_prices() -> None:
    """Run one poll cycle off-thread, then deliver whatever it returned.

        alerts = await asyncio.to_thread(run_poll_cycle)

    That single line is the whole concurrency model. run_poll_cycle is fully
    synchronous — psycopg, requests, and a time.sleep(1) between appids — and
    to_thread keeps all of it off the event loop. Blocking a worker thread is
    harmless; blocking the event loop drops the gateway.

    Then send each alert with deliver_alert().

    CONSEQUENCE, ACCEPTED: alerts go out AFTER the whole cycle finishes, so an
    alert found on the first appid waits out every remaining Steam request.
    Fine at this scale. Do not "fix" it by sending from inside the thread.
    """
    alerts = await asyncio.to_thread(run_poll_cycle)

    for alert in alerts:
        await deliver_alert(alert)


@poll_prices.before_loop
async def before_poll_prices() -> None:
    # Without this the first iteration can fire before the gateway connection
    # exists, and fetch_user() fails on a client that is not connected yet.
    await client.wait_until_ready()


async def deliver_alert(alert: UserAlert) -> None:
    """DM one user. Handle discord.Forbidden per the decided rule.

    On Forbidden — the user has DMs closed or blocked the bot:
      - LOG alert.discord_user_id AND alert.appid operationally.
        (UserAlert carries appid precisely so this log can name the game
        without parsing it back out of the message text.)
      - Do NOT retry.
      - Do NOT reverse the ratchet. This is the important one. Reversing needs
        a second transaction AFTER the commit, which reintroduces exactly the
        duplicate-DM risk that at-most-once delivery was chosen to avoid.
      - Do NOT fall back to posting the alert in a public channel. A price
        watch is not public information.

    DOCUMENTED CONSEQUENCE: that user now holds a watch that can never fire,
    whose ratchet keeps advancing past prices they will never see. The watch
    decays silently. Accepted for this milestone.
    """
    try:
        # fetch_user, not get_user — the user is probably not in cache.
        user = await client.fetch_user(alert.discord_user_id)
        await user.send(alert.message)
    except discord.Forbidden:
        log_operation(
            f"DMs closed for user {alert.discord_user_id} "
            f"(appid {alert.appid}); alert dropped, ratchet stands"
        )
    except discord.HTTPException as error:
        # Same at-most-once logic as Forbidden: the ratchet committed before
        # this coroutine ever saw the alert, so drop-and-log is the only
        # correct move — and an uncaught exception here would kill the
        # tasks.loop, silently ending all polling.
        log_operation(
            f"Could not DM user {alert.discord_user_id} "
            f"(appid {alert.appid}): {error}; alert dropped, ratchet stands"
        )


def parse_price_to_cents(text: str) -> int | None:
    """Parse "9.99", "$15", or ".99" into integer cents. None means unusable.

    Integer arithmetic only. Money is integer cents everywhere in this
    project, and accepting the threshold as a float parameter would smuggle
    float money in at the front door — so the command takes a string and this
    function parses it without float ever appearing.
    """
    amount = text.strip().removeprefix("$")

    # Empty input must be None, not 0 — the fallbacks below would otherwise
    # turn "" into 0 cents, and a threshold of $0.00 silences every alert.
    if amount == "":
        return None

    dollars, separator, cents = amount.partition(".")

    if not separator:
        cents = "0"

    if dollars == "":
        dollars = "0"

    if not (dollars.isdigit() and cents.isdigit() and len(cents) <= 2):
        return None

    # "9.9" means 90 cents, not 9: a single fractional digit is tenths.
    return int(dollars) * 100 + int(cents) * (10 if len(cents) == 1 else 1)


@client.tree.command(name="watch", description="Get a DM when a game's price drops.")
@app_commands.describe(
    appid="The number in the store URL, e.g. 620 for Portal 2.",
    alert_below="Only alert below this price in USD, e.g. 9.99. Empty = every new low.",
)
async def watch(
    interaction: discord.Interaction,
    appid: int,
    alert_below: str | None = None,
) -> None:
    """Add a watch. A PURE database write — Steam is never called here.

    Both nullable-column decisions — the ratchet and games.name — were made to
    buy that property: /watch keeps working while Steam is down, and the
    poller fills in the name and baseline later. That is also why the reply
    warns about unpriced games (defect D2): /watch cannot know whether Steam
    prices this appid, because /watch does not ask Steam anything.
    """
    # defer() first — Discord's ~3s interaction deadline is shorter than the
    # 10s connect timeout, so an unreachable database would otherwise show
    # "The application did not respond" instead of the OperationalError reply.
    await interaction.response.defer(ephemeral=True)

    # Validate in the handler; the CHECK constraints are the backstop, not the
    # input validation. Letting a CheckViolation escape turns a typo into a
    # stack trace and an "application did not respond".
    if appid <= 0:
        await interaction.followup.send(
            "That doesn't look like a Steam appid — it's the positive number "
            "in the store URL, e.g. 620 for Portal 2."
        )
        return

    threshold_cents = None

    if alert_below is not None:
        threshold_cents = parse_price_to_cents(alert_below)

        if threshold_cents is None:
            await interaction.followup.send(
                "I couldn't read that price — try something like 9.99 or 15."
            )
            return

    try:
        created = await asyncio.to_thread(
            create_watch,
            interaction.user.id,
            appid,
            threshold_cents,
        )
    except psycopg.OperationalError:
        await interaction.followup.send(
            "The database is unreachable right now — please try again shortly."
        )
        return

    # create_watch returning False IS the already-watching reply: the unique
    # constraint is the guarantee, this sentence is just presentation.
    if not created:
        await interaction.followup.send("You're already watching that game.")
        return

    threshold_text = (
        f" below {format_price(threshold_cents)}"
        if threshold_cents is not None
        else ""
    )
    await interaction.followup.send(
        f"Watching appid {appid}. You'll get a DM when its price drops"
        f"{threshold_text} — the first price I see sets the baseline, "
        "silently. If Steam has no price for it (free-to-play or unreleased), "
        "this watch stays quiet until Steam prices it."
    )


@client.tree.command(name="unwatch", description="Stop watching a game.")
@app_commands.describe(appid="The appid you watched, e.g. 620.")
async def unwatch(interaction: discord.Interaction, appid: int) -> None:
    """Remove one watch. Same defer + to_thread + OperationalError shape as /watch.

    delete_watch's RETURNING distinguishes "removed" from "you weren't
    watching that" — zero rows deleted is not an error, but it is a different
    reply. The games row deliberately survives: GET_WATCHED_APPIDS reads from
    watches, so the game simply stops being polled.
    """
    await interaction.response.defer(ephemeral=True)

    try:
        removed = await asyncio.to_thread(
            delete_watch,
            interaction.user.id,
            appid,
        )
    except psycopg.OperationalError:
        await interaction.followup.send(
            "The database is unreachable right now — please try again shortly."
        )
        return

    if removed:
        await interaction.followup.send(f"No longer watching appid {appid}.")
    else:
        await interaction.followup.send(
            f"You weren't watching appid {appid} — nothing to remove."
        )


@client.tree.command(name="list", description="Show the games you're watching.")
async def list_watches(interaction: discord.Interaction) -> None:
    """List the caller's watches, ephemerally — a watch list is their business.

    (The function is list_watches but the command is "list" — shadowing the
    builtin would be a real bug waiting to happen.)

    GET_USER_WATCHES carries both nullables: games.name is NULL until a
    successful poll, and the latest price is NULL until one exists.
    build_watch_line renders both — the appid fallback and "unavailable" —
    so this handler formats nothing itself.
    """
    await interaction.response.defer(ephemeral=True)

    try:
        watches = await asyncio.to_thread(
            get_user_watches,
            interaction.user.id,
        )
    except psycopg.OperationalError:
        await interaction.followup.send(
            "The database is unreachable right now — please try again shortly."
        )
        return

    if not watches:
        # An empty result is a normal answer, not an error.
        await interaction.followup.send(
            "You aren't watching anything yet. Try /watch."
        )
        return

    lines = [
        build_watch_line(
            watch_summary.appid,
            watch_summary.name,
            watch_summary.alert_below,
            watch_summary.latest_cents,
        )
        for watch_summary in watches
    ]
    await interaction.followup.send("\n".join(lines))


def main() -> None:
    """Preflight, then run.

    Order matters: require_bot_token() and preflight() BOTH run before
    client.run(), so a misconfigured process dies immediately with a clear
    message instead of connecting to Discord and then failing on its first
    query a cycle later. client.run() installs its own logging and blocks
    forever.
    """
    token = require_bot_token()
    preflight()
    client.run(token)


if __name__ == "__main__":
    main()
