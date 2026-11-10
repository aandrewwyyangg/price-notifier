"""Process exits with real task/client cleanup and mocked external services."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


PROCESS_PROBE = r'''
import asyncio
import os
import time
from unittest.mock import AsyncMock

import psycopg
import bot
import steampricingproject as poller
from output import UserAlert

scenario = os.environ["SCOPE_SCENARIO"]
poller.REQUEST_SPACING_SECONDS = 0

def watched_appids():
    if scenario == "list_failure":
        raise RuntimeError("missing configuration")
    return [620, 570, 730]

def get_game(appid):
    if scenario == "signal":
        print("FETCH_STARTED", flush=True)
        time.sleep(2)
    return {"name": "Test game", "price": 999}

def process_price(**kwargs):
    appid = kwargs["appid"]
    print(f"OBSERVED {appid}", flush=True)
    if scenario == "fatal" and appid == 570:
        raise psycopg.ProgrammingError("schema mismatch")
    return [UserAlert(discord_user_id=1, appid=appid, message="test alert")]

async def deliver(alert):
    print(f"ATTEMPTED {alert.appid}", flush=True)

poller.get_watched_appids = watched_appids
poller.get_game = get_game
poller.process_price_observation = process_price
bot.deliver_alert = deliver
bot.preflight = lambda: None
bot.require_bot_token = lambda: "tokenless-test"
bot.client.wait_until_ready = AsyncMock()

async def run_client():
    async with bot.client:
        await bot.poll_prices.start()

def run_without_discord(*args, **kwargs):
    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        pass

# Exercise main's exit boundary without invoking Client.run or logging in.
bot.client.run = run_without_discord
if scenario == "success":
    bot.poll_prices.count = 1
    bot.poll_prices.change_interval(seconds=0)

if os.environ.get("SCOPE_MODE") == "standalone":
    poller.emit_user_alert = lambda alert: print(f"ATTEMPTED {alert.appid}", flush=True)
    poller.main()
else:
    bot.main()
'''


def run_probe(scenario, mode="bot"):
    environment = os.environ.copy()
    environment.update(
        PYTHON_DOTENV_DISABLED="1", SCOPE_SCENARIO=scenario, SCOPE_MODE=mode
    )
    return subprocess.run(
        [sys.executable, "-c", PROCESS_PROBE],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("mode", ["bot", "standalone"])
def test_fatal_process_attempts_prior_alerts_and_exits_nonzero(mode):
    result = run_probe("fatal", mode)
    assert result.returncode != 0
    assert result.stdout.splitlines() == ["OBSERVED 620", "OBSERVED 570", "ATTEMPTED 620"]
    assert "ProgrammingError: schema mismatch" in result.stderr
    assert "PollCycleError: Polling failed for appid 570" in result.stderr


def test_a_fatal_poll_list_error_exits_nonzero_without_processing_games():
    result = run_probe("list_failure")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "RuntimeError: missing configuration" in result.stderr


def test_a_successful_process_closes_normally():
    result = run_probe("success")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "OBSERVED 620", "OBSERVED 570", "OBSERVED 730",
        "ATTEMPTED 620", "ATTEMPTED 570", "ATTEMPTED 730",
    ]
