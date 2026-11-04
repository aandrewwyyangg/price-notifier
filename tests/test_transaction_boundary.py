from contextlib import contextmanager
from unittest.mock import Mock

import psycopg
import pytest

import database


@pytest.mark.parametrize("commit_fails", [False, True], ids=["commit", "commit failure"])
def test_alerts_are_built_only_after_the_transaction_exits(monkeypatch, commit_fails):
    # The SQL tests use SAVEPOINTs; this separately checks the production
    # connection boundary, including an error raised while committing.
    transaction_exited = False
    connection = Mock()
    connection.cursor.return_value.__enter__ = Mock(return_value=Mock())
    connection.cursor.return_value.__exit__ = Mock(return_value=False)

    @contextmanager
    def connect():
        nonlocal transaction_exited
        yield connection
        if commit_fails:
            raise psycopg.OperationalError("commit failed")
        transaction_exited = True

    result = database._PollTransactionResult(
        stored_name="Portal 2",
        price_recorded=True,
        seeded_watch_count=0,
        discord_user_ids=(123,),
    )
    monkeypatch.setattr(database, "_connect", connect)
    monkeypatch.setattr(database, "_run_poll_transaction", Mock(return_value=result))
    real_builder = database.build_price_drop_alert

    def build_after_commit(**kwargs):
        assert transaction_exited, "alert built before successful transaction exit"
        return real_builder(**kwargs)

    builder = Mock(side_effect=build_after_commit)
    monkeypatch.setattr(database, "build_price_drop_alert", builder)

    if commit_fails:
        with pytest.raises(psycopg.OperationalError, match="commit failed"):
            database.process_price_observation(620, "Portal 2", 499)
        builder.assert_not_called()
    else:
        alerts = database.process_price_observation(620, "Portal 2", 499)
        assert len(alerts) == 1
        builder.assert_called_once()
