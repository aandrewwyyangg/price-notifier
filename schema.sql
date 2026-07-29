CREATE TABLE games (
    appid INTEGER PRIMARY KEY,

    -- NULL until the poller completes its first successful Steam fetch.
    -- appid is the game's IDENTITY; name is derived Steam metadata that may
    -- be temporarily unknown. Keeping this NOT NULL would force /watch to
    -- fetch from Steam synchronously — the exact availability problem the
    -- nullable ratchet was chosen to avoid. Output must fall back to the
    -- appid whenever this is NULL; never invent a placeholder name.
    name TEXT,

    last_checked_at TIMESTAMPTZ,

    CHECK (appid > 0)
);


CREATE TABLE prices (
    appid INTEGER NOT NULL,
    cents INTEGER NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (appid, checked_at),

    FOREIGN KEY (appid)
        REFERENCES games (appid)
        ON DELETE CASCADE,

    CHECK (cents >= 0)
);


CREATE TABLE watches (
    discord_user_id BIGINT NOT NULL,
    appid INTEGER NOT NULL,
    alert_below INTEGER,

    -- NULL = no reference point yet; this watch has never alerted. Seeded
    -- silently by the poller's first observation, never at watch creation.
    -- Any predicate on this column needs an explicit NULL branch: comparing
    -- to NULL yields NULL, and WHERE drops the row without erroring.
    -- The CHECK below is satisfied by NULL and will not catch a missing one.
    last_alerted_cents INTEGER,

    PRIMARY KEY (discord_user_id, appid),

    FOREIGN KEY (appid)
        REFERENCES games (appid)
        ON DELETE RESTRICT,

    CHECK (discord_user_id > 0),
    CHECK (alert_below >= 0),
    CHECK (last_alerted_cents >= 0)
);


CREATE INDEX watches_appid_idx
    ON watches (appid);
