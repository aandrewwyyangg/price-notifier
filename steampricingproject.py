import time
import json
import os
import requests


APPIDS = ["413150", "1091500"]
INTERVAL = 60
STATE_FILE = "prices.json"


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

    price_overview = data.get("price_overview") or {}

    return {
        "name": data.get("name", f"App {appid}"),
        "price": price_overview.get("final"),
    }


def load_prices():
    if not os.path.exists(STATE_FILE):
        return {}

    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_prices(prices):
    with open(STATE_FILE, "w") as f:
        json.dump(prices, f, indent=2)


def format_price(cents):
    if cents is None:
        return "unavailable"

    return f"${cents / 100:.2f}"


def check_all(appids, prices):
    for index, appid in enumerate(appids):
        if index > 0:
            time.sleep(1)

        try:
            game = get_game(appid)
        except requests.RequestException as e:
            print(f"Request failed for app {appid}: {e}")
            continue

        if game is None:
            print(f"Could not find data for app {appid}")
            continue

        # Missing data is not a price change, so retain the last known price.
        if game["price"] is None:
            print(
                f"No price reading for {game['name']} ({appid}); "
                "keeping the last known price"
            )
            continue

        # The first observation establishes a baseline; it is not a change.
        if appid not in prices:
            prices[appid] = game
            print(
                f"Baseline recorded for {game['name']}: "
                f"{format_price(game['price'])}"
            )
            continue

        old_price = prices[appid]["price"]
        new_price = game["price"]

        if new_price != old_price:
            print(
                f"{game['name']} price changed: "
                f"{format_price(old_price)} -> {format_price(new_price)}"
            )

        prices[appid] = game

    return prices


if __name__ == "__main__":
    prices = load_prices()

    while True:
        prices = check_all(APPIDS, prices)
        save_prices(prices)
        time.sleep(INTERVAL)
