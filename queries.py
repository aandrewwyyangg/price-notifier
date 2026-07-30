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


# The poll list is derived, not configured. This retires the old APPIDS
# constant: a game is polled because someone watches it, and for no other
# reason.
GET_WATCHED_APPIDS = """
SELECT DISTINCT appid
FROM watches
ORDER BY appid;
"""


# appid only — name is filled later by the poller (see RECORD_SUCCESSFUL_FETCH),
# which is what keeps /watch a pure database write. DO NOTHING because a
# pre-existing game is not an error and the caller has no reason to care.
ENSURE_GAME_EXISTS = """
INSERT INTO games (appid)
VALUES (%(appid)s)
ON CONFLICT (appid) DO NOTHING;
"""


# Deliberately NO `ON CONFLICT` clause: a duplicate watch MUST raise
# UniqueViolation so the caller can answer "you're already watching this game."
# DO NOTHING here would swallow that and look correct while silently dropping
# the reply. Must run in the same transaction as ENSURE_GAME_EXISTS.
# last_alerted_cents is intentionally unset — it starts NULL and
# SEED_WATCH_RATCHETS fills it on the first successful poll.
INSERT_WATCH = """
INSERT INTO watches (
    discord_user_id,
    appid,
    alert_below
)
VALUES (
    %(discord_user_id)s,
    %(appid)s,
    %(alert_below)s
)
RETURNING
    discord_user_id,
    appid,
    alert_below,
    last_alerted_cents;
"""


# COALESCE(%(name)s, name) — direction matters and the two forms are opposites.
# This one lets a supplied Steam name REPLACE the stored one, so renames
# propagate, while a missing name preserves what is already there and a
# never-seen name stays NULL. COALESCE(name, %(name)s) would instead freeze
# the first name ever seen.
#
# OPEN QUESTION: this currently runs on any response, so a reply carrying no
# price still advances last_checked_at — which can mask the very staleness the
# column exists to reveal. See the design notes "What counts as a successful fetch".
RECORD_SUCCESSFUL_FETCH = """
UPDATE games
SET
    name = COALESCE(%(name)s, name),
    last_checked_at = %(observed_at)s
WHERE appid = %(appid)s
RETURNING
    appid,
    name,
    last_checked_at;
"""