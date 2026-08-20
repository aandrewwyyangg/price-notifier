"""Discord command handlers, proven with mocks — no gateway, no database.

The handlers are the one layer with no coverage otherwise: they cannot be
reached by the database verifiers (those test SQL) and a live bot proves only
the happy path. Everything here patches names in BOT's namespace, for the same
reason verify_errors.py patches the poller's — bot.py imported these at import
time, so its module global is the reference that actually gets called.

The commands are app_commands.Command objects after decoration; .callback is
the original coroutine, which is what a real interaction ultimately invokes.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import psycopg

import bot


ALICE = 100000000000000001
PORTAL_2 = 620


def make_interaction(user_id=ALICE):
    """A fake Interaction with just the surface the handlers touch."""
    interaction = SimpleNamespace()
    interaction.user = SimpleNamespace(id=user_id)
    interaction.response = SimpleNamespace(defer=AsyncMock())
    interaction.followup = SimpleNamespace(send=AsyncMock())
    return interaction


def replies(interaction):
    return [call.args[0] for call in interaction.followup.send.call_args_list]


def only_reply(interaction):
    sent = replies(interaction)
    assert len(sent) == 1, f"expected exactly one reply, got {sent}"
    return sent[0]


def deferred_then(interaction, name, return_value):
    """A side_effect that fails if the database is reached before defer().

    Discord's interaction deadline is ~3s and the connect timeout is 10s, so
    without a defer an unreachable database blows the deadline and the user
    sees "The application did not respond" instead of the error we wrote.
    This is the only assertion that can catch a defer() being moved or
    dropped — every other case would still pass.
    """

    def check(*args, **kwargs):
        assert interaction.response.defer.called, (
            f"{name} reached the database before defer()"
        )
        return return_value

    return check


# --- /watch: the happy path, and defer() before the database. ---
interaction = make_interaction()

with patch.object(bot, "create_watch") as create_watch:
    create_watch.side_effect = deferred_then(interaction, "/watch", True)
    asyncio.run(bot.watch.callback(interaction, PORTAL_2, "9.99"))

interaction.response.defer.assert_awaited_once_with(ephemeral=True)
create_watch.assert_called_once_with(ALICE, PORTAL_2, 999)
reply = only_reply(interaction)
assert "$9.99" in reply, reply
# The D2 limitation is user-facing on purpose: /watch is a pure database write
# and cannot know whether Steam prices this appid.
assert "free-to-play or unreleased" in reply, reply
print("/watch: deferred first, dollars parsed to cents, D2 caveat stated")


# --- /watch: a duplicate is a reply, not an error. ---
interaction = make_interaction()

with patch.object(bot, "create_watch", return_value=False):
    asyncio.run(bot.watch.callback(interaction, PORTAL_2, None))

assert only_reply(interaction) == "You're already watching that game."
print("/watch: create_watch False becomes the already-watching reply")


# --- /watch: no threshold means no threshold, not zero. ---
# A 0-cent threshold would silence the watch forever, so None must survive
# all the way to the database call untouched.
interaction = make_interaction()

with patch.object(bot, "create_watch", return_value=True) as create_watch:
    asyncio.run(bot.watch.callback(interaction, PORTAL_2, None))

create_watch.assert_called_once_with(ALICE, PORTAL_2, None)
assert "below" not in only_reply(interaction)
print("/watch: an absent threshold stays None")


# --- /watch: rejected input must never reach the database. ---
# appid is INTEGER in Postgres; past PG_INTEGER_MAX the INSERT raises
# DataError, which is NOT an OperationalError and would escape the handler,
# leaving the interaction deferred forever. The bound belongs in the handler.
rejected_inputs = [
    ("appid zero", 0, None),
    ("appid negative", -5, None),
    ("appid past INTEGER max", bot.PG_INTEGER_MAX + 1, None),
    ("price unparseable", PORTAL_2, "nine dollars"),
    ("price empty", PORTAL_2, "   "),
    ("price past INTEGER max", PORTAL_2, "99999999999.99"),
]

for label, appid, alert_below in rejected_inputs:
    interaction = make_interaction()

    with patch.object(bot, "create_watch") as create_watch:
        asyncio.run(bot.watch.callback(interaction, appid, alert_below))

    create_watch.assert_not_called()
    assert len(replies(interaction)) == 1, f"{label}: {replies(interaction)}"

print(f"/watch: all {len(rejected_inputs)} rejected inputs answered without a write")


# --- Every command answers an unreachable database instead of raising. ---
# OperationalError is the transient branch. A command is interactive: it gets
# a sentence, not a silent retry and not a stack trace.
transient_cases = [
    ("/watch", "create_watch", lambda i: bot.watch.callback(i, PORTAL_2, None)),
    ("/unwatch", "delete_watch", lambda i: bot.unwatch.callback(i, PORTAL_2)),
    ("/list", "get_user_watches", lambda i: bot.list_watches.callback(i)),
]

for label, database_function, invoke in transient_cases:
    interaction = make_interaction()

    with patch.object(
        bot,
        database_function,
        side_effect=psycopg.OperationalError("database unavailable"),
    ):
        asyncio.run(invoke(interaction))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert "try again shortly" in only_reply(interaction), label

print("/watch /unwatch /list: an unreachable database is answered, not raised")


# --- /unwatch: removed and absent are different replies. ---
for removed, expected in [(True, "No longer watching"), (False, "weren't watching")]:
    interaction = make_interaction()

    with patch.object(bot, "delete_watch") as delete_watch:
        delete_watch.side_effect = deferred_then(interaction, "/unwatch", removed)
        asyncio.run(bot.unwatch.callback(interaction, PORTAL_2))

    delete_watch.assert_called_once_with(ALICE, PORTAL_2)
    assert expected in only_reply(interaction), replies(interaction)

print("/unwatch: removed and not-watching produce different replies")


# --- /list: empty is a normal answer; rows render through build_watch_line. ---
interaction = make_interaction()

with patch.object(bot, "get_user_watches", return_value=[]):
    asyncio.run(bot.list_watches.callback(interaction))

assert "aren't watching anything" in only_reply(interaction)

interaction = make_interaction()
listed = [
    SimpleNamespace(appid=PORTAL_2, name="Portal 2", alert_below=500, latest_cents=999),
    # NULL name and NULL latest price — the two states /list must render
    # without inventing a placeholder.
    SimpleNamespace(appid=292030, name=None, alert_below=None, latest_cents=None),
]

with patch.object(bot, "get_user_watches") as get_user_watches:
    get_user_watches.side_effect = deferred_then(interaction, "/list", listed)
    asyncio.run(bot.list_watches.callback(interaction))

rendered = only_reply(interaction)
assert "Portal 2 — latest price $9.99, alerting below $5.00" in rendered, rendered
assert "appid 292030 — latest price unavailable" in rendered, rendered
print("/list: empty answered, NULL name and NULL price both rendered")


# --- deliver_alert: a failed DM is logged and dropped, never raised. ---
# An exception escaping here kills the tasks.loop iteration and silently ends
# all polling. The ratchet already committed, so dropping is the only correct
# move — reversing it would need a second transaction and reintroduce the
# duplicate-DM risk at-most-once exists to avoid.
alert = bot.UserAlert(
    discord_user_id=ALICE,
    appid=PORTAL_2,
    message="Portal 2 dropped to $4.99.",
)
http_response = Mock(status=403, reason="Forbidden")

for label, error in [
    ("DMs closed", discord.Forbidden(http_response, "cannot send messages to this user")),
    ("transient HTTP", discord.HTTPException(http_response, "service unavailable")),
]:
    with patch.object(bot.client, "fetch_user", side_effect=error):
        with patch.object(bot, "log_operation") as log_operation:
            asyncio.run(bot.deliver_alert(alert))

    log_operation.assert_called_once()
    logged = log_operation.call_args.args[0]
    # UserAlert carries appid precisely so this log can name the game without
    # parsing it back out of the DM text.
    assert str(ALICE) in logged and str(PORTAL_2) in logged, f"{label}: {logged}"

print("deliver_alert: both failure modes logged with user and appid, neither raised")


# --- deliver_alert: the happy path actually sends the message. ---
recipient = SimpleNamespace(send=AsyncMock())

with patch.object(bot, "log_operation") as log_operation:
    with patch.object(bot.client, "fetch_user", new=AsyncMock(return_value=recipient)) as fetch_user:
        asyncio.run(bot.deliver_alert(alert))

fetch_user.assert_awaited_once_with(ALICE)
recipient.send.assert_awaited_once_with(alert.message)
log_operation.assert_not_called()
print("deliver_alert: a deliverable alert is sent and logs nothing")

print("All command-handler cases passed.")
