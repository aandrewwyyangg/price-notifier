import os
import time
from datetime import datetime, timezone
import psycopg
import requests
from dotenv import load_dotenv

from database import (
    get_watched_appids,
    process_price_observation,
)
from output import (
    emit_user_alert,
    log_game_label,
    log_operation,
)

load_dotenv()

INTERVAL = 900

STEAM_COUNTRY_CODE = os.getenv("STEAM_COUNTRY_CODE", "us")
STEAM_EXPECTED_CURRENCY = os.getenv("STEAM_EXPECTED_CURRENCY", "USD")

def get_game(appid, cc=None):
    if cc is None:
        cc = STEAM_COUNTRY_CODE

    r = requests.get(
        "https://store.steampowered.com/api/appdetails",
        params={
            "appids": appid,
            "cc": cc,
        },
        timeout=10,
    )

    r.raise_for_status()
    payload = r.json()

    if not isinstance(payload, dict):
        return None

    entry = payload.get(str(appid), {})

    if not entry.get("success"):
        return None

    data = entry.get("data")

    if not isinstance(data, dict):
        return None

    name = data.get("name")

    if not isinstance(name, str) or not name.strip():
        name = None

    price_overview = data.get("price_overview") or {}
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

def check_all(appids):
    collected_alerts = []

    for index, appid in enumerate(appids):
        if index > 0:
            time.sleep(1)

        try:
            game = get_game(appid)
        except requests.RequestException as e:
            log_operation(
                f"Request failed for appid {appid}: {e}"
            )
            continue

        if game is None:
            log_operation(
                f"Could not find data for "
                f"{log_game_label(appid, None)}"
            )
            continue

        try:
            alerts = process_price_observation(
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
            continue

        collected_alerts.extend(alerts)

    return collected_alerts

def run_poll_cycle():
    try:
        appids = get_watched_appids()
    except psycopg.OperationalError as e:
        log_operation(
            f"Could not load watched appids from PostgreSQL: {e}"
        )
        return []

    return check_all(appids)

if __name__ == "__main__":
    while True:
        for alert in run_poll_cycle():
            emit_user_alert(alert)

        time.sleep(INTERVAL)