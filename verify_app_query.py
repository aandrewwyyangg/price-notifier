from datetime import datetime, timezone
from pathlib import Path

import psycopg

from database import _run_poll_transaction, connection_args

from output import (
    build_price_drop_alert,
    build_watch_line,
    log_game_label,
    user_game_label,
)

from queries import (
    DELETE_WATCH,
    ENSURE_GAME_EXISTS,
    GET_USER_WATCHES,
    GET_WATCHED_APPIDS,
    INSERT_WATCH,
    RECORD_SUCCESSFUL_FETCH,
)


# Importing database also runs its load_dotenv(), so .env is already applied.

fixture_path = Path(__file__).with_name("fixtures.sql")
fixture_sql = "\n".join(
    line
    for line in fixture_path.read_text(encoding="utf-8").splitlines()
    if line.strip().upper() not in {"BEGIN;", "COMMIT;"}
)

# This script reloads fixtures.sql, which TRUNCATEs every table it touches.
# Rollback makes that safe locally, but pointing .env at a managed database
# and running this would still take an ACCESS EXCLUSIVE lock on live tables —
# so anything non-local is refused outright.
connection_parameters = connection_args()
assert connection_parameters["host"] in {"127.0.0.1", "localhost", "::1"}, (
    f"Refusing to run fixtures against non-local host "
    f"{connection_parameters['host']!r}"
)

conn = psycopg.connect(**connection_parameters)

try:
    with conn.cursor() as cur:
        cur.execute(fixture_sql)

        # GET_WATCHED_APPIDS returns each watched appid once, in numeric order.
        cur.execute(GET_WATCHED_APPIDS)
        actual_appids = [row[0] for row in cur.fetchall()]
        expected_appids = [620, 292030, 413150, 1145360]

        assert actual_appids == expected_appids, (
            f"GET_WATCHED_APPIDS: expected {expected_appids}, "
            f"got {actual_appids}"
        )
        print(f"GET_WATCHED_APPIDS: {actual_appids}")

        # GET_USER_WATCHES joins watches to games and pulls the latest price
        # per appid. Between them, Bob and Alice cover both nullables: Hades
        # has a watch and NO price history (latest cents NULL), and 292030 has
        # a NULL name. These run BEFORE the poll-transaction case below, which
        # would otherwise move 292030's latest price.
        cur.execute(
            GET_USER_WATCHES,
            {"discord_user_id": 100000000000000002},
        )
        bob_watches = cur.fetchall()

        assert bob_watches == [
            (620, "Portal 2", None, 999),
            (1145360, "Hades", 1500, None),
        ], f"GET_USER_WATCHES (Bob): {bob_watches}"

        cur.execute(
            GET_USER_WATCHES,
            {"discord_user_id": 100000000000000001},
        )
        alice_watches = cur.fetchall()

        assert alice_watches == [
            (620, "Portal 2", 500, 999),
            (292030, None, None, 3999),
        ], f"GET_USER_WATCHES (Alice): {alice_watches}"

        # Rendering the same rows: a missing price is "unavailable", a NULL
        # name falls back to the appid, and a NULL threshold adds no clause.
        assert build_watch_line(*bob_watches[1]) == (
            "Hades — latest price unavailable, alerting below $15.00"
        ), f"build_watch_line: {build_watch_line(*bob_watches[1])}"
        assert build_watch_line(*alice_watches[1]) == (
            "appid 292030 — latest price $39.99"
        ), f"build_watch_line: {build_watch_line(*alice_watches[1])}"

        # An unknown user gets an empty list — a normal answer, not an error.
        cur.execute(GET_USER_WATCHES, {"discord_user_id": 1})
        unknown_user_watches = cur.fetchall()

        assert unknown_user_watches == [], (
            f"GET_USER_WATCHES (unknown user): {unknown_user_watches}"
        )
        print("GET_USER_WATCHES: both nullables and the empty result covered")

        # DELETE_WATCH's RETURNING distinguishes "removed" from "you weren't
        # watching that" — different replies, so the difference must surface.
        delete_parameters = {
            "discord_user_id": 100000000000000003,
            "appid": 413150,
        }

        cur.execute(DELETE_WATCH, delete_parameters)
        removed_row = cur.fetchone()

        assert removed_row == (413150,), (
            f"DELETE_WATCH did not report the removed watch: {removed_row}"
        )

        cur.execute(DELETE_WATCH, delete_parameters)
        second_removed_row = cur.fetchone()

        assert second_removed_row is None, (
            f"Deleting an absent watch returned a row: {second_removed_row}"
        )

        # Carol was 413150's only watcher, so the poll list shrinks by itself
        # — GET_WATCHED_APPIDS reads from watches — while the games row and
        # its price history deliberately stay put.
        cur.execute(GET_WATCHED_APPIDS)
        appids_after_delete = [row[0] for row in cur.fetchall()]

        assert appids_after_delete == [620, 292030, 1145360], (
            f"Poll list after DELETE_WATCH: {appids_after_delete}"
        )

        cur.execute("SELECT 1 FROM games WHERE appid = 413150;")
        assert cur.fetchone() is not None, (
            "DELETE_WATCH must not remove the games row"
        )
        print("DELETE_WATCH: removed vs absent distinguished, game row kept")

        # ENSURE_GAME_EXISTS inserts an unknown appid once, then becomes a no-op.
        ensured_appid = 999_999_999
        ensure_parameters = {"appid": ensured_appid}

        cur.execute(ENSURE_GAME_EXISTS, ensure_parameters)
        first_ensure_rowcount = cur.rowcount

        cur.execute(ENSURE_GAME_EXISTS, ensure_parameters)
        second_ensure_rowcount = cur.rowcount

        cur.execute(
            """
            SELECT appid, name, last_checked_at
            FROM games
            WHERE appid = %s;
            """,
            (ensured_appid,),
        )
        ensured_game = cur.fetchone()

        assert first_ensure_rowcount == 1, (
            "ENSURE_GAME_EXISTS first call: "
            f"expected rowcount 1, got {first_ensure_rowcount}"
        )
        assert second_ensure_rowcount == 0, (
            "ENSURE_GAME_EXISTS second call: "
            f"expected rowcount 0, got {second_ensure_rowcount}"
        )
        assert ensured_game == (ensured_appid, None, None), (
            "ENSURE_GAME_EXISTS stored unexpected values: "
            f"{ensured_game}"
        )
        print("ENSURE_GAME_EXISTS: inserted once, then no-op")

        # INSERT_WATCH creates a watch with an intentionally unseeded ratchet.
        watch_parameters = {
            "discord_user_id": 900_000_000_000_000_001,
            "appid": 620,
            "alert_below": 750,
        }
        expected_watch = (
            watch_parameters["discord_user_id"],
            watch_parameters["appid"],
            watch_parameters["alert_below"],
            None,
        )

        cur.execute(INSERT_WATCH, watch_parameters)
        inserted_watch = cur.fetchone()

        assert inserted_watch == expected_watch, (
            f"INSERT_WATCH: expected {expected_watch}, "
            f"got {inserted_watch}"
        )
        print(f"INSERT_WATCH: {inserted_watch}")

        # Model create_watch's exact statement order. Because the game already
        # exists, ENSURE_GAME_EXISTS is a no-op. The duplicate INSERT_WATCH must
        # raise, and the savepoint must restore a usable outer transaction.
        try:
            with conn.transaction():
                cur.execute(
                    ENSURE_GAME_EXISTS,
                    {"appid": watch_parameters["appid"]},
                )
                assert cur.rowcount == 0

                cur.execute(INSERT_WATCH, watch_parameters)
        except psycopg.errors.UniqueViolation:
            pass
        else:
            raise AssertionError(
                "Duplicate INSERT_WATCH did not raise UniqueViolation"
            )

        cur.execute(
            """
            SELECT
                discord_user_id,
                appid,
                alert_below,
                last_alerted_cents
            FROM watches
            WHERE discord_user_id = %s
              AND appid = %s;
            """,
            (
                watch_parameters["discord_user_id"],
                watch_parameters["appid"],
            ),
        )
        watch_after_duplicate = cur.fetchone()

        assert watch_after_duplicate == expected_watch, (
            "Duplicate INSERT_WATCH changed the existing row: "
            f"{watch_after_duplicate}"
        )
        print("Duplicate INSERT_WATCH: raised and preserved existing watch")

        # A duplicate path cannot insert its parent game because that parent
        # must already exist. Use a new appid plus a deliberately invalid watch
        # to prove that a real ENSURE_GAME_EXISTS write rolls back when the
        # following INSERT_WATCH fails.
        rollback_appid = 999_999_998
        invalid_watch_parameters = {
            "discord_user_id": 900_000_000_000_000_002,
            "appid": rollback_appid,
            "alert_below": -1,
        }

        try:
            with conn.transaction():
                cur.execute(
                    ENSURE_GAME_EXISTS,
                    {"appid": rollback_appid},
                )
                assert cur.rowcount == 1

                cur.execute(
                    INSERT_WATCH,
                    invalid_watch_parameters,
                )
        except psycopg.errors.CheckViolation:
            pass
        else:
            raise AssertionError(
                "Invalid INSERT_WATCH did not raise CheckViolation"
            )

        cur.execute(
            "SELECT 1 FROM games WHERE appid = %s;",
            (rollback_appid,),
        )
        rolled_back_game = cur.fetchone()

        cur.execute(
            """
            SELECT 1
            FROM watches
            WHERE discord_user_id = %s
              AND appid = %s;
            """,
            (
                invalid_watch_parameters["discord_user_id"],
                rollback_appid,
            ),
        )
        rolled_back_watch = cur.fetchone()

        assert rolled_back_game is None, (
            "ENSURE_GAME_EXISTS was not rolled back after INSERT_WATCH failed"
        )
        assert rolled_back_watch is None, (
            "Failed INSERT_WATCH left a watch row behind"
        )
        print("Failed watch transaction: both tables unchanged")
        # Exercise the complete poll transaction against the fixture's
        # intentionally unnamed game. The surrounding verifier transaction
        # rolls every change back in finally.
        poll_observed_at = datetime(
            2026,
            7,
            29,
            11,
            0,
            tzinfo=timezone.utc,
        )
        poll_parameters = {
            "appid": 292030,
            "name": None,
            "new_price": 3499,
            "observed_at": poll_observed_at,
        }

        poll_result = _run_poll_transaction(
            cur,
            poll_parameters,
        )

        assert poll_result.stored_name is None, (
            "Poll transaction replaced the fixture's NULL name"
        )
        assert poll_result.price_recorded is True, (
            "Poll transaction did not record the changed price"
        )
        assert poll_result.seeded_watch_count == 0, (
            "Poll transaction unexpectedly seeded an existing ratchet"
        )
        assert poll_result.discord_user_ids == (
            100000000000000001,
        ), (
            "Poll transaction returned unexpected users: "
            f"{poll_result.discord_user_ids}"
        )

        fallback_alert = build_price_drop_alert(
            discord_user_id=poll_result.discord_user_ids[0],
            appid=poll_parameters["appid"],
            name=poll_result.stored_name,
            new_price=poll_parameters["new_price"],
        ) 

        assert fallback_alert.appid == poll_parameters["appid"], (
            "UserAlert did not retain the appid needed for delivery logs: "
            f"{fallback_alert.appid}"
        )

        assert fallback_alert.message == (
            "appid 292030 dropped to $34.99."
        ), (
            "NULL-name alert did not fall back to the appid: "
            f"{fallback_alert.message}"
        )

        assert user_game_label(620, "Portal 2") == "Portal 2"
        assert log_game_label(620, "Portal 2") == "Portal 2 (620)"
        assert user_game_label(292030, None) == "appid 292030"
        assert log_game_label(292030, None) == "appid 292030"

        print(f"Poll transaction fallback: {fallback_alert}")


        # RECORD_SUCCESSFUL_FETCH fills a NULL name.
        record_appid = 292030
        first_observed_at = datetime(
            2026,
            7,
            29,
            12,
            0,
            tzinfo=timezone.utc,
        )

        cur.execute(
            RECORD_SUCCESSFUL_FETCH,
            {
                "appid": record_appid,
                "name": "Initial Name",
                "observed_at": first_observed_at,
            },
        )
        filled_name = cur.fetchone()

        assert filled_name == (
            record_appid,
            "Initial Name",
            first_observed_at,
        ), f"Fill NULL name: got {filled_name}"
        print(f"RECORD_SUCCESSFUL_FETCH fill: {filled_name}")

        # A missing name preserves the stored name while advancing the time.
        second_observed_at = datetime(
            2026,
            7,
            29,
            13,
            0,
            tzinfo=timezone.utc,
        )

        cur.execute(
            RECORD_SUCCESSFUL_FETCH,
            {
                "appid": record_appid,
                "name": None,
                "observed_at": second_observed_at,
            },
        )
        preserved_name = cur.fetchone()

        assert preserved_name == (
            record_appid,
            "Initial Name",
            second_observed_at,
        ), f"Preserve name on None: got {preserved_name}"
        print(f"RECORD_SUCCESSFUL_FETCH preserve: {preserved_name}")

        # A supplied replacement name propagates a Steam rename.
        third_observed_at = datetime(
            2026,
            7,
            29,
            14,
            0,
            tzinfo=timezone.utc,
        )

        cur.execute(
            RECORD_SUCCESSFUL_FETCH,
            {
                "appid": record_appid,
                "name": "Renamed Game",
                "observed_at": third_observed_at,
            },
        )
        renamed_game = cur.fetchone()

        assert renamed_game == (
            record_appid,
            "Renamed Game",
            third_observed_at,
        ), f"Propagate rename: got {renamed_game}"

        cur.execute(
            """
            SELECT appid, name, last_checked_at
            FROM games
            WHERE appid = %s;
            """,
            (record_appid,),
        )
        stored_game = cur.fetchone()

        assert stored_game == renamed_game, (
            f"Stored game does not match RETURNING row: {stored_game}"
        )
        print(f"RECORD_SUCCESSFUL_FETCH rename: {renamed_game}")
finally:
    conn.rollback()
    conn.close()

print("All app query cases passed.")