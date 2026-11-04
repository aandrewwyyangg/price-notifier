# Steam Notifier

A Discord bot that watches Steam game prices and sends you a DM when one drops.

## Commands

| Command | What it does |
| --- | --- |
| `/watch <appid> [alert_below]` | Watch a game. Set `alert_below` to only hear about prices under that amount. Leave it empty to hear about every new low. |
| `/unwatch <appid>` | Stop watching a game. |
| `/list` | Show the games you're watching. |

The appid is the number in the store URL. Portal 2 is `620`. `/watch` looks the appid up on Steam before saving it, so a typo gets an answer instead of a watch that never fires. Replies are visible only to you, and each user can watch up to 25 games.

## How it works

One process runs both the Discord client and the price poller.

1. Every 15 minutes the poller asks PostgreSQL which games anyone is watching, least recently checked first, then fetches each one from Steam's `appdetails` API, one request per second.
2. Each price goes through one transaction: record the fetch, store the price if it changed, give brand-new watches a silent baseline, then move each watcher's ratchet and return who to notify, in a single `UPDATE ... RETURNING`.
3. The transaction commits, then the bot sends the DMs.

A watcher only hears about a price that is lower than the last price they were told about, and below their `alert_below` if they set one. Rises and repeats are silent. The first price seen for a new watch is its silent starting point, so a game already under `alert_below` alerts only once it drops again; the `/watch` reply says so.

Delivery is at-most-once on purpose. The ratchet commits before the DM goes out, so a crash between the two loses that one alert instead of risking the same DM twice.

psycopg and requests are blocking, so the poll cycle and command database calls run in `asyncio.to_thread` to keep that work off Discord's event loop. The startup database check runs before the Discord client starts.

Every transaction sets a 10-second `statement_timeout`, so a stuck lock fails one call instead of freezing the poll loop. `/watch` takes a per-user advisory lock (`pg_advisory_xact_lock`) before checking the limit, so several commands sent at once can't take a user past it.

| File | Role |
| --- | --- |
| `bot.py` | Discord client: slash commands, the poll loop, DM delivery, startup checks. |
| `steampricingproject.py` | The poller: fetches prices, skips a game that fails, stops early if Steam rate-limits. |
| `database.py` | Every connection and transaction. |
| `queries.py` | The SQL. |
| `output.py` | Message formatting and logging. |
| `schema.sql` | The schema. |
| `fixtures.sql` | Test data that covers each nullable column. |

## Running locally

Needs Docker and Python 3.13 or newer. On Windows (PowerShell):

```powershell
Copy-Item .env.example .env        # then set POSTGRES_PASSWORD and DISCORD_BOT_TOKEN
docker compose up -d --wait        # local PostgreSQL; a fresh volume gets schema.sql and fixtures.sql
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\venv\Scripts\python.exe bot.py
```

On macOS or Linux use `cp` and `venv/bin/python` instead.

While developing, set `DISCORD_GUILD_ID` to your test server's id: commands sync there instantly, while a global sync can take up to an hour. The fixtures contain made-up users, so expect a few "Could not DM user" lines locally.

`python steampricingproject.py` runs the poller on its own and prints alerts instead of sending DMs.

To run the bot in a container next to the database: `docker compose --profile bot up -d --build`.

## Configuration

Everything comes from environment variables: `.env` locally, the platform's secrets when deployed.

| Variable | Default | Notes |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | none | Required. |
| `POSTGRES_PASSWORD` | none | Required. |
| `POSTGRES_HOST` | `127.0.0.1` | On Windows use `127.0.0.1`, not `localhost`, or every connect waits out an IPv6 timeout first. |
| `POSTGRES_PORT` | `5432` | |
| `POSTGRES_DB` | `steam_notifier` | |
| `POSTGRES_USER` | `postgres` | |
| `POSTGRES_SSLMODE` | libpq's `prefer` | Set `require` for a managed database. |
| `POSTGRES_CONNECT_TIMEOUT` | `10` | Seconds. |
| `STEAM_COUNTRY_CODE` | `us` | Keep `us` for the supported USD setup. |
| `STEAM_EXPECTED_CURRENCY` | `USD` | Keep `USD`. A response in another currency is treated as a failed read. |
| `MAX_WATCHES_PER_USER` | `25` | |
| `DISCORD_GUILD_ID` | unset | Development only. |

Only USD is supported: thresholds and messages use dollars, and stored prices have no currency field. Changing the expected currency can mix currencies in the price history and produce false drops.

## Tests

```powershell
docker compose up -d --wait
.\venv\Scripts\python.exe -m pytest
```

The database tests load `fixtures.sql` inside a transaction and roll it back afterwards, so they leave the database as they found it, and they refuse to run against anything but a local host. Functions that open their own connection get a savepoint inside that transaction instead, so the real code runs without escaping the rollback. If no database is reachable those tests are skipped; `REQUIRE_DB=1` makes that a failure, which is how CI runs them (GitHub Actions with a `postgres:18` service, Python 3.13 and 3.14).

## Deploying

The bot keeps a persistent outbound connection to Discord and serves no HTTP, so it needs an always-on host. Scale-to-zero platforms such as Cloud Run or Replit Autoscale see it as idle.

1. Create the database (Supabase's free tier works) and apply `schema.sql` once. Never load `fixtures.sql` into production.
2. On Supabase, use the **session** pooler: the direct host can be IPv6-only, and the transaction pooler breaks psycopg's prepared statements. Set `POSTGRES_SSLMODE=require`.
3. Run the bot with the variables above, either way:
   - **Replit:** `.replit` publishes it as a Reserved VM background worker. Put the variables in Secrets.
   - **Any Docker host**, such as a small GCE VM: `docker build -t steam-notifier .`, then `docker run -d --restart unless-stopped --env-file prod.env steam-notifier`. The image sets `STOPSIGNAL SIGINT`, so `docker stop` shuts the bot down cleanly instead of waiting 10 seconds and killing it.

At startup the bot checks that the database is reachable and the required tables exist, and exits non-zero if either check fails. It does not validate every column or constraint. Logs go to stderr with timestamps.

## Known limitations

- A crash between the commit and the DM loses that alert. A transactional outbox would keep pending alerts for retries, but a retry can send a duplicate if the previous DM succeeded before the sender recorded it.
- A user with DMs closed keeps a watch that can never reach them, and its ratchet keeps moving.
- Free-to-play and unreleased games have no price, so their watches stay silent until Steam lists one.
- Every database call opens its own connection; `psycopg_pool` would be the next step.
- A cycle costs about a second per watched game, and Steam's undocumented rate limit (commonly reported as about 200 requests per five minutes) ends a cycle early. Games rotate, least recently checked first, so none is skipped forever, but past a couple of hundred games each one is checked less often. Going bigger would need backoff and one request budget shared with `/watch`'s lookups.
