from datetime import datetime, timezone
from pathlib import Path

import psycopg

from database import connection_args
from queries import INSERT_PRICE_IF_CHANGED


# Importing database also runs its load_dotenv(), so .env is already applied.

fixture_path = Path(__file__).with_name("fixtures.sql")
fixture_sql = "\n".join(
    line
    for line in fixture_path.read_text(encoding="utf-8").splitlines()
    if line.strip().upper() not in {"BEGIN;", "COMMIT;"}
)

cases = [
    (
        "no-op",
        999,
        datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        False,
    ),
    (
        "insert",
        899,
        datetime(2026, 7, 29, 15, 1, tzinfo=timezone.utc),
        True,
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

        for label, new_price, observed_at, should_insert in cases:
            cur.execute(
                INSERT_PRICE_IF_CHANGED,
                {
                    "appid": 620,
                    "new_price": new_price,
                    "observed_at": observed_at,
                },
            )
            actual = cur.fetchone()

            expected = (
                (620, new_price, observed_at)
                if should_insert
                else None
            )

            assert actual == expected, (
                f"{label}: expected {expected}, got {actual}"
            )
            print(f"{label}: {actual}")
finally:
    conn.rollback()
    conn.close()

print("All price-insert cases passed.")