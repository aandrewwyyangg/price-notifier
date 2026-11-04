import pytest

import bot


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("9.99", 999),
        ("$9.99", 999),
        ("15", 1500),
        ("9.9", 990),
        (".5", 50),
        ("  4.20 ", 420),
        ("0009.99", 999),
        ("0", 0),
        ("21474836.47", bot.PG_INTEGER_MAX),
    ],
)
def test_accepts_dollar_amounts(text, cents):
    assert bot.parse_price_to_cents(text) == cents


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("$", id="bare dollar sign"),
        pytest.param(".", id="bare point"),
        pytest.param("5.", id="trailing point"),
        pytest.param("9.999", id="three decimals"),
        pytest.param("nine dollars", id="words"),
        pytest.param("-5", id="negative"),
        pytest.param("1e3", id="exponent"),
        pytest.param("9,99", id="comma decimal"),
        pytest.param("$$5", id="double dollar"),
        pytest.param("$ 5", id="space after dollar"),
        pytest.param("²", id="superscript digit"),
        pytest.param("١٢", id="arabic-indic digits"),
        pytest.param("５", id="fullwidth digit"),
        pytest.param("9" * 4400, id="past the int conversion limit"),
        pytest.param("21474836.48", id="one cent past INTEGER max"),
        pytest.param("99999999999.99", id="far past INTEGER max"),
    ],
)
def test_rejects_anything_else_without_raising(text):
    assert bot.parse_price_to_cents(text) is None
