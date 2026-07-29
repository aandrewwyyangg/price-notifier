"""Parameterized PostgreSQL queries for the database milestone."""


# `observed_at` is captured by the poller when Steam returns the price.
# The database timestamp default remains a fallback for manual inserts.
INSERT_PRICE_IF_CHANGED = """
INSERT INTO prices (appid, cents, checked_at)
SELECT %(appid)s, %(new_price)s, %(observed_at)s
WHERE %(new_price)s IS DISTINCT FROM (
    SELECT cents
    FROM prices
    WHERE appid = %(appid)s
    ORDER BY checked_at DESC
    LIMIT 1
)
RETURNING appid, cents, checked_at;
"""
SEED_WATCH_RATCHETS = """
UPDATE watches
SET last_alerted_cents = %(new_price)s
WHERE appid = %(appid)s
  AND last_alerted_cents IS NULL;
"""


RATCHET_WATCHERS_TO_NOTIFY = """
UPDATE watches
SET last_alerted_cents = %(new_price)s
WHERE appid = %(appid)s
  AND last_alerted_cents IS NOT NULL
  AND %(new_price)s < last_alerted_cents
  AND (
      alert_below IS NULL
      OR %(new_price)s < alert_below
  )
RETURNING discord_user_id;
"""