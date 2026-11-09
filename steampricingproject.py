import os
import time
from datetime import datetime, timezone
from threading import Event
import psycopg
import requests
from dotenv import load_dotenv
from database import (
    get_watched_appids,
    process_price_observation,
)
from output import (
    configure_logging,
    emit_user_alert,
    log_exception,
    log_game_label,
    log_operation,
)

load_dotenv()

POLL_INTERVAL_SECONDS = 900
REQUEST_SPACING_SECONDS = 1
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

STEAM_COUNTRY_CODE = os.getenv("STEAM_COUNTRY_CODE", "us")
STEAM_EXPECTED_CURRENCY = os.getenv("STEAM_EXPECTED_CURRENCY", "USD")

class SteamRateLimited(Exception):
    pass

def get_game(appid, cc=None):
    if cc is None:
        cc = STEAM_COUNTRY_CODE

    r = requests.get(
        STEAM_APPDETAILS_URL,
        params={
            "appids": appid,
            "cc": cc,
        },
        timeout=10,
    )

    if r.status_code == 429:
        raise SteamRateLimited(f"Steam returned HTTP 429 for appid {appid}")

    r.raise_for_status()
    payload = r.json()

    if not isinstance(payload, dict):
        return None

    entry = payload.get(str(appid))

    if not isinstance(entry, dict) or not entry.get("success"):
        return None

    data = entry.get("data")

    if not isinstance(data, dict):
        return None

    name = data.get("name")

    if not isinstance(name, str) or not name.strip():
        name = None

    price_overview = data.get("price_overview")
    if not isinstance(price_overview, dict):
        price_overview = {}
    price = price_overview.get("final")
    currency = price_overview.get("currency")
    if price is not None and currency != STEAM_EXPECTED_CURRENCY:
        log_operation(
            f"Currency mismatch for appid {appid}: got {currency!r}, "
            f"expected {STEAM_EXPECTED_CURRENCY!r}; treating as a read failure"
        )
        price = None

    return {
        "name": name,
        "price": price,
    }

def check_one(appid, stop_event: Event | None = None):
    try:
        game = get_game(appid)
    except requests.RequestException as e:
        log_operation(
            f"Request failed for appid {appid}: {e}"
        )
        return []

    if stop_event is not None and stop_event.is_set():
        return []

    if game is None:
        log_operation(
            f"Could not find data for "
            f"{log_game_label(appid, None)}"
        )
        return []

    try:
        return process_price_observation(
            appid=appid,
            name=game["name"],
            new_price=game["price"],
            observed_at=datetime.now(timezone.utc),
        )
    except psycopg.OperationalError as e:
        log_operation(
            f"Database operation failed for "
            f"{log_game_label(appid, game['name'])}: {e}"
        )
        return []

def check_all(appids, stop_event: Event | None = None):
    collected_alerts = []

    for index, appid in enumerate(appids):
        if stop_event is not None and stop_event.is_set():
            break
        if index > 0:
            if stop_event is None:
                time.sleep(REQUEST_SPACING_SECONDS)
            elif stop_event.wait(REQUEST_SPACING_SECONDS):
                break

        try:
            collected_alerts.extend(check_one(appid, stop_event))
        except SteamRateLimited:
            log_operation(
                f"Steam rate limit hit at appid {appid}; ending this cycle "
                f"early and leaving {len(appids) - index} game(s) for the next one"
            )
            break
        except Exception:
            log_exception(
                f"Unexpected error checking appid {appid}; skipped it this cycle"
            )

    return collected_alerts

def run_poll_cycle(stop_event: Event | None = None):
    if stop_event is not None and stop_event.is_set():
        return []
    try:
        appids = get_watched_appids()
    except psycopg.OperationalError as e:
        log_operation(
            f"Could not load watched appids from PostgreSQL: {e}"
        )
        return []

    return check_all(appids, stop_event)

if __name__ == "__main__":
    configure_logging()
    while True:
        for alert in run_poll_cycle():
            emit_user_alert(alert)

        time.sleep(POLL_INTERVAL_SECONDS)
