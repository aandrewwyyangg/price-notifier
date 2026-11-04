"""connection_args: what the environment turns into psycopg.connect arguments."""

import pytest

import database


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "not-a-real-password")
    monkeypatch.delenv("POSTGRES_SSLMODE", raising=False)
    return monkeypatch


def test_sslmode_is_passed_through_when_set(environment):
    environment.setenv("POSTGRES_SSLMODE", "require")
    assert database.connection_args()["sslmode"] == "require"


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_sslmode_is_left_to_libpq_otherwise(environment, value):
    if value is not None:
        environment.setenv("POSTGRES_SSLMODE", value)
    assert "sslmode" not in database.connection_args()


def test_a_missing_password_is_refused(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        database.connection_args()
