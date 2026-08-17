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


# Keyed on the primary key, so this is an index lookup, not a scan. RETURNING
# lets the caller distinguish "removed" from "you weren't watching that" —
# deleting zero rows is not an error, but it is a different reply.
# The games row is deliberately NOT deleted when the last watcher leaves:
# prices is history worth keeping, the FK is ON DELETE RESTRICT so game
# removal stays deliberate, and GET_WATCHED_APPIDS reads from watches — an
# unwatched game stops being polled the moment this row is gone.
DELETE_WATCH = """
DELETE FROM watches
WHERE discord_user_id = %(discord_user_id)s
  AND appid = %(appid)s
RETURNING appid;
"""


# The one query in the project that needs a JOIN: watches has the appid,
# games has the name. /list DOES show the latest price — the LATERAL subquery
# is the same latest-price-per-appid shape as INSERT_PRICE_IF_CHANGED's, and
# a watch list without prices answers "what am I watching" but not "was the
# threshold worth setting". LEFT JOIN LATERAL, not JOIN: a watched game with
# no price history yet (fixture 1145360) must still appear, with cents NULL.
# games.name can be NULL too (fixture 292030) — rendering falls back to the
# appid, like every other output path.
GET_USER_WATCHES = """
SELECT
    watches.appid,
    games.name,
    watches.alert_below,
    latest.cents
FROM watches
JOIN games
    ON games.appid = watches.appid
LEFT JOIN LATERAL (
    SELECT cents
    FROM prices
    WHERE prices.appid = watches.appid
    ORDER BY checked_at DESC
    LIMIT 1
) AS latest ON TRUE
WHERE watches.discord_user_id = %(discord_user_id)s
ORDER BY watches.appid;
"""


# COALESCE(%(name)s, name) — direction matters and the two forms are opposites.
# This one lets a supplied Steam name REPLACE the stored one, so renames
# propagate, while a missing name preserves what is already there and a
# never-seen name stays NULL. COALESCE(name, %(name)s) would instead freeze
# the first name ever seen.
#
# CALLER INVARIANT: run this ONLY once Steam has returned a USABLE PRICE.
# "Successful fetch" was decided as option B — a usable price, not merely an
# HTTP response — and the guard that enforces it lives one level up, in
# database.py::process_price_observation, which returns before reaching this
# query. The query itself cannot check: it never sees the price.
# Advancing last_checked_at on a price-less reply would make a dead fetcher
# look healthy, masking the exact staleness the column exists to reveal.
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