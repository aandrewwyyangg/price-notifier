import logging
from dataclasses import dataclass

DISCORD_MESSAGE_LIMIT = 2000

logger = logging.getLogger("steam_notifier")


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

def store_url(appid: int) -> str:
    return f"https://store.steampowered.com/app/{appid}/"

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
        message=f"{label} dropped to {format_price(new_price)}.\n{store_url(appid)}",
    )

def build_watch_line(
    appid: int,
    name: str | None,
    alert_below: int | None,
    latest_cents: int | None,
) -> str:
    label = user_game_label(appid, name)
    line = f"{label} — latest price {format_price(latest_cents)}"
    if alert_below is not None:
        line += f", alerting below {format_price(alert_below)}"
    return line

def chunk_lines(
    lines: list[str],
    limit: int = DISCORD_MESSAGE_LIMIT,
) -> list[str]:
    chunks = []
    current = ""
    for line in lines:
        pieces = [line[start:start + limit] for start in range(0, len(line), limit)]
        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks

def emit_user_alert(alert: UserAlert) -> None:
    print(f"{alert.discord_user_id}: {alert.message}")

def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

def log_operation(message: str) -> None:
    logger.info(message)

def log_exception(message: str, error: BaseException | None = None) -> None:
    logger.error(message, exc_info=error if error is not None else True)
