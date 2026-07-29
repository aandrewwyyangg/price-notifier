import os

import psycopg
from dotenv import load_dotenv

from queries import FIND_WATCHERS_TO_NOTIFY


load_dotenv()

with psycopg.connect(
    host="127.0.0.1",
    port=5432,
    dbname="steam_notifier",
    user="postgres",
    password=os.getenv("POSTGRES_PASSWORD"),
) as conn:
    with conn.cursor() as cur:
        cases = [
            (
                "Portal 2 at 899",
                620,
                899,
                [100000000000000002],
            ),
            (
                "Portal 2 at 499",
                620,
                499,
                [
                    100000000000000001,
                    100000000000000002,
                ],
            ),
            (
                "Portal 2 at 999",
                620,
                999,
                [],
            ),
            (
                "Stardew with an unseeded ratchet",
                413150,
                1,
                [],
            ),
            (
                "Hades with an unseeded ratchet",
                1145360,
                499,
                [],
            ),
        ]

        for label, appid, new_price, expected in cases:
            cur.execute(
                FIND_WATCHERS_TO_NOTIFY,
                {
                    "appid": appid,
                    "new_price": new_price,
                },
            )

            actual = [row[0] for row in cur.fetchall()]
            print(f"{label}: {actual}")
            assert actual == expected, (
                f"{label}: expected {expected}, got {actual}"
            )

print("All watcher cases passed.")
