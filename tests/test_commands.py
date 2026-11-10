import asyncio
import logging
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import discord
import psycopg
import pytest
from discord import app_commands

import bot
import steampricingproject as poller
from database import WatchResult

ALICE = 100000000000000001
PORTAL_2 = 620
STEAM_PORTAL_2 = {"name": "Portal 2", "price": 999}


@pytest.fixture(autouse=True)
def no_steam_requests():
    """Commands must stay available independently of Steam."""
    with patch.object(
        poller.requests, "get", side_effect=AssertionError("unexpected Steam request")
    ) as request:
        yield request
        request.assert_not_called()


@pytest.fixture(autouse=True)
def isolated_poll_state(monkeypatch):
    monkeypatch.setattr(bot.client, "fatal_poll_error", None)
    monkeypatch.setattr(bot.client, "poll_stop", Event())


def make_interaction(user_id=ALICE):
    interaction = SimpleNamespace()
    interaction.user = SimpleNamespace(id=user_id)
    interaction.command = SimpleNamespace(name="watch")
    interaction.response = SimpleNamespace(
        defer=AsyncMock(),
        is_done=Mock(return_value=True),
        send_message=AsyncMock(),
    )
    interaction.followup = SimpleNamespace(send=AsyncMock())
    return interaction


def replies(interaction):
    calls = interaction.followup.send.call_args_list
    for call in calls:
        # The first followup inherits the deferred response's privacy.
        # Later followups need ephemeral=True or they default to public.
        assert call.kwargs.get("ephemeral") is True, f"public reply: {call}"
    return [call.args[0] for call in calls]


def only_reply(interaction):
    sent = replies(interaction)
    assert len(sent) == 1, f"expected exactly one reply, got {sent}"
    return sent[0]


def deferred_then(interaction, name, return_value):
    def check(*args, **kwargs):
        assert interaction.response.defer.called, (
            f"{name} reached the database before defer()"
        )
        return return_value
    return check


def test_watch_defers_first_parses_dollars_and_confirms_the_threshold():
    interaction = make_interaction()

    with patch.object(bot, "create_watch") as create_watch:
        create_watch.side_effect = deferred_then(
            interaction, "/watch", WatchResult.CREATED
        )
        asyncio.run(bot.watch.callback(interaction, PORTAL_2, "9.99"))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    create_watch.assert_called_once_with(ALICE, PORTAL_2, 999)
    reply = only_reply(interaction)
    assert reply.startswith("Watching appid 620.")
    # The first price is a silent baseline, so a game already under the
    # threshold stays quiet until it drops again. The reply has to say so.
    assert "each new low below $9.99, counting from the first price I see" in reply
    assert "free to play or unreleased" in reply


def test_watch_without_a_threshold_passes_none():
    interaction = make_interaction()

    with patch.object(bot, "create_watch", return_value=WatchResult.CREATED) as create_watch:
        asyncio.run(bot.watch.callback(interaction, PORTAL_2, None))

    create_watch.assert_called_once_with(ALICE, PORTAL_2, None)
    reply = only_reply(interaction)
    assert "below" not in reply
    assert "each new low, counting from the first price I see" in reply


def test_watch_accepts_a_valid_appid_without_looking_it_up():
    interaction = make_interaction()

    with patch.object(bot, "create_watch", return_value=WatchResult.CREATED) as create_watch:
        asyncio.run(bot.watch.callback(interaction, 999_999_999, "9.99"))

    create_watch.assert_called_once_with(ALICE, 999_999_999, 999)
    assert only_reply(interaction).startswith("Watching appid 999999999.")


def test_watch_reports_a_duplicate():
    interaction = make_interaction()

    with patch.object(bot, "create_watch", return_value=WatchResult.ALREADY_WATCHING):
        asyncio.run(bot.watch.callback(interaction, PORTAL_2, None))

    assert only_reply(interaction).startswith("You're already watching that game.")


def test_watch_reports_the_per_user_limit():
    interaction = make_interaction()

    with patch.object(bot, "create_watch", return_value=WatchResult.LIMIT_REACHED):
        asyncio.run(bot.watch.callback(interaction, PORTAL_2, None))

    reply = only_reply(interaction)
    assert str(bot.MAX_WATCHES_PER_USER) in reply
    assert "/unwatch" in reply


@pytest.mark.parametrize(
    ("appid", "alert_below"),
    [
        pytest.param(0, None, id="appid zero"),
        pytest.param(-5, None, id="appid negative"),
        pytest.param(bot.PG_INTEGER_MAX + 1, None, id="appid past INTEGER max"),
        pytest.param(PORTAL_2, "nine dollars", id="price unparseable"),
        pytest.param(PORTAL_2, "   ", id="price empty"),
        pytest.param(PORTAL_2, "99999999999.99", id="price past INTEGER max"),
        pytest.param(PORTAL_2, "²", id="price superscript digit"),
        pytest.param(PORTAL_2, "9" * 6000, id="price 6000 digits"),
        pytest.param(PORTAL_2, "0", id="price zero"),
    ],
)
def test_watch_rejects_bad_input_without_writing(appid, alert_below):
    interaction = make_interaction()

    with patch.object(bot, "create_watch") as create_watch:
        asyncio.run(bot.watch.callback(interaction, appid, alert_below))

    create_watch.assert_not_called()
    only_reply(interaction)


@pytest.mark.parametrize(
    ("database_function", "invoke"),
    [
        ("create_watch", lambda i: bot.watch.callback(i, PORTAL_2, None)),
        ("delete_watch", lambda i: bot.unwatch.callback(i, PORTAL_2)),
        ("get_user_watches", lambda i: bot.list_watches.callback(i)),
    ],
    ids=["/watch", "/unwatch", "/list"],
)
def test_an_unreachable_database_is_answered_not_raised(database_function, invoke):
    interaction = make_interaction()

    with patch.object(
        bot,
        database_function,
        side_effect=psycopg.OperationalError("database unavailable"),
    ):
        asyncio.run(invoke(interaction))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert "try again shortly" in only_reply(interaction)


@pytest.mark.parametrize(
    ("removed", "expected"),
    [(True, "No longer watching"), (False, "weren't watching")],
)
def test_unwatch_distinguishes_removed_from_not_watching(removed, expected):
    interaction = make_interaction()

    with patch.object(bot, "delete_watch") as delete_watch:
        delete_watch.side_effect = deferred_then(interaction, "/unwatch", removed)
        asyncio.run(bot.unwatch.callback(interaction, PORTAL_2))

    delete_watch.assert_called_once_with(ALICE, PORTAL_2)
    assert expected in only_reply(interaction)


def test_unwatch_answers_an_out_of_range_appid_without_a_query():
    interaction = make_interaction()

    with patch.object(bot, "delete_watch") as delete_watch:
        asyncio.run(bot.unwatch.callback(interaction, bot.PG_INTEGER_MAX + 1))

    delete_watch.assert_not_called()
    assert "weren't watching" in only_reply(interaction)


def test_list_answers_an_empty_watch_list():
    interaction = make_interaction()

    with patch.object(bot, "get_user_watches", return_value=[]):
        asyncio.run(bot.list_watches.callback(interaction))

    assert "aren't watching anything" in only_reply(interaction)


def test_list_renders_a_null_name_and_a_null_price():
    interaction = make_interaction()
    listed = [
        SimpleNamespace(appid=PORTAL_2, name="Portal 2", alert_below=500, latest_cents=999),
        SimpleNamespace(appid=292030, name=None, alert_below=None, latest_cents=None),
    ]

    with patch.object(bot, "get_user_watches") as get_user_watches:
        get_user_watches.side_effect = deferred_then(interaction, "/list", listed)
        asyncio.run(bot.list_watches.callback(interaction))

    rendered = only_reply(interaction)
    assert "Portal 2 (appid 620) — latest price $9.99, alerting below $5.00" in rendered
    assert "appid 292030 — latest price unavailable" in rendered


def test_list_splits_a_long_list_into_private_messages_under_the_limit():
    interaction = make_interaction()
    listed = [
        SimpleNamespace(
            appid=100000 + index,
            name="An Extremely Long Game Name " * 4,
            alert_below=500,
            latest_cents=999,
        )
        for index in range(40)
    ]

    with patch.object(bot, "get_user_watches", return_value=listed):
        asyncio.run(bot.list_watches.callback(interaction))

    sent = replies(interaction)
    assert len(sent) > 1
    assert all(len(message) <= 2000 for message in sent)
    assert len("\n".join(sent).split("\n")) == len(listed)


ALERT = bot.UserAlert(
    discord_user_id=ALICE,
    appid=PORTAL_2,
    message="Portal 2 dropped to $4.99.",
)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            discord.Forbidden(Mock(status=403, reason="Forbidden"), "cannot send messages to this user"),
            id="DMs closed",
        ),
        pytest.param(
            discord.NotFound(Mock(status=404, reason="Not Found"), "unknown user"),
            id="unknown user",
        ),
        pytest.param(
            discord.HTTPException(Mock(status=503, reason="Service Unavailable"), "unavailable"),
            id="transient HTTP",
        ),
    ],
)
def test_deliver_alert_logs_http_failures_without_raising(error):
    with patch.object(bot.client, "fetch_user", side_effect=error):
        with patch.object(bot, "log_operation") as log_operation:
            asyncio.run(bot.deliver_alert(ALERT))

    log_operation.assert_called_once()
    logged = log_operation.call_args.args[0]
    assert str(ALICE) in logged and str(PORTAL_2) in logged


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError("network unreachable"), id="OSError"),
        pytest.param(asyncio.TimeoutError(), id="timeout"),
        pytest.param(aiohttp.ClientConnectionError("connection reset"), id="aiohttp"),
    ],
)
def test_deliver_alert_survives_network_errors(error):
    with patch.object(bot.client, "fetch_user", side_effect=error):
        with patch.object(bot, "log_exception") as log_exception:
            asyncio.run(bot.deliver_alert(ALERT))

    log_exception.assert_called_once()
    assert str(ALICE) in log_exception.call_args.args[0]


def test_deliver_alert_sends_the_message_and_logs_nothing():
    recipient = SimpleNamespace(send=AsyncMock())

    with patch.object(bot, "log_operation") as log_operation:
        with patch.object(
            bot.client, "fetch_user", new=AsyncMock(return_value=recipient)
        ) as fetch_user:
            asyncio.run(bot.deliver_alert(ALERT))

    fetch_user.assert_awaited_once_with(ALICE)
    recipient.send.assert_awaited_once_with(ALERT.message)
    log_operation.assert_not_called()


def test_poll_delivers_every_alert_in_order():
    alerts = [
        bot.UserAlert(discord_user_id=1, appid=PORTAL_2, message="first"),
        bot.UserAlert(discord_user_id=2, appid=PORTAL_2, message="second"),
    ]

    with patch.object(bot, "run_poll_cycle", return_value=alerts):
        with patch.object(bot, "deliver_alert", new=AsyncMock()) as deliver_alert:
            asyncio.run(bot.poll_prices.coro())

    assert [call.args[0] for call in deliver_alert.await_args_list] == alerts


@pytest.mark.parametrize(
    "error",
    [RuntimeError("missing configuration"), psycopg.ProgrammingError("schema drift")],
)
def test_an_unexpected_cycle_error_closes_the_client(error):
    with patch.object(bot, "run_poll_cycle", side_effect=error):
        with patch.object(bot, "deliver_alert", new=AsyncMock()) as deliver_alert:
            with patch.object(bot, "log_exception") as log_exception:
                with patch.object(bot.client, "close", new=AsyncMock()) as close:
                    asyncio.run(bot.poll_prices.coro())

    deliver_alert.assert_not_awaited()
    assert bot.client.fatal_poll_error is error
    log_exception.assert_called_once_with("Poll cycle failed; shutting down", error)
    close.assert_awaited_once_with()


def test_a_fatal_cycle_attempts_collected_alerts_before_closing():
    alerts = [
        bot.UserAlert(discord_user_id=1, appid=PORTAL_2, message="first"),
        bot.UserAlert(discord_user_id=2, appid=PORTAL_2, message="second"),
    ]
    error = poller.PollCycleError(570, alerts)
    attempted = []

    async def deliver(alert):
        assert bot.client.fatal_poll_error is error
        attempted.append(alert)

    async def close_client():
        assert attempted == alerts

    with patch.object(bot, "run_poll_cycle", side_effect=error):
        with patch.object(bot, "deliver_alert", side_effect=deliver) as deliver_alert:
            with patch.object(bot, "log_exception"):
                with patch.object(bot.client, "close", side_effect=close_client) as close:
                    asyncio.run(bot.poll_prices.coro())

    assert deliver_alert.await_count == 2
    close.assert_awaited_once_with()


def test_closed_dms_during_fatal_draining_do_not_block_other_alerts():
    alerts = [
        bot.UserAlert(discord_user_id=1, appid=PORTAL_2, message="first"),
        bot.UserAlert(discord_user_id=2, appid=PORTAL_2, message="second"),
    ]
    error = poller.PollCycleError(570, alerts)
    forbidden = discord.Forbidden(Mock(status=403, reason="Forbidden"), "DMs closed")
    users = [
        SimpleNamespace(send=AsyncMock(side_effect=forbidden)),
        SimpleNamespace(send=AsyncMock()),
    ]

    with patch.object(bot, "run_poll_cycle", side_effect=error):
        with patch.object(bot.client, "fetch_user", side_effect=users) as fetch:
            with patch.object(bot.client, "close", new=AsyncMock()) as close:
                with patch.object(bot, "log_exception"):
                    asyncio.run(bot.poll_prices.coro())

    assert [call.args[0] for call in fetch.await_args_list] == [1, 2]
    users[0].send.assert_awaited_once_with("first")
    users[1].send.assert_awaited_once_with("second")
    assert bot.client.fatal_poll_error is error
    close.assert_awaited_once_with()


def test_a_fatal_poll_stops_its_own_loop_without_cancelling_cleanup():
    error = RuntimeError("broken query")

    async def run_loop():
        with patch.object(bot.client, "wait_until_ready", new=AsyncMock()):
            with patch.object(bot, "run_poll_cycle", side_effect=error) as run_cycle:
                with patch.object(discord.Client, "close", new=AsyncMock()) as close:
                    task = bot.poll_prices.start()
                    await asyncio.wait_for(task, timeout=2)
        assert not task.cancelled()
        assert bot.client.poll_stop.is_set()
        assert bot.client.fatal_poll_error is error
        assert not bot.poll_prices.is_running()
        run_cycle.assert_called_once()
        close.assert_awaited_once_with()

    with patch.object(bot, "log_exception"):
        asyncio.run(run_loop())


def test_main_propagates_the_recorded_poll_failure():
    error = poller.PollCycleError(570, [])

    def run_client(*args, **kwargs):
        bot.client.fatal_poll_error = error

    with patch.object(bot, "require_bot_token", return_value="test-only"):
        with patch.object(bot, "preflight"):
            with patch.object(bot.signal, "signal"):
                with patch.object(bot.client, "run", side_effect=run_client):
                    with pytest.raises(poller.PollCycleError) as failure:
                        bot.main()

    assert failure.value is error


def test_cancelling_a_poll_stops_its_worker_before_writing_or_fetching_again():
    started, release, finished = Event(), Event(), Event()
    stop = Event()
    original_cycle = poller.run_poll_cycle

    def get_game(appid):
        started.set()
        assert release.wait(2)
        return STEAM_PORTAL_2

    def run_cycle(stop_event):
        try:
            return original_cycle(stop_event)
        finally:
            finished.set()

    async def cancel_in_flight():
        task = asyncio.create_task(bot.poll_prices.coro())
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 2)

    with patch.object(bot.client, "poll_stop", stop):
        with patch.object(bot, "run_poll_cycle", side_effect=run_cycle):
            with patch.object(poller, "get_watched_appids", return_value=[620, 570]):
                with patch.object(poller, "get_game", side_effect=get_game) as fetch:
                    with patch.object(poller, "process_price_observation") as process:
                        asyncio.run(cancel_in_flight())

    assert stop.is_set()
    fetch.assert_called_once_with(620)
    process.assert_not_called()


def test_closing_the_client_stops_polling_before_closing_discord():
    client = bot.SteamNotifierClient()

    async def close_discord():
        assert client.poll_stop.is_set()
        cancel.assert_called_once_with()

    with patch.object(bot.poll_prices, "cancel") as cancel:
        with patch.object(discord.Client, "close", new=AsyncMock(side_effect=close_discord)):
            asyncio.run(client.close())


def test_the_command_error_handler_is_registered():
    assert bot.client.tree.on_error is bot.on_command_error


def test_bot_messages_never_ping_anyone():
    mentions = bot.client.allowed_mentions
    assert (mentions.everyone, mentions.users, mentions.roles, mentions.replied_user) == (
        False,
        False,
        False,
        False,
    )


def test_command_errors_are_logged_and_answered():
    interaction = make_interaction()
    error = app_commands.CommandInvokeError(bot.watch, RuntimeError("boom"))

    with patch.object(bot, "log_exception") as log_exception:
        asyncio.run(bot.on_command_error(interaction, error))

    log_exception.assert_called_once()
    assert only_reply(interaction) == bot.UNEXPECTED_ERROR


def test_command_errors_before_defer_use_the_initial_response():
    interaction = make_interaction()
    interaction.response.is_done = Mock(return_value=False)
    error = app_commands.CommandInvokeError(bot.watch, RuntimeError("boom"))

    with patch.object(bot, "log_exception"):
        asyncio.run(bot.on_command_error(interaction, error))

    interaction.response.send_message.assert_awaited_once_with(
        bot.UNEXPECTED_ERROR, ephemeral=True
    )
    interaction.followup.send.assert_not_awaited()


@pytest.mark.parametrize("guild_id", [None, "", "100000000000000003"])
def test_setup_syncs_commands_then_starts_the_poller(monkeypatch, guild_id):
    if guild_id is None:
        monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    else:
        monkeypatch.setenv("DISCORD_GUILD_ID", guild_id)

    with patch.object(bot.client.tree, "copy_global_to") as copy_global_to:
        with patch.object(bot.client.tree, "sync", new=AsyncMock()) as sync:
            with patch.object(bot.poll_prices, "start") as start:
                start.side_effect = sync.assert_awaited_once
                asyncio.run(bot.client.setup_hook())

    if guild_id:
        guild = discord.Object(id=int(guild_id))
        copy_global_to.assert_called_once_with(guild=guild)
        sync.assert_awaited_once_with(guild=guild)
    else:
        copy_global_to.assert_not_called()
        sync.assert_awaited_once_with()
    start.assert_called_once_with()


def test_require_bot_token(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        bot.require_bot_token()

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
    with pytest.raises(RuntimeError):
        bot.require_bot_token()

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "not-a-real-token")
    assert bot.require_bot_token() == "not-a-real-token"


def app_log(caplog):
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "steam_notifier"
    ]


def test_preflight_exits_with_an_error_when_the_database_is_unreachable(caplog):
    with patch.object(
        bot,
        "missing_tables",
        side_effect=psycopg.OperationalError("connection refused"),
    ):
        with caplog.at_level(logging.INFO, logger="steam_notifier"):
            with pytest.raises(SystemExit) as exit_info:
                bot.preflight()

    assert exit_info.value.code == 1
    ((level, message),) = app_log(caplog)
    assert level == logging.ERROR
    assert "connection refused" in message


def test_preflight_exits_with_an_error_when_the_schema_is_missing(caplog):
    with patch.object(bot, "missing_tables", return_value=["watches"]):
        with caplog.at_level(logging.INFO, logger="steam_notifier"):
            with pytest.raises(SystemExit) as exit_info:
                bot.preflight()

    assert exit_info.value.code == 1
    ((level, message),) = app_log(caplog)
    assert level == logging.ERROR
    assert "watches" in message


def test_preflight_passes_with_the_schema_present(caplog):
    with patch.object(bot, "missing_tables", return_value=[]):
        with caplog.at_level(logging.INFO, logger="steam_notifier"):
            bot.preflight()

    ((level, message),) = app_log(caplog)
    assert level == logging.INFO
    assert "passed" in message
