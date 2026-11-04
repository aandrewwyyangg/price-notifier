import asyncio
import os
import re
import sys
from typing import Optional
import discord
import psycopg
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from database import (
    MAX_WATCHES_PER_USER,
    WatchResult,
    create_watch,
    delete_watch,
    get_user_watches,
    missing_tables,
)
from output import (
    UserAlert,
    build_watch_line,
    chunk_lines,
    configure_logging,
    format_price,
    log_exception,
    log_operation,
)
from steampricingproject import POLL_INTERVAL_SECONDS, run_poll_cycle

load_dotenv()
PG_INTEGER_MAX = 2147483647
MAX_PRICE_TEXT_LENGTH = 20
PRICE_PATTERN = re.compile(r"\$?(\d*)(?:\.(\d{1,2}))?", re.ASCII)
DATABASE_UNAVAILABLE = "The database is unreachable right now, please try again shortly."
UNEXPECTED_ERROR = "Something went wrong on my end. Please try again later."


def require_bot_token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set")
    return token


def preflight() -> None:
    try:
        missing = missing_tables()
    except Exception as error:
        log_operation(f"Startup preflight failed: {error}")
        sys.exit(1)

    if missing:
        log_operation(
            f"Startup preflight failed: missing table(s) {', '.join(missing)}; "
            "apply schema.sql to this database first"
        )
        sys.exit(1)

    log_operation("Startup preflight passed: database reachable, schema present")

class SteamNotifierClient(discord.Client):
    def __init__(self) -> None:
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
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
    try:
        alerts = await asyncio.to_thread(run_poll_cycle)
    except Exception:
        log_exception("Poll cycle failed; the next cycle will try again")
        return
    for alert in alerts:
        await deliver_alert(alert)

@poll_prices.before_loop
async def before_poll_prices() -> None:
    await client.wait_until_ready()

async def deliver_alert(alert: UserAlert) -> None:
    try:
        user = await client.fetch_user(alert.discord_user_id)
        await user.send(alert.message)
    except discord.Forbidden:
        log_operation(
            f"DMs closed for user {alert.discord_user_id} "
            f"(appid {alert.appid}); alert dropped, ratchet stands"
        )
    except discord.HTTPException as error:
        log_operation(
            f"Could not DM user {alert.discord_user_id} "
            f"(appid {alert.appid}): {error}; alert dropped, ratchet stands"
        )
    except Exception:
        log_exception(
            f"Unexpected error DMing user {alert.discord_user_id} "
            f"(appid {alert.appid}); alert dropped, ratchet stands"
        )

async def reply(interaction: discord.Interaction, message: str) -> None:
    await interaction.followup.send(message, ephemeral=True)

@client.tree.error
async def on_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    command = getattr(interaction, "command", None)
    command_name = command.name if command is not None else "unknown"
    log_exception(f"Unhandled error in /{command_name}", error)
    try:
        if interaction.response.is_done():
            await reply(interaction, UNEXPECTED_ERROR)
        else:
            await interaction.response.send_message(UNEXPECTED_ERROR, ephemeral=True)
    except discord.HTTPException:
        pass

def parse_price_to_cents(text: str) -> int | None:
    amount = text.strip()
    if len(amount) > MAX_PRICE_TEXT_LENGTH:
        return None
    match = PRICE_PATTERN.fullmatch(amount)
    if match is None:
        return None
    dollars, cents = match.groups()
    if not dollars and cents is None:
        return None
    total = int(dollars or "0") * 100 + int((cents or "0").ljust(2, "0"))
    return total if total <= PG_INTEGER_MAX else None

@client.tree.command(name="watch", description="Get a DM when a game's price drops.")
@app_commands.describe(
    appid="The number in the store URL.",
    alert_below="Only alert below this price in USD.",
)
async def watch(
    interaction: discord.Interaction,
    appid: app_commands.Range[int, 1, PG_INTEGER_MAX],
    alert_below: Optional[app_commands.Range[str, 1, MAX_PRICE_TEXT_LENGTH]] = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    if not 0 < appid <= PG_INTEGER_MAX:
        await reply(
            interaction,
            "That doesn't look like a Steam appid. It's the number "
            "in the store URL, e.g. 620 for Portal 2.",
        )
        return

    threshold_cents = None

    if alert_below is not None:
        threshold_cents = parse_price_to_cents(alert_below)

        if threshold_cents is None:
            await reply(interaction, "I couldn't read that price. Try a number like 9.99.")
            return

        if threshold_cents == 0:
            await reply(interaction, "The alert price has to be more than $0.00.")
            return

    try:
        result = await asyncio.to_thread(
            create_watch,
            interaction.user.id,
            appid,
            threshold_cents,
        )
    except psycopg.OperationalError:
        await reply(interaction, DATABASE_UNAVAILABLE)
        return

    if result is WatchResult.ALREADY_WATCHING:
        await reply(
            interaction,
            "You're already watching that game. To change its alert price, "
            "/unwatch it and /watch it again.",
        )
        return

    if result is WatchResult.LIMIT_REACHED:
        await reply(
            interaction,
            f"You're already watching {MAX_WATCHES_PER_USER} games, the most I allow. "
            "/unwatch one to make room.",
        )
        return

    threshold_text = (
        f" below {format_price(threshold_cents)}"
        if threshold_cents is not None
        else ""
    )
    await reply(
        interaction,
        f"Watching appid {appid}. You'll get a DM when its price drops{threshold_text}. "
        "If Steam has no price for it (free to play or unreleased), "
        "you won't hear anything until it does.",
    )


@client.tree.command(name="unwatch", description="Stop watching a game.")
@app_commands.describe(appid="The appid you watched, e.g. 620.")
async def unwatch(
    interaction: discord.Interaction,
    appid: app_commands.Range[int, 1, PG_INTEGER_MAX],
) -> None:
    await interaction.response.defer(ephemeral=True)
    if not 0 < appid <= PG_INTEGER_MAX:
        await reply(interaction, f"You weren't watching appid {appid}.")
        return

    try:
        removed = await asyncio.to_thread(
            delete_watch,
            interaction.user.id,
            appid,
        )
    except psycopg.OperationalError:
        await reply(interaction, DATABASE_UNAVAILABLE)
        return

    if removed:
        await reply(interaction, f"No longer watching appid {appid}.")
    else:
        await reply(interaction, f"You weren't watching appid {appid}.")


@client.tree.command(name="list", description="Show the games you're watching.")
async def list_watches(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    try:
        watches = await asyncio.to_thread(
            get_user_watches,
            interaction.user.id,
        )
    except psycopg.OperationalError:
        await reply(interaction, DATABASE_UNAVAILABLE)
        return

    if not watches:
        await reply(interaction, "You aren't watching anything yet. Try /watch.")
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
    for chunk in chunk_lines(lines):
        await reply(interaction, chunk)


def main() -> None:
    configure_logging()
    token = require_bot_token()
    preflight()
    client.run(token, log_handler=None)

if __name__ == "__main__":
    main()
