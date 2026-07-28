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
-- fan-out. Bob's Hades watch is the deliberate NULL-ratchet case: never
-- alerted, no baseline, and Hades has no prices row at all. Any alert query
-- that silently drops this row has the three-valued-logic bug.
INSERT INTO watches (
    discord_user_id,
    appid,
    alert_below,
    last_alerted_cents
)
VALUES
    (100000000000000001, 620, 500, 999),
    (100000000000000002, 620, NULL, 999),
    (100000000000000002, 1145360, 1500, NULL);

COMMIT;
