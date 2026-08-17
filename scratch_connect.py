"""Smallest possible Python-to-Postgres proof: SELECT 1 over TCP.

Kept because it is the first thing to run when anything database-shaped
breaks — if this prints (1,), the driver, port, auth, and network are all
ruled out in one go. All connection parameters come from connection_args(),
same as the real code, so this tests the same path the bot uses.
"""

import psycopg

from database import connection_args

with psycopg.connect(**connection_args()) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT 1;")
        print(cur.fetchone())
