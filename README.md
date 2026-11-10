# Steam Notifier

A Discord bot that watches Steam game prices and sends you a DM when one drops.

## Commands

| Command | What it does |
| --- | --- |
| `/watch <appid> [alert_below]` | Watch a game. Set `alert_below` to only hear about prices under that amount. Leave it empty to hear about every new low. |
| `/unwatch <appid>` | Stop watching a game. |
| `/list` | Show the games you're watching. |

The appid is the number in the store URL. Portal 2 is `620`. `/watch` validates its format and saves directly to PostgreSQL, so adding a watch works while Steam is unavailable. The poller looks up the name and price later; a nonexistent appid stays silent until you `/unwatch` it. Replies are visible only to you, and each user can watch up to 25 games.

## How it works

One process runs both the Discord client and the price poller.

1. Every 15 minutes the poller asks PostgreSQL which games anyone is watching, least recently checked first, then fetches each one from Steam's `appdetails` API, one request per second. After a rate limit, the next cycle resumes at the interrupted game and wraps around, so games with unreadable prices cannot hold up the rest indefinitely.
2. Each price goes through one transaction: record the fetch, store the price if it changed, give brand-new watches a silent baseline, then move each watcher's ratchet and return who to notify, in a single `UPDATE ... RETURNING`.
3. The transaction commits, then the bot sends the DMs.

A watcher only hears about a price that is lower than the last price they were told about, and below their `alert_below` if they set one. Rises and repeats are silent. The first price seen for a new watch is its silent starting point, so a game already under `alert_below` alerts only once it drops again; the `/watch` reply says so.

Delivery is at-most-once on purpose. The ratchet commits before the DM goes out, so a crash between the two loses that one alert instead of risking the same DM twice.

psycopg and requests are blocking, so the poll cycle and command database calls run in `asyncio.to_thread` to keep that work off Discord's event loop. The startup database check runs before the Discord client starts.

Every transaction sets a 10-second `statement_timeout`, so a stuck lock fails one call instead of freezing the poll loop. `/watch` takes a per-user advisory lock (`pg_advisory_xact_lock`) before checking the limit, so several commands sent at once can't take a user past it.

Unusable prices, including values outside PostgreSQL's integer range, write nothing. Steam request failures and runtime `psycopg.OperationalError` are logged and retried on later polls; HTTP 429 ends the current cycle. Unexpected programming, schema, or configuration errors stop polling: the bot attempts any alerts already collected, closes Discord, and exits nonzero. Standalone polling emits its collected alerts before failing in the same way.

| File | Role |
| --- | --- |
| `bot.py` | Discord client: slash commands, the poll loop, DM delivery, startup checks. |
| `steampricingproject.py` | The poller: fetches prices, handles expected read failures and rate limits, propagates unexpected failures with collected alerts. |
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

1. Create a Discord application in the [Developer Portal](https://discord.com/developers/applications), get its bot token, and invite it to your test server with the `bot` and `applications.commands` scopes. No privileged intents are needed. Allow DMs from that server's members.
2. Create a dedicated PostgreSQL database and apply `schema.sql` once. Never load `fixtures.sql` into production. On Supabase, **disable the unused Data API** before creating the tables: this bot connects with psycopg, and SQL-created tables can otherwise be exposed through API grants. See [Supabase's Data API security instructions](https://supabase.com/docs/guides/api/securing-your-api#disable-the-data-api).
3. On Supabase, copy the **session pooler** settings from the Connect dialog, including its `postgres.<project-ref>` username and `postgres` database name. The direct host can be IPv6-only; the shared transaction pooler does not support prepared statements. Set `POSTGRES_SSLMODE=require`. See [connection options](https://supabase.com/docs/guides/database/connecting-to-postgres).
4. Run **one bot process** against the database. Stop local development copies before starting the deployed copy; do not run the standalone poller alongside it. Use the configuration above:
   - **Replit:** import the release commit, put the variables in Secrets, and publish as a **Reserved VM / Background worker** using the committed `.replit` build and run commands. Verify the published app's secrets, including SSL and USD settings. See [Replit deployment types](https://docs.replit.com/features/publishing/deployment-types).
   - **Any Docker host**, such as a small GCE VM: build a versioned image with `docker build -t steam-notifier:<release> .`, then start it with `docker run -d --name steam-notifier --restart unless-stopped --stop-timeout 60 --env-file prod.env steam-notifier:<release>`. Replace `<release>` with the Git commit ID. Keep `prod.env` private on the host; it is excluded from Git and the Docker build context. The image uses `STOPSIGNAL SIGINT` for shutdown; the 60-second grace period lets an in-flight request or database transaction finish.

At startup the bot checks that the database is reachable and the required tables exist, and exits non-zero if either check fails. It does not validate every column or constraint. Logs go to stderr with timestamps.

A fatal exit does not guarantee that the deployment stays stopped: Docker's `unless-stopped` policy can restart a failed process. Repeated fatal failures require correcting the cause or rolling back the release, rather than relying on restarts to repair it. See [Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/).

Before release, run the full suite with `REQUIRE_DB=1` against local PostgreSQL so skipped database tests cannot pass the release check. Then use a test bot and test database for a real Discord smoke test:

- Confirm startup preflight, command sync and Steam price reads in the logs. Try `/watch 620`, the duplicate watch, `/list`, and `/unwatch 620`; replies should be private.
- To verify delivery without waiting for a sale, watch `620` without a threshold, wait for its baseline, and stop the bot. In the **test database only**, raise `last_alerted_cents` on just your own `(discord_user_id, appid)` watch above the current price, then restart. Confirm one DM, no repeat on the next cycle, and that your watch survives restart.

After publishing, repeat the command checks and confirm at least two poll cycles run while the local bot is stopped. Keep the last working release/image and a database backup. To roll back, stop the new process and start the previous release with the same secrets and database; do not reapply the schema or fixtures. The committed Compose file is for local development and loads fixtures on a fresh volume, so it is not the production database setup.

## Known limitations

- A crash between the commit and the DM loses that alert. A transactional outbox would keep pending alerts for retries, but a retry can send a duplicate if the previous DM succeeded before the sender recorded it.
- A user with DMs closed keeps a watch that can never reach them, and its ratchet keeps moving.
- Free-to-play and unreleased games have no price, so their watches stay silent until Steam lists one.
- Every database call opens its own connection; `psycopg_pool` would be the next step.
- A database password rotated while running still raises runtime `OperationalError` and follows the existing recovery policy. Startup preflight catches incorrect credentials on restart; runtime failures remain visible in logs.
- A cycle costs about a second per watched game, and Steam's undocumented rate limit can end a cycle early. Interrupted cycles resume on the next poll; the resume position is in memory and resets on restart. Larger watch lists mean each game is checked less often. Going bigger would need backoff and a shared request budget.
