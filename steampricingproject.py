import time
from datetime import datetime, timezone
import psycopg
import requests

from database import (
    get_watched_appids,
    process_price_observation,
)
from output import (
    emit_user_alert,
    log_game_label,
    log_operation,
)

INTERVAL = 60

def get_game(appid, cc="us"):
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

    # Steam may return JSON null, which becomes Python None.
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

    return {
        "name": name,
        "price": price_overview.get("final"),
    }

def check_all(appids):
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

        # process_price_observation returns only after its transaction commits.
        for alert in alerts:
            emit_user_alert(alert)

def run_poll_cycle():
    try:
        appids = get_watched_appids()
    except psycopg.OperationalError as e:
        log_operation(
            f"Could not load watched appids from PostgreSQL: {e}"
        )
        return

    check_all(appids)

if __name__ == "__main__":
    while True:
        run_poll_cycle()
        time.sleep(INTERVAL)