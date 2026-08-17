"""Poller and guard error handling, proven with mocks — no database, no network.

Every case patches names in the namespace that CALLS them. steampricingproject
imported these names at import time, so its module global is the reference that
actually gets called; patching database.process_price_observation would leave
the poller's copy untouched and the test would silently prove nothing. The
same rule is why the guard cases patch database._connect in database's own
namespace.
"""

from unittest.mock import Mock, patch

import psycopg
import requests

import database
import steampricingproject as poller
from output import UserAlert


# Two appids, not one. With a single appid `continue` and `return` produce
# IDENTICAL observable behaviour, so a `return` bug would pass every assertion.
# The second appid being reached is the only evidence the loop continued.
FIRST_APPID = 620
SECOND_APPID = 1145360
SECOND_GAME = {"name": "Hades", "price": 2499}

# Returned by the second appid in the continuation cases. Proving check_all
# RETURNS this — and never emits it — is the poller-refactor contract the
# Discord layer depends on.
SECOND_ALERT = UserAlert(
    discord_user_id=100000000000000002,
    appid=SECOND_APPID,
    message="Hades dropped to $24.99.",
)


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
            skipped_cycle_alerts = poller.run_poll_cycle()

check_all.assert_not_called()
log_operation.assert_called_once()
assert "Could not load watched appids" in log_operation.call_args.args[0]
assert skipped_cycle_alerts == [], (
    "A skipped cycle must return [], not None — the Discord task iterates it"
)
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
                [SECOND_ALERT],
            ],
        ) as process_price_observation:
            with patch.object(poller, "emit_user_alert") as emit_user_alert:
                with patch.object(poller, "log_operation") as log_operation:
                    returned_alerts = poller.check_all(
                        [FIRST_APPID, SECOND_APPID]
                    )

# The refactor contract: alerts come back as the RETURN VALUE and nothing is
# emitted from inside the cycle, even when there is an alert to emit. The
# Discord layer does the sending, on the event loop.
emit_user_alert.assert_not_called()
assert returned_alerts == [SECOND_ALERT], (
    f"check_all did not return the second appid's alerts: {returned_alerts}"
)
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
            return_value=[SECOND_ALERT],
        ) as process_price_observation:
            with patch.object(poller, "log_operation") as log_operation:
                returned_alerts = poller.check_all(
                    [FIRST_APPID, SECOND_APPID]
                )

log_operation.assert_called_once()
assert "Request failed for appid 620" in log_operation.call_args.args[0]
process_price_observation.assert_called_once()
assert_continued_to_second_appid(process_price_observation)
assert returned_alerts == [SECOND_ALERT]
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
                returned_alerts = poller.check_all(
                    [FIRST_APPID, SECOND_APPID]
                )

log_operation.assert_called_once()
assert "Could not find data for appid 620" in log_operation.call_args.args[0]
process_price_observation.assert_called_once()
assert_continued_to_second_appid(process_price_observation)
assert returned_alerts == []
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


# --- Defect D1: a price in the wrong currency is a read failure. ---
# Steam gates pricing on the requesting IP, not on cc=, so a deployed host can
# be quoted a different denomination than this laptop. The guard lives in
# get_game: a mismatched currency zeroes the price out, and the existing
# no-usable-price path then writes nothing.
def steam_price_response(price_overview: dict) -> Mock:
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        str(FIRST_APPID): {
            "success": True,
            "data": {
                "name": "Portal 2",
                "price_overview": price_overview,
            },
        }
    }
    return response


with patch.object(
    poller.requests,
    "get",
    return_value=steam_price_response({"currency": "EUR", "final": 899}),
):
    with patch.object(poller, "log_operation") as log_operation:
        mismatched_game = poller.get_game(FIRST_APPID)

assert mismatched_game == {"name": "Portal 2", "price": None}, (
    f"Wrong-currency price was not discarded: {mismatched_game}"
)
log_operation.assert_called_once()
assert "Currency mismatch" in log_operation.call_args.args[0]

with patch.object(
    poller.requests,
    "get",
    return_value=steam_price_response({"currency": "USD", "final": 899}),
):
    with patch.object(poller, "log_operation") as log_operation:
        matching_game = poller.get_game(FIRST_APPID)

assert matching_game == {"name": "Portal 2", "price": 899}, (
    f"Expected-currency price did not survive the guard: {matching_game}"
)
log_operation.assert_not_called()
print("Currency guard: mismatch discarded, expected currency kept")


# --- Defect D3: process_price_observation's own reject paths. ---
# These are the guards that enforce option B, and they must return [] BEFORE
# any connection is opened — so _connect is patched with a mock that would
# make the test fail loudly if it were ever reached. True is included
# deliberately: bool subclasses int, and the isinstance(new_price, bool) check
# exists specifically to stop True being stored as 1 cent.
with patch.object(database, "_connect") as connect:
    with patch.object(database, "log_operation") as log_operation:
        for unusable_price in (None, True, False, "999", 9.99, -1):
            guarded_alerts = database.process_price_observation(
                appid=FIRST_APPID,
                name="Portal 2",
                new_price=unusable_price,
            )

            assert guarded_alerts == [], (
                f"Unusable price {unusable_price!r} produced alerts"
            )

connect.assert_not_called()
assert log_operation.call_count == 6
print("Unusable-price guards: all six rejected before any connection")


# --- The recorded_game-is-None guard: an unknown appid is a RuntimeError. ---
# RECORD_SUCCESSFUL_FETCH returns no row when the appid has no games row,
# which means the observation cannot be recorded at all — a broken invariant,
# not a transient condition, so it must propagate and kill the process.
unknown_game_cursor = Mock()
unknown_game_cursor.fetchone.return_value = None

try:
    database._run_poll_transaction(
        unknown_game_cursor,
        {"appid": 999_999_999},
    )
except RuntimeError:
    pass
else:
    raise AssertionError(
        "_run_poll_transaction accepted an appid with no games row"
    )

print("Unknown-appid poll: raised instead of writing")
print("All poller error-handling cases passed.")
