import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from queries import (
    RATCHET_WATCHERS_TO_NOTIFY,
    SEED_WATCH_RATCHETS,
)


load_dotenv()

connection_args = {
    "host": "127.0.0.1",
    "port": 5432,
    "dbname": "steam_notifier",
    "user": "postgres",
    "password": os.getenv("POSTGRES_PASSWORD"),
}

fixture_path = Path(__file__).with_name("fixtures.sql")
fixture_sql = "\n".join(
    line
    for line in fixture_path.read_text(encoding="utf-8").splitlines()
    if line.strip().upper() not in {"BEGIN;", "COMMIT;"}
)

cases = [
    (
        "Portal 2 at 999 - equal",
        620,
        999,
        0,
        [],
    ),
    (
        "Portal 2 at 1099 - rise",
        620,
        1099,
        0,
        [],
    ),
    (
        "Portal 2 at 899 - first drop",
        620,
        899,
        0,
        [100000000000000002],
    ),
    (
        "Portal 2 at 899 - repeated",
        620,
        899,
        0,
        [],
    ),
    (
        "Portal 2 at 499 - deeper drop",
        620,
        499,
        0,
        [
            100000000000000001,
            100000000000000002,
        ],
    ),
    (
        "Stardew Valley - silent seed",
        413150,
        1499,
        1,
        [],
    ),
    (
        "Stardew Valley - drop after seed",
        413150,
        1399,
        0,
        [100000000000000003],
    ),
    (
        "Hades - silent seed",
        1145360,
        1499,
        1,
        [],
    ),
    (
        "Hades - drop after seed",
        1145360,
        1399,
        0,
        [100000000000000002],
    ),
]

conn = psycopg.connect(**connection_args)

try:
    with conn.cursor() as cur:
        cur.execute(fixture_sql)

        for label, appid, new_price, expected_seeded, expected_users in cases:
            parameters = {
                "appid": appid,
                "new_price": new_price,
            }

            cur.execute(SEED_WATCH_RATCHETS, parameters)
            actual_seeded = cur.rowcount

            cur.execute(RATCHET_WATCHERS_TO_NOTIFY, parameters)
            actual_users = sorted(row[0] for row in cur.fetchall())

            assert actual_seeded == expected_seeded, (
                f"{label}: expected {expected_seeded} seeded, "
                f"got {actual_seeded}"
            )
            assert actual_users == expected_users, (
                f"{label}: expected {expected_users}, "
                f"got {actual_users}"
            )

            print(f"{label}: {actual_users}")
finally:
    conn.rollback()
    conn.close()

print("All watch-ratchet cases passed.")