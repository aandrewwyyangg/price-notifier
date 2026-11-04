import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

import database

ROOT = Path(__file__).resolve().parents[1]
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# fixtures.sql wraps itself in BEGIN/COMMIT for psql. A COMMIT here would
# persist the reload, so the test transaction runs the statements without it.
FIXTURE_SQL = "\n".join(
    line
    for line in (ROOT / "fixtures.sql").read_text(encoding="utf-8").splitlines()
    if line.strip().upper() not in {"BEGIN;", "COMMIT;"}
)


def _database_unavailable(reason: str) -> None:
    if os.getenv("REQUIRE_DB") == "1":
        pytest.fail(f"REQUIRE_DB=1 but the database is unavailable: {reason}")
    pytest.skip(f"local PostgreSQL unavailable: {reason}")


def _connect_locally() -> psycopg.Connection:
    try:
        params = database.connection_args()
    except RuntimeError as error:
        _database_unavailable(str(error))
    if params["host"] not in LOCAL_HOSTS:
        pytest.fail(
            f"refusing to run database tests against non-local host {params['host']!r}; "
            "point POSTGRES_HOST at the local compose database"
        )
    params["connect_timeout"] = 5
    try:
        return psycopg.connect(**params)
    except psycopg.OperationalError as error:
        _database_unavailable(str(error))


@pytest.fixture
def db():
    """A connection with fresh fixtures loaded, rolled back after the test.

    TRUNCATE is transactional in PostgreSQL, so nothing a test does survives.
    """
    conn = _connect_locally()
    try:
        with conn.cursor() as cur:
            cur.execute(FIXTURE_SQL)
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def other_connection(db):
    """A second session, for tests where two sessions compete for a lock.

    It never touches the tables db truncated, and it is rolled back too.
    """
    conn = _connect_locally()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def cur(db):
    with db.cursor() as cursor:
        yield cursor


@pytest.fixture
def app_db(db, monkeypatch):
    """Route database.py's own connections into the test transaction.

    Each call gets a SAVEPOINT instead of a new connection, so the real
    functions run their real statements and still roll back with the test.
    """

    @contextmanager
    def connect_inside_test_transaction():
        with db.transaction():
            yield db

    monkeypatch.setattr(database, "_connect", connect_inside_test_transaction)
    return db
