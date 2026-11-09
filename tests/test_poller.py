from unittest.mock import Mock, patch
from threading import Event

import psycopg
import pytest
import requests

import database
import steampricingproject as poller
from output import UserAlert

FIRST_APPID = 620
SECOND_APPID = 1145360
THIRD_APPID = 292030
FIRST_GAME = {"name": "Portal 2", "price": 999}
SECOND_GAME = {"name": "Hades", "price": 2499}
FIRST_ALERT = UserAlert(
    discord_user_id=100000000000000001,
    appid=FIRST_APPID,
    message="Portal 2 dropped to $9.99.",
)
SECOND_ALERT = UserAlert(
    discord_user_id=100000000000000002,
    appid=SECOND_APPID,
    message="Hades dropped to $24.99.",
)


@pytest.fixture(autouse=True)
def no_rate_limit_sleep():
    with patch.object(poller.time, "sleep"):
        yield


def processed_appids(process_price_observation):
    return [call.kwargs["appid"] for call in process_price_observation.call_args_list]


def test_a_poll_list_database_failure_skips_the_cycle():
    with patch.object(
        poller,
        "get_watched_appids",
        side_effect=psycopg.OperationalError("database unavailable"),
    ):
        with patch.object(poller, "check_all") as check_all:
            with patch.object(poller, "log_operation") as log_operation:
                alerts = poller.run_poll_cycle()

    check_all.assert_not_called()
    log_operation.assert_called_once()
    assert "Could not load watched appids" in log_operation.call_args.args[0]
    # The Discord task iterates the result, so a skipped cycle is [], not None.
    assert alerts == []


def test_a_non_operational_poll_list_error_fails_fast():
    with patch.object(
        poller,
        "get_watched_appids",
        side_effect=RuntimeError("configuration error"),
    ):
        with pytest.raises(RuntimeError):
            poller.run_poll_cycle()


def test_a_per_game_database_failure_is_logged_and_the_cycle_continues():
    with patch.object(poller, "get_game", side_effect=[FIRST_GAME, SECOND_GAME]):
        with patch.object(
            poller,
            "process_price_observation",
            side_effect=[psycopg.OperationalError("connection lost"), [SECOND_ALERT]],
        ) as process_price_observation:
            with patch.object(poller, "emit_user_alert") as emit_user_alert:
                with patch.object(poller, "log_operation") as log_operation:
                    alerts = poller.check_all([FIRST_APPID, SECOND_APPID])

    emit_user_alert.assert_not_called()
    assert alerts == [SECOND_ALERT]
    log_operation.assert_called_once()
    assert "Database operation failed for Portal 2 (620)" in log_operation.call_args.args[0]
    # Two appids, the first failing: with one appid, continue and return
    # would look identical and this test could not fail.
    assert processed_appids(process_price_observation) == [FIRST_APPID, SECOND_APPID]


def test_a_steam_request_failure_is_logged_and_the_cycle_continues():
    with patch.object(
        poller,
        "get_game",
        side_effect=[requests.ConnectionError("steam unreachable"), SECOND_GAME],
    ):
        with patch.object(
            poller, "process_price_observation", return_value=[SECOND_ALERT]
        ) as process_price_observation:
            with patch.object(poller, "log_operation") as log_operation:
                alerts = poller.check_all([FIRST_APPID, SECOND_APPID])

    log_operation.assert_called_once()
    assert "Request failed for appid 620" in log_operation.call_args.args[0]
    assert processed_appids(process_price_observation) == [SECOND_APPID]
    assert alerts == [SECOND_ALERT]


def test_an_unusable_steam_response_writes_nothing_and_the_cycle_continues():
    with patch.object(poller, "get_game", side_effect=[None, SECOND_GAME]):
        with patch.object(
            poller, "process_price_observation", return_value=[]
        ) as process_price_observation:
            with patch.object(poller, "log_operation") as log_operation:
                alerts = poller.check_all([FIRST_APPID, SECOND_APPID])

    log_operation.assert_called_once()
    assert "Could not find data for appid 620" in log_operation.call_args.args[0]
    assert processed_appids(process_price_observation) == [SECOND_APPID]
    assert alerts == []


def test_an_unexpected_error_for_one_game_keeps_the_other_games_alerts():
    # FIRST_ALERT's ratchet is already committed when the second game fails,
    # so dropping it would lose the DM for good.
    with patch.object(poller, "get_game", side_effect=[FIRST_GAME, SECOND_GAME, SECOND_GAME]):
        with patch.object(
            poller,
            "process_price_observation",
            side_effect=[[FIRST_ALERT], psycopg.ProgrammingError("bug"), [SECOND_ALERT]],
        ) as process_price_observation:
            with patch.object(poller, "log_exception") as log_exception:
                alerts = poller.check_all([FIRST_APPID, THIRD_APPID, SECOND_APPID])

    assert alerts == [FIRST_ALERT, SECOND_ALERT]
    assert len(processed_appids(process_price_observation)) == 3
    log_exception.assert_called_once()
    assert str(THIRD_APPID) in log_exception.call_args.args[0]


def test_a_steam_rate_limit_ends_the_cycle_early():
    with patch.object(
        poller, "get_game", side_effect=poller.SteamRateLimited("HTTP 429")
    ) as get_game:
        with patch.object(poller, "process_price_observation") as process_price_observation:
            with patch.object(poller, "log_operation") as log_operation:
                alerts = poller.check_all([FIRST_APPID, SECOND_APPID, THIRD_APPID])

    assert alerts == []
    get_game.assert_called_once()
    process_price_observation.assert_not_called()
    assert "rate limit" in log_operation.call_args.args[0]


def test_a_steam_rate_limit_keeps_alerts_collected_before_it():
    with patch.object(
        poller,
        "get_game",
        side_effect=[FIRST_GAME, poller.SteamRateLimited("HTTP 429"), SECOND_GAME],
    ) as get_game:
        with patch.object(
            poller, "process_price_observation", return_value=[FIRST_ALERT]
        ) as process_price_observation:
            alerts = poller.check_all([FIRST_APPID, SECOND_APPID, THIRD_APPID])

    assert alerts == [FIRST_ALERT]
    assert [call.args[0] for call in get_game.call_args_list] == [FIRST_APPID, SECOND_APPID]
    assert processed_appids(process_price_observation) == [FIRST_APPID]


def test_stopping_a_cycle_keeps_collected_alerts_and_skips_remaining_games():
    stopped = Event()

    def first_game(appid, stop_event):
        assert appid == FIRST_APPID
        stopped.set()
        return [FIRST_ALERT]

    with patch.object(poller, "check_one", side_effect=first_game) as check_one:
        alerts = poller.check_all([FIRST_APPID, SECOND_APPID], stopped)

    assert alerts == [FIRST_ALERT]
    check_one.assert_called_once_with(FIRST_APPID, stopped)


def test_a_stopped_poller_does_not_query_the_database():
    stopped = Event()
    stopped.set()
    with patch.object(poller, "get_watched_appids") as get_watched_appids:
        assert poller.run_poll_cycle(stopped) == []
    get_watched_appids.assert_not_called()


def steam_response(payload, status=200):
    response = Mock(status_code=status)
    response.json.return_value = payload
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(
            f"{status} error", response=response
        )
    else:
        response.raise_for_status.return_value = None
    return response


def priced_payload(price_overview, name="Portal 2"):
    return {
        str(FIRST_APPID): {
            "success": True,
            "data": {"name": name, "price_overview": price_overview},
        }
    }


def test_the_currency_guard_discards_a_mismatched_currency():
    response = steam_response(priced_payload({"currency": "EUR", "final": 899}))

    with patch.object(poller.requests, "get", return_value=response):
        with patch.object(poller, "log_operation") as log_operation:
            game = poller.get_game(FIRST_APPID)

    assert game == {"name": "Portal 2", "price": None}
    log_operation.assert_called_once()
    assert "Currency mismatch" in log_operation.call_args.args[0]


def test_the_currency_guard_keeps_the_expected_currency():
    response = steam_response(priced_payload({"currency": "USD", "final": 899}))

    with patch.object(poller.requests, "get", return_value=response):
        with patch.object(poller, "log_operation") as log_operation:
            game = poller.get_game(FIRST_APPID)

    assert game == {"name": "Portal 2", "price": 899}
    log_operation.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="null payload"),
        pytest.param([], id="list payload"),
        pytest.param({}, id="appid missing"),
        pytest.param({"620": None}, id="null entry"),
        pytest.param({"620": []}, id="list entry"),
        pytest.param({"620": "error"}, id="string entry"),
        pytest.param({"620": {"success": False}}, id="success false"),
        pytest.param({"620": {"success": True, "data": []}}, id="free game data list"),
        pytest.param({"620": {"success": True, "data": None}}, id="null data"),
    ],
)
def test_unusable_payloads_return_none_instead_of_raising(payload):
    with patch.object(poller.requests, "get", return_value=steam_response(payload)):
        assert poller.get_game(FIRST_APPID) is None


@pytest.mark.parametrize(
    "price_overview",
    [
        pytest.param(None, id="null"),
        pytest.param([], id="list"),
        pytest.param("free", id="string"),
        pytest.param({}, id="empty"),
        pytest.param({"currency": "USD"}, id="no final price"),
    ],
)
def test_a_missing_or_malformed_price_is_no_price(price_overview):
    response = steam_response(priced_payload(price_overview))

    with patch.object(poller.requests, "get", return_value=response):
        assert poller.get_game(FIRST_APPID) == {"name": "Portal 2", "price": None}


@pytest.mark.parametrize("name", [None, "", "   ", 5])
def test_an_unusable_name_becomes_none(name):
    response = steam_response(priced_payload({"currency": "USD", "final": 999}, name=name))

    with patch.object(poller.requests, "get", return_value=response):
        assert poller.get_game(FIRST_APPID) == {"name": None, "price": 999}


def test_http_429_raises_rate_limited():
    with patch.object(poller.requests, "get", return_value=steam_response({}, status=429)):
        with pytest.raises(poller.SteamRateLimited):
            poller.get_game(FIRST_APPID)


def test_a_server_error_raises_a_request_exception():
    with patch.object(poller.requests, "get", return_value=steam_response({}, status=503)):
        with pytest.raises(requests.RequestException):
            poller.get_game(FIRST_APPID)


@pytest.mark.parametrize("unusable_price", [None, True, False, "999", 9.99, -1])
def test_unusable_prices_are_rejected_before_any_connection(unusable_price):
    with patch.object(database, "_connect") as connect:
        with patch.object(database, "log_operation") as log_operation:
            alerts = database.process_price_observation(
                appid=FIRST_APPID,
                name="Portal 2",
                new_price=unusable_price,
            )

    assert alerts == []
    connect.assert_not_called()
    log_operation.assert_called_once()


def test_an_unknown_appid_raises_instead_of_writing():
    cursor = Mock()
    cursor.fetchone.return_value = None

    with pytest.raises(RuntimeError):
        database._run_poll_transaction(cursor, {"appid": 999_999_999})
