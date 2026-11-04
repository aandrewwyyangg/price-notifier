import pytest

from output import (
    DISCORD_MESSAGE_LIMIT,
    build_price_drop_alert,
    build_watch_line,
    chunk_lines,
    format_price,
    log_game_label,
    store_url,
    user_game_label,
)


@pytest.mark.parametrize(
    ("cents", "expected"),
    [
        (None, "unavailable"),
        (0, "$0.00"),
        (5, "$0.05"),
        (999, "$9.99"),
        (1500, "$15.00"),
        (123456, "$1234.56"),
    ],
)
def test_format_price(cents, expected):
    assert format_price(cents) == expected


def test_labels_use_the_name_when_there_is_one():
    assert user_game_label(620, "Portal 2") == "Portal 2"
    assert log_game_label(620, "Portal 2") == "Portal 2 (620)"


@pytest.mark.parametrize("name", [None, "", "   "])
def test_labels_fall_back_to_the_appid(name):
    assert user_game_label(292030, name) == "appid 292030"
    assert log_game_label(292030, name) == "appid 292030"


def test_price_drop_alert_names_the_game_and_links_the_store_page():
    alert = build_price_drop_alert(
        discord_user_id=1,
        appid=620,
        name="Portal 2",
        new_price=499,
    )

    assert alert.discord_user_id == 1
    assert alert.appid == 620
    assert alert.message == (
        "Portal 2 dropped to $4.99.\nhttps://store.steampowered.com/app/620/"
    )


def test_price_drop_alert_falls_back_to_the_appid_for_a_null_name():
    alert = build_price_drop_alert(
        discord_user_id=1,
        appid=292030,
        name=None,
        new_price=3499,
    )

    assert alert.message == f"appid 292030 dropped to $34.99.\n{store_url(292030)}"


def test_watch_line_renders_a_null_price_and_a_threshold():
    assert build_watch_line(1145360, "Hades", 1500, None) == (
        "Hades — latest price unavailable, alerting below $15.00"
    )


def test_watch_line_renders_a_null_name_without_a_threshold():
    assert build_watch_line(292030, None, None, 3999) == (
        "appid 292030 — latest price $39.99"
    )


def test_chunk_lines_keeps_a_short_list_in_one_message():
    assert chunk_lines(["a", "b"]) == ["a\nb"]
    assert chunk_lines([]) == []


def test_chunk_lines_splits_a_long_list_without_losing_or_reordering_lines():
    lines = [f"game {i} — latest price $9.99, alerting below $5.00" for i in range(200)]

    chunks = chunk_lines(lines)

    assert len(chunks) > 1
    assert all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in chunks)
    assert "\n".join(chunks).split("\n") == lines


def test_chunk_lines_hard_splits_a_single_overlong_line():
    line = "x" * (DISCORD_MESSAGE_LIMIT * 2 + 5)

    chunks = chunk_lines([line])

    assert [len(chunk) for chunk in chunks] == [
        DISCORD_MESSAGE_LIMIT,
        DISCORD_MESSAGE_LIMIT,
        5,
    ]
    assert "".join(chunks) == line
