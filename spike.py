"""THROWAWAY deploy spike. DELETE THIS FILE once it has passed.

It is not part of the bot and nothing imports it. Its whole job is to answer
four questions that cannot be answered from this laptop, plus one bonus:

  a. can a long-running process be got onto a host at all
  b. does it survive the laptop closing            <- this IS the milestone
  c. does connection_args() really move the database in CONFIGURATION
  d. does the managed Postgres accept connections from this host's IP
  +  what CURRENCY does Steam return to a datacenter IP (defect D1)

Run it in the FOREGROUND first. Only daemonise once it works there.

    python spike.py

Pass condition is not "it printed something". It is: close the laptop, wait
ten minutes, reconnect, and count heartbeats that happened while you were gone.

NOTE ON flush=True BELOW — it is load-bearing, not decoration. Python buffers
stdout when it is not a TTY, so under nohup or systemd the heartbeats sit in a
buffer and the log looks empty. That reads as "nothing is happening" rather
than "output is buffered", which is the same failure signature as this
project's localhost/IPv6 and missing-connect_timeout lessons.
"""

import sys
import time
from datetime import datetime, timezone

import psycopg
import requests

from database import connection_args

HEARTBEAT_SECONDS = 30

# Portal 2. Any always-priced, never-free appid works; this one is already
# familiar from the fixtures.
CURRENCY_PROBE_APPID = 620


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(message: str) -> None:
    print(f"[{stamp()}] {message}", flush=True)


def probe_steam_currency() -> None:
    """Answer D1: what currency does Steam quote to THIS host?

    Runs once at startup, not per heartbeat — it is a one-time fact and there
    is no reason to spend rate limit on it repeatedly.

    Steam gates pricing on the requesting IP's geolocation, not on cc= alone,
    so this can legitimately differ from what the laptop sees. That is the
    entire point of asking from here.
    """
    try:
        response = requests.get(
            "https://store.steampowered.com/api/appdetails",
            params={"appids": CURRENCY_PROBE_APPID, "cc": "us"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as error:
        log(f"STEAM PROBE FAILED: {error}")
        return

    entry = (payload or {}).get(str(CURRENCY_PROBE_APPID), {})

    if not entry.get("success"):
        # A datacenter IP getting success:false is itself a finding worth
        # having — it means the deployed poller would see nothing at all.
        log("STEAM PROBE: success=false — Steam refused this host. RECORD THIS.")
        return

    price = (entry.get("data") or {}).get("price_overview") or {}

    log(
        f"STEAM PROBE: currency={price.get('currency')!r} "
        f"final={price.get('final')!r} "
        f"({price.get('final_formatted')!r})"
    )
    log("If currency is not USD, defect D1 is CONFIRMED and must be fixed.")


def main() -> None:
    log("spike starting")

    args = connection_args()
    # Never log the password. Everything else is useful for confirming that
    # .env on this host is the file you think it is.
    log(
        f"connection target: host={args['host']} port={args['port']} "
        f"dbname={args['dbname']} user={args['user']} "
        f"connect_timeout={args['connect_timeout']}"
    )

    probe_steam_currency()

    beat = 0

    while True:
        beat += 1

        try:
            # A fresh connection every beat ON PURPOSE. It mirrors the bot's
            # connection-per-call shape, so a firewall or idle-timeout problem
            # shows up here rather than after the bot is built.
            with psycopg.connect(**connection_args()) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    result = cur.fetchone()

            log(f"beat {beat}: database OK {result}")
        except psycopg.OperationalError as error:
            # Transient by this project's taxonomy — log and keep going, so an
            # overnight run shows whether failures are constant or occasional.
            log(f"beat {beat}: DATABASE UNREACHABLE: {error}")

        time.sleep(HEARTBEAT_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("spike stopped by operator")
        sys.exit(0)
