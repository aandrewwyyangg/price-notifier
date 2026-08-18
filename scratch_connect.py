import psycopg

from database import connection_args

with psycopg.connect(**connection_args()) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT 1;")
        print(cur.fetchone())
