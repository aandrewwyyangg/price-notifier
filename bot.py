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
POLL_INTERVAL_SECONDS = 900

PG_INTEGER_MAX = 2147483647


def require_bot_token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if token is None:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")
    return token


def preflight() -> None:
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
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
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
    alerts = await asyncio.to_thread(run_poll_cycle)
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

def parse_price_to_cents(text: str) -> int | None:
    amount = text.strip().removeprefix("$")
    if amount == "":
        return None
    dollars, separator, cents = amount.partition(".")
    if not separator:
        cents = "0"
    if dollars == "":
        dollars = "0"
    if not (dollars.isdigit() and cents.isdigit() and len(cents) <= 2):
        return None
    total = int(dollars) * 100 + int(cents) * (10 if len(cents) == 1 else 1)
    return total if total <= PG_INTEGER_MAX else None

@client.tree.command(name="watch", description="Get a DM when a game's price drops.")
@app_commands.describe(
    appid="The number in the store URL.",
    alert_below="Only alert below this price in USD.",
)
async def watch(
    interaction: discord.Interaction,
    appid: int,
    alert_below: str | None = None,
) -> None:
    await interaction.response.defer(ephemeral=True)
    if not 0 < appid <= PG_INTEGER_MAX:
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
    token = require_bot_token()
    preflight()
    client.run(token)

if __name__ == "__main__":
    main()
