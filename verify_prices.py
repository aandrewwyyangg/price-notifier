import os
from datetime import datetime, timezone
import psycopg
from dotenv import load_dotenv
from queries import INSERT_PRICE_IF_CHANGED


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
            ("no-op", 999),
            ("insert", 899),
        ]

        for label, new_price in cases:
            cur.execute(
                INSERT_PRICE_IF_CHANGED,
                {
                    "appid": 620,
                    "new_price": new_price,
                    "observed_at": datetime.now(timezone.utc),
                },
            )
            print(f"{label}: {cur.fetchone()}")
