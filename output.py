from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UserAlert:
    discord_user_id: int
    message: str


def game_label(appid: int | str, name: str | None) -> str:
    if isinstance(name, str) and name.strip():
        return name

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
    label = game_label(appid, name)

    return UserAlert(
        discord_user_id=discord_user_id,
        message=f"{label} dropped to {format_price(new_price)}.",
    )


def log_operation(message: str) -> None:
    print(message)