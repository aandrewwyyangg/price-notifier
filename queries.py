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

GET_WATCHED_APPIDS = """
SELECT DISTINCT appid
FROM watches
ORDER BY appid;
"""

ENSURE_GAME_EXISTS = """
INSERT INTO games (appid)
VALUES (%(appid)s)
ON CONFLICT (appid) DO NOTHING;
"""

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

DELETE_WATCH = """
DELETE FROM watches
WHERE discord_user_id = %(discord_user_id)s
  AND appid = %(appid)s
RETURNING appid;
"""

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