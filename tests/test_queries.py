from datetime import datetime, timezone

import psycopg
import pytest

from database import REQUIRED_TABLES, _run_poll_transaction
from output import build_price_drop_alert
from queries import (
    DELETE_WATCH,
    ENSURE_GAME_EXISTS,
    FIND_MISSING_TABLES,
    GET_USER_WATCHES,
    GET_WATCH_COUNT_AND_DUPLICATE,
    GET_WATCHED_APPIDS,
    INSERT_PRICE_IF_CHANGED,
    INSERT_WATCH,
    RATCHET_WATCHERS_TO_NOTIFY,
    RECORD_SUCCESSFUL_FETCH,
    SEED_WATCH_RATCHETS,
)

pytestmark = pytest.mark.db

ALICE = 100000000000000001
BOB = 100000000000000002
CAROL = 100000000000000003
NEW_USER = 900000000000000001


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


def watch_row(cur, discord_user_id, appid):
    cur.execute(
        """
        SELECT discord_user_id, appid, alert_below, last_alerted_cents
        FROM watches
        WHERE discord_user_id = %s AND appid = %s;
        """,
        (discord_user_id, appid),
    )
    return cur.fetchone()


def game_exists(cur, appid):
    cur.execute("SELECT 1 FROM games WHERE appid = %s;", (appid,))
    return cur.fetchone() is not None


def test_insert_price_if_changed_skips_an_unchanged_price(cur):
    cur.execute(
        INSERT_PRICE_IF_CHANGED,
        {"appid": 620, "new_price": 999, "observed_at": utc(2026, 7, 29, 15, 0)},
    )
    assert cur.fetchone() is None


def test_insert_price_if_changed_records_a_change(cur):
    observed_at = utc(2026, 7, 29, 15, 1)
    cur.execute(
        INSERT_PRICE_IF_CHANGED,
        {"appid": 620, "new_price": 899, "observed_at": observed_at},
    )
    assert cur.fetchone() == (620, 899, observed_at)


# Each step runs against the state the previous steps left behind.
RATCHET_SEQUENCE = [
    ("Portal 2 at 999 - equal", 620, 999, 0, []),
    ("Portal 2 at 1099 - rise", 620, 1099, 0, []),
    ("Portal 2 at 899 - first drop", 620, 899, 0, [BOB]),
    ("Portal 2 at 899 - repeated", 620, 899, 0, []),
    ("Portal 2 at 500 - equal to Alice's threshold", 620, 500, 0, [BOB]),
    ("Portal 2 at 499 - deeper drop", 620, 499, 0, [ALICE, BOB]),
    ("Stardew Valley - silent seed", 413150, 1499, 1, []),
    ("Stardew Valley - drop after seed", 413150, 1399, 0, [CAROL]),
    ("Hades - silent seed", 1145360, 1499, 1, []),
    ("Hades - drop after seed", 1145360, 1399, 0, [BOB]),
    ("Unnamed game at 3499 - drop", 292030, 3499, 0, [ALICE]),
]


def test_the_ratchet_alerts_only_on_new_lows_below_the_threshold(cur):
    for label, appid, new_price, expected_seeded, expected_users in RATCHET_SEQUENCE:
        parameters = {"appid": appid, "new_price": new_price}

        cur.execute(SEED_WATCH_RATCHETS, parameters)
        seeded = cur.rowcount
        cur.execute(RATCHET_WATCHERS_TO_NOTIFY, parameters)
        users = sorted(row[0] for row in cur.fetchall())

        assert seeded == expected_seeded, f"{label}: seeded {seeded}"
        assert users == expected_users, f"{label}: notified {users}"


def test_get_watched_appids_lists_each_game_once_least_recently_checked_first(cur):
    # Hades has never been checked, so it goes first. The other three were
    # checked at the same moment and fall back to appid order.
    cur.execute(GET_WATCHED_APPIDS)
    assert [row[0] for row in cur.fetchall()] == [1145360, 620, 292030, 413150]


def test_a_checked_game_moves_to_the_back_of_the_poll_list(cur):
    # A game a rate limit cut off keeps its older timestamp, so it goes first
    # next cycle instead of being skipped every time.
    cur.execute(
        RECORD_SUCCESSFUL_FETCH,
        {"appid": 1145360, "name": "Hades", "observed_at": utc(2026, 7, 29, 12, 0)},
    )
    cur.execute(GET_WATCHED_APPIDS)
    assert [row[0] for row in cur.fetchall()] == [620, 292030, 413150, 1145360]


def test_get_user_watches_covers_both_nullables_and_the_empty_result(cur):
    cur.execute(GET_USER_WATCHES, {"discord_user_id": BOB})
    assert cur.fetchall() == [
        (620, "Portal 2", None, 999),
        (1145360, "Hades", 1500, None),
    ]

    cur.execute(GET_USER_WATCHES, {"discord_user_id": ALICE})
    assert cur.fetchall() == [
        (620, "Portal 2", 500, 999),
        (292030, None, None, 3999),
    ]

    cur.execute(GET_USER_WATCHES, {"discord_user_id": 1})
    assert cur.fetchall() == []


def test_delete_watch_distinguishes_removed_from_absent_and_keeps_the_game(cur):
    parameters = {"discord_user_id": CAROL, "appid": 413150}

    cur.execute(DELETE_WATCH, parameters)
    assert cur.fetchone() == (413150,)

    cur.execute(DELETE_WATCH, parameters)
    assert cur.fetchone() is None

    cur.execute(GET_WATCHED_APPIDS)
    assert [row[0] for row in cur.fetchall()] == [1145360, 620, 292030]
    assert game_exists(cur, 413150)


def test_ensure_game_exists_inserts_once_then_does_nothing(cur):
    appid = 999_999_999

    cur.execute(ENSURE_GAME_EXISTS, {"appid": appid})
    assert cur.rowcount == 1
    cur.execute(ENSURE_GAME_EXISTS, {"appid": appid})
    assert cur.rowcount == 0

    cur.execute("SELECT appid, name, last_checked_at FROM games WHERE appid = %s;", (appid,))
    assert cur.fetchone() == (appid, None, None)


def test_insert_watch_starts_with_no_ratchet(cur):
    cur.execute(
        INSERT_WATCH,
        {"discord_user_id": NEW_USER, "appid": 620, "alert_below": 750},
    )
    assert cur.fetchone() == (NEW_USER, 620, 750, None)


def test_a_duplicate_watch_raises_and_keeps_the_existing_row(db, cur):
    parameters = {"discord_user_id": NEW_USER, "appid": 620, "alert_below": 750}
    cur.execute(INSERT_WATCH, parameters)

    # A savepoint, so the expected error does not abort the test transaction.
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            cur.execute(INSERT_WATCH, {**parameters, "alert_below": 100})

    assert watch_row(cur, NEW_USER, 620) == (NEW_USER, 620, 750, None)


def test_a_failed_watch_transaction_leaves_both_tables_unchanged(db, cur):
    appid = 999_999_998

    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            cur.execute(ENSURE_GAME_EXISTS, {"appid": appid})
            assert cur.rowcount == 1
            cur.execute(
                INSERT_WATCH,
                {"discord_user_id": NEW_USER, "appid": appid, "alert_below": -1},
            )

    assert not game_exists(cur, appid)
    assert watch_row(cur, NEW_USER, appid) is None


def test_the_poll_transaction_falls_back_to_the_appid_for_a_null_name(cur):
    parameters = {
        "appid": 292030,
        "name": None,
        "new_price": 3499,
        "observed_at": utc(2026, 7, 29, 11, 0),
    }

    result = _run_poll_transaction(cur, parameters)

    assert result.stored_name is None
    assert result.price_recorded is True
    assert result.seeded_watch_count == 0
    assert result.discord_user_ids == (ALICE,)
    alert = build_price_drop_alert(
        discord_user_id=ALICE,
        appid=292030,
        name=result.stored_name,
        new_price=3499,
    )
    assert alert.message.startswith("appid 292030 dropped to $34.99.")


def test_record_successful_fetch_fills_preserves_and_renames(cur):
    appid = 292030
    steps = [
        ("Initial Name", utc(2026, 7, 29, 12, 0), "Initial Name"),
        (None, utc(2026, 7, 29, 13, 0), "Initial Name"),
        ("Renamed Game", utc(2026, 7, 29, 14, 0), "Renamed Game"),
    ]

    for supplied_name, observed_at, expected_name in steps:
        cur.execute(
            RECORD_SUCCESSFUL_FETCH,
            {"appid": appid, "name": supplied_name, "observed_at": observed_at},
        )
        assert cur.fetchone() == (appid, expected_name, observed_at)

    cur.execute("SELECT appid, name, last_checked_at FROM games WHERE appid = %s;", (appid,))
    assert cur.fetchone() == (appid, "Renamed Game", utc(2026, 7, 29, 14, 0))


def test_watch_count_and_duplicate(cur):
    cur.execute(GET_WATCH_COUNT_AND_DUPLICATE, {"discord_user_id": BOB, "appid": 620})
    assert cur.fetchone() == (2, True)

    cur.execute(GET_WATCH_COUNT_AND_DUPLICATE, {"discord_user_id": BOB, "appid": 413150})
    assert cur.fetchone() == (2, False)

    cur.execute(GET_WATCH_COUNT_AND_DUPLICATE, {"discord_user_id": 1, "appid": 620})
    assert cur.fetchone() == (0, False)


def test_find_missing_tables_with_the_full_schema(cur):
    cur.execute(FIND_MISSING_TABLES, {"tables": list(REQUIRED_TABLES)})
    assert cur.fetchall() == []

    cur.execute(FIND_MISSING_TABLES, {"tables": ["games", "no_such_table"]})
    assert cur.fetchall() == [("no_such_table",)]
