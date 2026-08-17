from pathlib import Path

import psycopg

from database import connection_args
from queries import (
    RATCHET_WATCHERS_TO_NOTIFY,
    SEED_WATCH_RATCHETS,
)


# Importing database also runs its load_dotenv(), so .env is already applied.

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
    # DECIDED (defect D4): alert_below is STRICT — the column is named
    # alert_BELOW, so a price equal to the threshold does not fire. 500 equals
    # Alice's threshold exactly: her ratchet (999) would allow it, the
    # threshold must veto it. Bob has no threshold and a ratchet of 899, so he
    # is the proof the price itself is alertable. Before this case existed,
    # mutating `< alert_below` to `<=` broke nothing — no fixture case ever
    # landed ON the boundary.
    (
        "Portal 2 at 500 - equal to Alice's threshold",
        620,
        500,
        0,
        [100000000000000002],
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
    (
        "Unnamed game at 3499 - drop",
        292030,
        3499,
        0,
        [100000000000000001],
    ),
]

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