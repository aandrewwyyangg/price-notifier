import os
from dataclasses import dataclass
from datetime import datetime, timezone

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
    ENSURE_GAME_EXISTS,
    GET_WATCHED_APPIDS,
    INSERT_PRICE_IF_CHANGED,
    INSERT_WATCH,
    RATCHET_WATCHERS_TO_NOTIFY,
    RECORD_SUCCESSFUL_FETCH,
    SEED_WATCH_RATCHETS,
)


load_dotenv()


@dataclass(frozen=True, slots=True)
class _PollTransactionResult:
    stored_name: str | None
    price_recorded: bool
    seeded_watch_count: int
    discord_user_ids: tuple[int, ...]


def _connect():
    password = os.getenv("POSTGRES_PASSWORD")

    if password is None:
        raise RuntimeError("POSTGRES_PASSWORD is not set")

    return psycopg.connect(
        host="127.0.0.1",
        port=5432,
        dbname=os.getenv("POSTGRES_DB", "steam_notifier"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=password,
    )


def get_watched_appids() -> list[int]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(GET_WATCHED_APPIDS)
            return [row[0] for row in cur.fetchall()]


def create_watch(
    discord_user_id: int,
    appid: int,
    alert_below: int | None = None,
) -> bool:
    parameters = {
        "discord_user_id": discord_user_id,
        "appid": appid,
        "alert_below": alert_below,
    }

    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    ENSURE_GAME_EXISTS,
                    {"appid": appid},
                )
                cur.execute(INSERT_WATCH, parameters)
                cur.fetchone()
    except psycopg.errors.UniqueViolation:
        # The connection context has already rolled back both statements.
        return False

    return True


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

    # Seeding must happen first so a newly seeded watch cannot alert during
    # the same observation.
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

    # A response without a usable integer price is a read failure. It must not
    # advance last_checked_at or change price and ratchet state.
    if (
        isinstance(new_price, bool)
        or not isinstance(new_price, int)
        or new_price < 0
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

    # The connection context committed before execution reached this point.
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