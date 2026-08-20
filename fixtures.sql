BEGIN;
TRUNCATE TABLE watches, prices, games;
INSERT INTO games (appid, name, last_checked_at)
VALUES
    (620, 'Portal 2', '2026-07-27 12:00:00+00'),
    (413150, 'Stardew Valley', '2026-07-27 12:00:00+00'),
    (1145360, 'Hades', NULL),
    (292030, NULL, '2026-07-27 12:00:00+00');
INSERT INTO prices (appid, cents, checked_at)
VALUES
    (620, 1299, '2026-07-27 10:00:00+00'),
    (620, 1099, '2026-07-27 11:00:00+00'),
    (620, 999, '2026-07-27 12:00:00+00'),
    (413150, 1499, '2026-07-27 12:00:00+00'),
    (292030, 3999, '2026-07-27 12:00:00+00');
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
    (100000000000000003, 413150, NULL, NULL),
    (100000000000000001, 292030, NULL, 3999);

COMMIT;
