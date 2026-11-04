"""The real database.py functions, run inside the rolled-back test transaction.

The app_db fixture turns each of their connections into a SAVEPOINT, so these
tests exercise the functions themselves, not a copy of their statements.
"""

import psycopg
import pytest

import database
from database import WatchResult, WatchSummary
from output import UserAlert, store_url
from queries import LOCK_USER_WATCHES

pytestmark = pytest.mark.db

ALICE = 100000000000000001
BOB = 100000000000000002
CAROL = 100000000000000003
NEW_USER = 900000000000000001


def watch_row(conn, discord_user_id, appid):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT discord_user_id, appid, alert_below, last_alerted_cents
            FROM watches
            WHERE discord_user_id = %s AND appid = %s;
            """,
            (discord_user_id, appid),
        )
        return cur.fetchone()


def game_exists(conn, appid):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM games WHERE appid = %s;", (appid,))
        return cur.fetchone() is not None


def test_create_watch_inserts_the_watch_and_its_game(app_db):
    assert database.create_watch(NEW_USER, 999_999_999, 750) is WatchResult.CREATED

    assert watch_row(app_db, NEW_USER, 999_999_999) == (NEW_USER, 999_999_999, 750, None)
    assert game_exists(app_db, 999_999_999)


def test_create_watch_reports_a_duplicate_and_leaves_the_row_alone(app_db):
    assert database.create_watch(ALICE, 620, 100) is WatchResult.ALREADY_WATCHING

    assert watch_row(app_db, ALICE, 620) == (ALICE, 620, 500, 999)


def test_create_watch_enforces_the_per_user_limit(app_db):
    # Alice watches two games in the fixtures.
    assert database.create_watch(ALICE, 999_999_998, max_watches=2) is WatchResult.LIMIT_REACHED
    assert watch_row(app_db, ALICE, 999_999_998) is None
    assert not game_exists(app_db, 999_999_998)

    assert database.create_watch(ALICE, 999_999_998, max_watches=3) is WatchResult.CREATED


def test_a_duplicate_is_reported_even_at_the_limit(app_db):
    assert database.create_watch(ALICE, 620, max_watches=2) is WatchResult.ALREADY_WATCHING


def test_create_watch_waits_while_the_same_user_has_one_in_flight(app_db, other_connection):
    # The other session is part-way through a /watch for Alice and holds her lock.
    other_connection.execute(LOCK_USER_WATCHES, {"discord_user_id": ALICE})
    app_db.execute("SET LOCAL lock_timeout = '200ms';")

    with pytest.raises(psycopg.errors.LockNotAvailable):
        database.create_watch(ALICE, 999_999_995)
    assert watch_row(app_db, ALICE, 999_999_995) is None

    # Other users don't share Alice's lock.
    assert database.create_watch(NEW_USER, 999_999_995) is WatchResult.CREATED


def test_a_duplicate_that_slips_past_the_check_is_still_reported(app_db, monkeypatch):
    # The race the lock prevents: a check that ran before the other /watch
    # committed. Only the primary key stops the insert, and it must still
    # read as a duplicate rather than an error.
    monkeypatch.setattr(
        database,
        "GET_WATCH_COUNT_AND_DUPLICATE",
        "SELECT 0, FALSE WHERE %(discord_user_id)s IS NOT NULL;",
    )

    assert database.create_watch(ALICE, 620, 100) is WatchResult.ALREADY_WATCHING
    assert watch_row(app_db, ALICE, 620) == (ALICE, 620, 500, 999)


def test_every_connection_has_a_statement_timeout(db):
    # The real _connect, not app_db's savepoint; it writes nothing.
    with database._connect() as conn:
        assert conn.execute("SHOW statement_timeout;").fetchone() == ("10s",)


def test_a_statement_past_the_timeout_is_cancelled(db, monkeypatch):
    monkeypatch.setattr(database, "STATEMENT_TIMEOUT", "50ms")
    conn = database._connect()
    try:
        with pytest.raises(psycopg.errors.QueryCanceled):
            conn.execute("SELECT pg_sleep(1);")
    finally:
        conn.close()


def test_a_rejected_watch_rolls_back_its_game_row(app_db):
    with pytest.raises(psycopg.errors.CheckViolation):
        database.create_watch(NEW_USER, 999_999_997, alert_below=-1)

    assert not game_exists(app_db, 999_999_997)


def test_delete_watch_reports_whether_anything_was_removed(app_db):
    assert database.delete_watch(CAROL, 413150) is True
    assert database.delete_watch(CAROL, 413150) is False


def test_get_user_watches_returns_summaries(app_db):
    assert database.get_user_watches(BOB) == [
        WatchSummary(appid=620, name="Portal 2", alert_below=None, latest_cents=999),
        WatchSummary(appid=1145360, name="Hades", alert_below=1500, latest_cents=None),
    ]
    assert database.get_user_watches(1) == []


def test_get_watched_appids_reads_the_poll_list(app_db):
    assert database.get_watched_appids() == [1145360, 620, 292030, 413150]


def test_missing_tables_is_empty_with_the_schema_applied(app_db):
    assert database.missing_tables() == []


def test_a_price_drop_updates_the_ratchet_and_returns_the_alert(app_db):
    alerts = database.process_price_observation(appid=292030, name=None, new_price=3499)

    assert alerts == [
        UserAlert(
            discord_user_id=ALICE,
            appid=292030,
            message=f"appid 292030 dropped to $34.99.\n{store_url(292030)}",
        )
    ]
    assert watch_row(app_db, ALICE, 292030)[3] == 3499

    # Same price again, now with a name: no alert, no new price row, name filled.
    assert database.process_price_observation(
        appid=292030, name="The Witcher 3", new_price=3499
    ) == []
    with app_db.cursor() as cur:
        cur.execute("SELECT cents FROM prices WHERE appid = 292030 ORDER BY checked_at;")
        assert [row[0] for row in cur.fetchall()] == [3999, 3499]
        cur.execute("SELECT name FROM games WHERE appid = 292030;")
        assert cur.fetchone() == ("The Witcher 3",)


def test_the_first_observation_seeds_silently_then_a_drop_alerts(app_db):
    assert database.process_price_observation(
        appid=413150, name="Stardew Valley", new_price=1499
    ) == []
    assert watch_row(app_db, CAROL, 413150)[3] == 1499

    alerts = database.process_price_observation(
        appid=413150, name="Stardew Valley", new_price=1399
    )
    assert [alert.discord_user_id for alert in alerts] == [CAROL]


def test_an_unknown_appid_raises_and_writes_nothing(app_db):
    with pytest.raises(RuntimeError):
        database.process_price_observation(appid=999_999_996, name="Ghost", new_price=100)

    with app_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM prices WHERE appid = 999999996;")
        assert cur.fetchone() == (0,)
