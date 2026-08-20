CREATE TABLE games (
    appid INTEGER PRIMARY KEY,
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
