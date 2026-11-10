import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import psycopg
from dotenv import load_dotenv

from output import (
    UserAlert,
    build_price_drop_alert,
    format_price,
    log_game_label,
    log_operation,
)
from queries import (
    DELETE_WATCH,
    ENSURE_GAME_EXISTS,
    FIND_MISSING_TABLES,
    GET_USER_WATCHES,
    GET_WATCH_COUNT_AND_DUPLICATE,
    GET_WATCHED_APPIDS,
    INSERT_PRICE_IF_CHANGED,
    INSERT_WATCH,
    LOCK_USER_WATCHES,
    RATCHET_WATCHERS_TO_NOTIFY,
    RECORD_SUCCESSFUL_FETCH,
    SEED_WATCH_RATCHETS,
    SET_STATEMENT_TIMEOUT,
)

load_dotenv()

PG_INTEGER_MAX = 2147483647
MAX_WATCHES_PER_USER = int(os.getenv("MAX_WATCHES_PER_USER", "25"))
REQUIRED_TABLES = ("games", "prices", "watches")
STATEMENT_TIMEOUT = "10s"

class WatchResult(Enum):
    CREATED = "created"
    ALREADY_WATCHING = "already watching"
    LIMIT_REACHED = "limit reached"

@dataclass(frozen=True, slots=True)
class WatchSummary:
    appid: int
    name: str | None
    alert_below: int | None
    latest_cents: int | None

@dataclass(frozen=True, slots=True)
class _PollTransactionResult:
    stored_name: str | None
    price_recorded: bool
    seeded_watch_count: int
    discord_user_ids: tuple[int, ...]

def connection_args() -> dict:
    password = os.getenv("POSTGRES_PASSWORD")
    if password is None:
        raise RuntimeError("POSTGRES_PASSWORD is not set")
    args = {
        "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "dbname": os.getenv("POSTGRES_DB", "steam_notifier"),
        "user": os.getenv("POSTGRES_USER", "postgres"),
        "password": password,
        "connect_timeout": int(os.getenv("POSTGRES_CONNECT_TIMEOUT", "10")),
    }
    sslmode = os.getenv("POSTGRES_SSLMODE")
    if sslmode:
        args["sslmode"] = sslmode
    return args

def _connect():
    conn = psycopg.connect(**connection_args())
    try:
        # Local to the transaction each caller runs, so it never leaks
        # through a connection pooler to another session.
        conn.execute(SET_STATEMENT_TIMEOUT, {"timeout": STATEMENT_TIMEOUT})
    except BaseException:
        conn.close()
        raise
    return conn

def missing_tables() -> list[str]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(FIND_MISSING_TABLES, {"tables": list(REQUIRED_TABLES)})
            return [row[0] for row in cur.fetchall()]

def get_watched_appids() -> list[int]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(GET_WATCHED_APPIDS)
            return [row[0] for row in cur.fetchall()]

def create_watch(
    discord_user_id: int,
    appid: int,
    alert_below: int | None = None,
    max_watches: int = MAX_WATCHES_PER_USER,
) -> WatchResult:
    parameters = {
        "discord_user_id": discord_user_id,
        "appid": appid,
        "alert_below": alert_below,
    }
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                # Held until commit: one user's /watch commands take turns,
                # so two at once can't both pass the limit check.
                cur.execute(LOCK_USER_WATCHES, parameters)
                cur.execute(GET_WATCH_COUNT_AND_DUPLICATE, parameters)
                watch_count, already_watching = cur.fetchone()
                if already_watching:
                    return WatchResult.ALREADY_WATCHING
                if watch_count >= max_watches:
                    return WatchResult.LIMIT_REACHED
                cur.execute(
                    ENSURE_GAME_EXISTS,
                    {"appid": appid},
                )
                cur.execute(INSERT_WATCH, parameters)
                cur.fetchone()
    except psycopg.errors.UniqueViolation:
        return WatchResult.ALREADY_WATCHING
    return WatchResult.CREATED

def delete_watch(discord_user_id: int, appid: int) -> bool:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                DELETE_WATCH,
                {
                    "discord_user_id": discord_user_id,
                    "appid": appid,
                },
            )
            return cur.fetchone() is not None

def get_user_watches(discord_user_id: int) -> list[WatchSummary]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                GET_USER_WATCHES,
                {"discord_user_id": discord_user_id},
            )
            return [WatchSummary(*row) for row in cur.fetchall()]

def _run_poll_transaction(
    cur,
    parameters: dict,
) -> _PollTransactionResult:
    cur.execute(RECORD_SUCCESSFUL_FETCH, parameters)
    recorded_game = cur.fetchone()

    if recorded_game is None:
        raise RuntimeError(
            f"Cannot record a poll result for unknown appid "
            f"{parameters['appid']}"
        )

    cur.execute(INSERT_PRICE_IF_CHANGED, parameters)
    price_row = cur.fetchone()
    cur.execute(SEED_WATCH_RATCHETS, parameters)
    seeded_watch_count = cur.rowcount

    cur.execute(RATCHET_WATCHERS_TO_NOTIFY, parameters)
    discord_user_ids = tuple(
        sorted(row[0] for row in cur.fetchall())
    )

    return _PollTransactionResult(
        stored_name=recorded_game[1],
        price_recorded=price_row is not None,
        seeded_watch_count=seeded_watch_count,
        discord_user_ids=discord_user_ids,
    )

def process_price_observation(
    appid: int,
    name: str | None,
    new_price: int | None,
    observed_at: datetime | None = None,
) -> list[UserAlert]:
    normalized_name = (
        name
        if isinstance(name, str) and name.strip()
        else None
    )
    label = log_game_label(appid, normalized_name)
    if (
        isinstance(new_price, bool)
        or not isinstance(new_price, int)
        or new_price < 0
        or new_price > PG_INTEGER_MAX
    ):
        log_operation(
            f"No usable price for {label}; database unchanged"
        )
        return []

    if observed_at is None:
        observed_at = datetime.now(timezone.utc)

    parameters = {
        "appid": appid,
        "name": normalized_name,
        "new_price": new_price,
        "observed_at": observed_at,
    }

    with _connect() as conn:
        with conn.cursor() as cur:
            result = _run_poll_transaction(cur, parameters)
    label = log_game_label(appid, result.stored_name)

    if result.price_recorded:
        log_operation(
            f"Stored price observation for {label}: "
            f"{format_price(new_price)}"
        )
    else:
        log_operation(
            f"Price unchanged for {label}: "
            f"{format_price(new_price)}"
        )

    if result.seeded_watch_count:
        log_operation(
            f"Seeded {result.seeded_watch_count} watch ratchet(s) "
            f"for {label}"
        )

    if result.discord_user_ids:
        log_operation(
            f"Committed {len(result.discord_user_ids)} alert ratchet(s) "
            f"for {label}"
        )

    return [
        build_price_drop_alert(
            discord_user_id=discord_user_id,
            appid=appid,
            name=result.stored_name,
            new_price=new_price,
        )
        for discord_user_id in result.discord_user_ids
    ]
