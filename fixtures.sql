BEGIN;

-- Truncate the complete FK-related set together so the fixture is repeatable.
TRUNCATE TABLE watches, prices, games;

-- Parent rows must exist before prices or watches can reference them.
INSERT INTO games (appid, name, last_checked_at)
VALUES
    (620, 'Portal 2', '2026-07-27 12:00:00+00'),
    (413150, 'Stardew Valley', '2026-07-27 12:00:00+00'),
    (1145360, 'Hades', NULL);

-- Price history contains baselines and actual price changes only.
INSERT INTO prices (appid, cents, checked_at)
VALUES
    (620, 1299, '2026-07-27 10:00:00+00'),
    (620, 1099, '2026-07-27 11:00:00+00'),
    (620, 999, '2026-07-27 12:00:00+00'),
    (413150, 1499, '2026-07-27 12:00:00+00');

-- Alice and Bob watch Portal 2 in common, which exercises the game->users
-- fan-out. Stardew Valley has a price but no watcher until Carol, and Hades
-- has a watcher but no price at all.
--
-- alert_below and last_alerted_cents are INDEPENDENTLY nullable, so the alert
-- predicate has four cases. One row covers each; a query that silently drops
-- any of them has the three-valued-logic bug.
--
--   alert_below | last_alerted | row              | why it matters
--   ------------+--------------+------------------+---------------------------
--   set         | set          | Alice / Portal 2 | both bounds apply
--   NULL        | set          | Bob   / Portal 2 | ratchet only, no threshold
--   set         | NULL         | Bob   / Hades    | never alerted, no price
--   NULL        | NULL         | Carol / Stardew  | bare /watch, price exists
--
-- Portal 2's latest price is 999, so the Portal 2 rows are falsifiable: a
-- candidate of 899 must reach Bob, and 499 must reach both. A candidate of
-- 999 must reach NEITHER — the ratchet comparison is strict.
INSERT INTO watches (
    discord_user_id,
    appid,
    alert_below,
    last_alerted_cents
)
VALUES
    (100000000000000001, 620, 500, 999),
    (100000000000000002, 620, NULL, 999),
    (100000000000000002, 1145360, 1500, NULL),
    (100000000000000003, 413150, NULL, NULL);

COMMIT;
