"""Poller error handling, proven with mocks — no database, no network.

Every case patches names in the POLLER's namespace rather than in the module
that defines them. steampricingproject imported these names at import time, so
its module global is the reference that actually gets called; patching
database.process_price_observation would leave the poller's copy untouched and
the test would silently prove nothing.
"""

from unittest.mock import patch

import psycopg
import requests

import steampricingproject as poller


# Two appids, not one. With a single appid `continue` and `return` produce
# IDENTICAL observable behaviour, so a `return` bug would pass every assertion.
# The second appid being reached is the only evidence the loop continued.
FIRST_APPID = 620
SECOND_APPID = 1145360
SECOND_GAME = {"name": "Hades", "price": 2499}


def assert_continued_to_second_appid(process_price_observation):
    assert process_price_observation.call_args.kwargs["appid"] == SECOND_APPID, (
        "check_all did not reach the second appid — it stopped at the failure "
        "instead of continuing"
    )


# --- The poll list itself is unreadable: skip the cycle, keep the process. ---
with patch.object(
    poller,
    "get_watched_appids",
    side_effect=psycopg.OperationalError("database unavailable"),
):
    with patch.object(poller, "check_all") as check_all:
        with patch.object(poller, "log_operation") as log_operation:
            poller.run_poll_cycle()

check_all.assert_not_called()
log_operation.assert_called_once()
assert "Could not load watched appids" in log_operation.call_args.args[0]
print("Poll-list database failure: logged and survived")


# --- A per-appid database failure: log it, then carry on to the next game. ---
# check_all sleeps 1s between appids for Steam's rate limit. The poller calls
# it as time.sleep, so patching that attribute keeps the test instant without
# bending the code under test to suit the test.
with patch.object(poller.time, "sleep"):
    with patch.object(
        poller,
        "get_game",
        side_effect=[
            {"name": "Portal 2", "price": 999},
            SECOND_GAME,
        ],
    ):
        with patch.object(
            poller,
            "process_price_observation",
            side_effect=[
                psycopg.OperationalError("connection lost"),
                [],
            ],
        ) as process_price_observation:
            with patch.object(poller, "emit_user_alert") as emit_user_alert:
                with patch.object(poller, "log_operation") as log_operation:
                    poller.check_all([FIRST_APPID, SECOND_APPID])

emit_user_alert.assert_not_called()
log_operation.assert_called_once()
assert "Database operation failed for Portal 2 (620)" in (
    log_operation.call_args.args[0]
)
assert process_price_observation.call_count == 2, (
    "check_all abandoned the cycle after one database failure"
)
assert_continued_to_second_appid(process_price_observation)
print("Per-appid database failure: logged, and the next appid was reached")


# --- Steam is unreachable: same shape, different exception. ---
with patch.object(poller.time, "sleep"):
    with patch.object(
        poller,
        "get_game",
        side_effect=[
            requests.ConnectionError("steam unreachable"),
            SECOND_GAME,
        ],
    ):
        with patch.object(
            poller,
            "process_price_observation",
            return_value=[],
        ) as process_price_observation:
            with patch.object(poller, "log_operation") as log_operation:
                poller.check_all([FIRST_APPID, SECOND_APPID])

log_operation.assert_called_once()
assert "Request failed for appid 620" in log_operation.call_args.args[0]
process_price_observation.assert_called_once()
assert_continued_to_second_appid(process_price_observation)
print("Steam request failure: logged, and the next appid was reached")


# --- Steam answers, but with nothing usable: no write at all, then continue. ---
# The database must not be touched here. Writing would advance last_checked_at
# and make an unreadable game look healthy.
with patch.object(poller.time, "sleep"):
    with patch.object(
        poller,
        "get_game",
        side_effect=[None, SECOND_GAME],
    ):
        with patch.object(
            poller,
            "process_price_observation",
            return_value=[],
        ) as process_price_observation:
            with patch.object(poller, "log_operation") as log_operation:
                poller.check_all([FIRST_APPID, SECOND_APPID])

log_operation.assert_called_once()
assert "Could not find data for appid 620" in log_operation.call_args.args[0]
process_price_observation.assert_called_once()
assert_continued_to_second_appid(process_price_observation)
print("Unusable Steam response: no database write, and the cycle continued")


# --- Anything that is not OperationalError must kill the process. ---
# _connect() raises RuntimeError when POSTGRES_PASSWORD is missing, and a
# misconfiguration should crash loudly instead of logging the same line every
# 60 seconds forever.
try:
    with patch.object(
        poller,
        "get_watched_appids",
        side_effect=RuntimeError("configuration error"),
    ):
        poller.run_poll_cycle()
except RuntimeError:
    pass
else:
    raise AssertionError(
        "run_poll_cycle swallowed a non-operational error"
    )

print("Non-operational error: failed fast as intended")
print("All poller error-handling cases passed.")
