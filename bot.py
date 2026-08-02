"""Discord layer — SCAFFOLD. Signatures and constraints only; bodies are yours.

Every decision this file depends on is already made and written down in
the design notes. This module exists so you do not have to re-derive them from a
660-line document while also fighting discord.py for the first time. What it
gives you is the SHAPE and the CONSTRAINTS. The logic is deliberately missing.

Read this first — the four things that will bite:

1. A COMMAND MUST defer() BEFORE IT TOUCHES THE DATABASE.
   Discord's interaction deadline is ~3 seconds. POSTGRES_CONNECT_TIMEOUT is
   10. An unreachable database therefore blows the deadline every time, and the
   user sees "The application did not respond" rather than an error you wrote.
   defer() buys 15 minutes. There is no case where skipping it is correct.

2. BLOCKING CALLS GO THROUGH asyncio.to_thread.
   psycopg and requests are both synchronous. Calling them directly on the
   event loop stalls the gateway heartbeat, and a stalled heartbeat gets the
   bot disconnected. Every database call in this file is blocking.

3. THE POLLER MUST BE REFACTORED TO RETURN ALERTS, NOT EMIT THEM.
   Right now steampricingproject.check_all() calls emit_user_alert() itself and
   returns None. That has to change: check_all returns list[UserAlert],
   run_poll_cycle returns that list (or [] when the poll list was unavailable),
   and NEITHER calls emit_user_alert. The reason is that sending a DM is an
   `await`, and run_poll_cycle runs in a worker thread — emitting from there
   would need run_coroutine_threadsafe and a loop reference. Returning the
   alerts keeps every await on the event loop and avoids all of it.

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

from database import create_watch, get_watched_appids
from output import UserAlert, log_operation

load_dotenv()


# tasks.loop waits for the previous iteration to finish before starting the
# next, so the real period is (cycle runtime + POLL_INTERVAL_SECONDS), not a
# fixed 60. With the 1s-per-appid rate limit that drift grows with the watch
# list. Expect it; it is documented as a known limitation, not a bug to fix.
POLL_INTERVAL_SECONDS = 60


def require_bot_token() -> str:
    """Return DISCORD_BOT_TOKEN or raise RuntimeError.

    Mirror database.py::connection_args() exactly — same shape, same reasoning.
    A missing secret is a misconfiguration, not a transient failure: there is
    nothing to retry, so it must raise rather than default or warn.
    """
    raise NotImplementedError("yours")


def preflight() -> None:
    """Prove the database is reachable BEFORE the gateway connection opens.

    This is the whole database error taxonomy in one function. Open one
    connection, run something trivial, close it. If ANYTHING raises, log it and
    sys.exit(1).

    Why a preflight instead of catching auth errors at runtime: connection-time
    failures carry NO SQLSTATE (measured — wrong password, unknown user and
    unknown database all give OperationalError with sqlstate=None). So the
    class-28 check that would normally handle this literally cannot fire, and
    without a preflight a wrong password degrades into the transient branch and
    logs the same line every 60 seconds forever.

    Exiting non-zero is the point. Retrying a wrong password is not resilience.

    NOTE the deployment consequence before you wire up a service unit:
    fail-fast plus Restart=always is a crash loop that logs once per restart —
    the same spam, relocated. Pair it with StartLimitBurst.
    """
    raise NotImplementedError("yours")


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

        Two things here, and the first one has a trap:

        - self.tree.sync() registers the slash commands with Discord. A GLOBAL
          sync can take up to an hour to propagate. While developing, copy to a
          guild and sync that guild instead — it is instant. Do not spend an
          afternoon debugging commands that were never going to appear yet.
        - start the poll loop. setup_hook is the right place: it runs after
          login but before the gateway is fully ready.
        """
        raise NotImplementedError("yours")


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
    raise NotImplementedError("yours")


@poll_prices.before_loop
async def before_poll_prices() -> None:
    """Wait until the client is ready before the first cycle.

    Without `await client.wait_until_ready()` the first iteration can fire
    before the gateway connection exists, and fetch_user() fails on a client
    that is not connected yet. One line, and it is not optional.
    """
    raise NotImplementedError("yours")


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

    You will need client.fetch_user(id) — the user is probably not in cache.
    """
    raise NotImplementedError("yours")


@client.tree.command(name="watch", description="Get a DM when a game's price drops.")
async def watch(
    interaction: discord.Interaction,
    appid: int,
    alert_below: int | None = None,
) -> None:
    """Add a watch. MUST stay a pure database write.

    Both nullable-column decisions — the ratchet and games.name — were made to
    buy this property. DO NOT call Steam here to validate the appid or fetch
    the name. The moment you do, /watch starts failing whenever Steam is down,
    and both of those decisions have been spent for nothing.

    Shape:
      - defer() first (see the module docstring).
      - await asyncio.to_thread(create_watch, ...)
      - create_watch returns False when the watch already exists. That False IS
        the "you're already watching this game" reply — the UniqueViolation is
        caught in database.py, the constraint is the guarantee, and this line is
        just presentation. Never check-then-insert; that is a race.
      - catch psycopg.OperationalError and tell the user to try again shortly.
        A command is interactive: it gets a message, not a silent retry.

    alert_below is in CENTS, which is a terrible interface for a human. Decide
    deliberately whether to accept dollars here and convert, or to document the
    unit in the command description — but do the conversion with divmod / int
    arithmetic, never float. Money is integer cents everywhere in this project,
    including on paths that only display it.
    """
    raise NotImplementedError("yours")


@client.tree.command(name="unwatch", description="Stop watching a game.")
async def unwatch(interaction: discord.Interaction, appid: int) -> None:
    """Remove one watch.

    THE SQL DOES NOT EXIST YET — this is the last SQL in the project to write,
    and it is yours. Put it in queries.py with the others.

    Design notes to settle before you write it:
      - DELETE ... WHERE discord_user_id = %s AND appid = %s. The primary key
        covers this exactly, so it is an index lookup, not a scan.
      - Use RETURNING (or cur.rowcount) to distinguish "removed" from "you were
        not watching that." Deleting zero rows is not an error, but it is a
        different reply, and the user cares which one they got.
      - Do NOT delete the games row when the last watcher leaves. Leave the
        orphan. games is cheap, prices is derived history worth keeping, and
        the FK is ON DELETE RESTRICT precisely so game removal is deliberate
        rather than a side effect. GET_WATCHED_APPIDS reads from watches, so an
        unwatched game stops being polled the moment its last watch is gone —
        the cleanup you would be reaching for already happens for free.
      - Same defer + to_thread + OperationalError shape as /watch.

    You will also want a database.py function to wrap it, for symmetry with
    create_watch — the pattern is one function per user-visible operation, and
    the SQL constant stays in queries.py.
    """
    raise NotImplementedError("yours")


@client.tree.command(name="list", description="Show the games you're watching.")
async def list_watches(interaction: discord.Interaction) -> None:
    """List the caller's watches.

    Note the function is list_watches but the command is "list" — shadowing the
    builtin would be a real bug waiting to happen. The name= argument is what
    users type; the Python name is yours.

    THE SQL DOES NOT EXIST YET. Design notes:
      - This is the ONE query that needs a JOIN: watches has the appid, games
        has the name. Join them, filter on discord_user_id.
      - games.name CAN BE NULL — a game watched but never successfully polled
        has no name yet. Every rendered row must fall back to "appid N".
        output.py::user_game_label already does this; use it rather than
        writing a second fallback that can drift from the first. Test with
        fixture appid 292030, which is NULL-named ON PURPOSE.
      - Showing the current price means the latest prices row per appid, which
        is a lateral join or a window function, and it can be NULL too (a game
        with a watch but no price yet — fixture 1145360 is exactly that).
        Decide whether /list needs price at all before writing that query;
        appid + name + threshold may be enough, and it is one table less.
      - An empty result is a normal answer, not an error. Say "you aren't
        watching anything yet" rather than sending an empty embed.
      - ephemeral=True on this reply. A watch list is the user's business, and
        a public channel does not need it.
    """
    raise NotImplementedError("yours")


def main() -> None:
    """Preflight, then run.

    Order matters: require_bot_token() and preflight() BOTH run before
    client.run(), so a misconfigured process dies immediately with a clear
    message instead of connecting to Discord and then failing on its first
    query 60 seconds later.

    client.run() installs its own logging and blocks forever. Pass the token
    from require_bot_token(); never read os.environ inline here, or the
    missing-secret check silently stops applying.
    """
    raise NotImplementedError("yours")


if __name__ == "__main__":
    main()
