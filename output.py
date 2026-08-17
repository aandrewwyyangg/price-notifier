import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UserAlert:
    discord_user_id: int
    appid: int
    message: str


def user_game_label(
    appid: int | str,
    name: str | None,
) -> str:
    if isinstance(name, str) and name.strip():
        return name

    return f"appid {appid}"


def log_game_label(
    appid: int | str,
    name: str | None,
) -> str:
    if isinstance(name, str) and name.strip():
        return f"{name} ({appid})"

    return f"appid {appid}"


def format_price(cents: int | None) -> str:
    if cents is None:
        return "unavailable"

    dollars, remaining_cents = divmod(cents, 100)
    return f"${dollars}.{remaining_cents:02d}"


def build_price_drop_alert(
    discord_user_id: int,
    appid: int,
    name: str | None,
    new_price: int,
) -> UserAlert:
    label = user_game_label(appid, name)

    return UserAlert(
        discord_user_id=discord_user_id,
        appid=appid,
        message=f"{label} dropped to {format_price(new_price)}.",
    )


def build_watch_line(
    appid: int,
    name: str | None,
    alert_below: int | None,
    latest_cents: int | None,
) -> str:
    # format_price(None) renders "unavailable", which is exactly right for a
    # watched game with no price history yet. The label falls back to the
    # appid while name is NULL, same as every other output path.
    label = user_game_label(appid, name)
    line = f"{label} — latest price {format_price(latest_cents)}"

    if alert_below is not None:
        line += f", alerting below {format_price(alert_below)}"

    return line


def emit_user_alert(alert: UserAlert) -> None:
    # Temporary delivery sink until this becomes a Discord DM.
    print(f"{alert.discord_user_id}: {alert.message}")


def log_operation(message: str) -> None:
    print(message, file=sys.stderr)