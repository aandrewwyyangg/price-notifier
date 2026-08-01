from datetime import datetime, timezone
from pathlib import Path

import psycopg

from database import _run_poll_transaction, connection_args

from output import (
    build_price_drop_alert,
    log_game_label,
    user_game_label,
)

from queries import (
    ENSURE_GAME_EXISTS,
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

conn = psycopg.connect(**connection_args())

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