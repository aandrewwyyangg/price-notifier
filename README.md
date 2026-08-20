# Steam Notifier

A Discord bot that watches Steam game prices and sends you a DM when one drops.

## Commands

| Command | What it does |
| --- | --- |
| `/watch <appid> [alert_below]` | Watch a game. `alert_below` is optional — leave it empty to be alerted on every new low. |
| `/unwatch <appid>` | Stop watching a game. |
| `/list` | Show the games you're watching. |

The appid is the number in the store URL — `620` for Portal 2.

## Requirements

- Docker
- Python 3.11+
- A Discord bot token ([Developer Portal](https://discord.com/developers/applications) → Bot → Reset Token)

## Setup

```powershell
cp .env.example .env
```

Open `.env` and fill in `POSTGRES_PASSWORD` and `DISCORD_BOT_TOKEN`.

Start the database:

```powershell
docker compose up -d
docker compose ps
```

Wait until it reports `healthy` — `Up` is not enough. First boot applies
`schema.sql` and `fixtures.sql` automatically.

Install the dependencies:

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Running

```powershell
.\venv\Scripts\python.exe bot.py
```

To run the price poller on its own, without Discord:

```powershell
.\venv\Scripts\python.exe steampricingproject.py
```

## Tests

There is no test runner — each file is a script that prints its cases and
ends with `All ... passed.`, or dies on an assertion.

```powershell
.\venv\Scripts\python.exe verify_errors.py      # no database needed
.\venv\Scripts\python.exe verify_commands.py    # no database needed
.\venv\Scripts\python.exe verify_prices.py      # needs the container
.\venv\Scripts\python.exe verify_watches.py     # needs the container
.\venv\Scripts\python.exe verify_app_query.py   # needs the container
```

## Design notes

**Why a database and not a JSON file.** The original version stored watches in
`prices.json`. Two problems made that unfixable rather than merely slow. First,
the bot writes when someone runs `/watch` and the poll loop writes when a price
changes — both rewrite the whole file, so the later write silently discards the
earlier one, and what gets discarded is the user's watch list, the one thing
that cannot be regenerated. Second, the file stored user -> games, but every
price drop asks the opposite question, game -> users. That is a full scan on
every observation; the `watches_appid_idx` index is the fix, and serving that
one lookup is the reason the database exists.

PostgreSQL over SQLite because the deploy target is a managed database, and
porting later would have been pure work.

**Prices are a history table, not a `last_price` column.** Rows are inserted
only when the price changes, so a row means the price *moved*, and the history
is free. A useful consequence falls out with no code behind it: the first row
for a game is its baseline, which is exactly the "never alert on the first
observation" rule.

**Alert spam is stopped by a ratchet, not a cooldown.** `last_alerted_cents`
records the price a user was last told about, and an alert fires only when the
new price is *strictly* lower. Someone alerted at $14.99 hears nothing about a
rise back to $16.99 and nothing about a second observation at $14.99 — only
about a new low. The strict `<` is load-bearing: with `<=`, a freshly seeded
baseline would alert immediately at its own price.

**Delivery is at-most-once, deliberately.** The ratchet commits *before* the
caller learns who to DM, enforced structurally by doing both in a single
`UPDATE ... RETURNING`. A crash in between loses that alert. The alternative —
send first, record after — risks DMing the same user repeatedly, which is far
worse for a bot whose whole value is not being annoying. The real fix is a
transactional outbox; it is a known limitation, not a bug.

**Mutation testing found a bug in the documentation.** Deliberately introducing
twelve bugs to see which ones the passing test suite would catch was the most
useful hour on this project. It caught the ones that mattered — flipping the
ratchet's `<` to `<=`, reversing a `COALESCE`, negating an `IS DISTINCT FROM`.
Four survived, and one was a documentation bug rather than a code bug: I had
written that statement ordering is what stops a brand-new watch from alerting
immediately, and reversing the order proved otherwise. The real mechanism is
the strict `<` above. The code was right; my explanation of why was wrong, and
only breaking it on purpose surfaced that.

## Files

| File | Role |
| --- | --- |
| `bot.py` | The Discord client: slash commands, the poll loop, DM delivery. |
| `steampricingproject.py` | Polls the Steam API. |
| `database.py` | Every connection and transaction. The only module that talks to PostgreSQL. |
| `queries.py` | The SQL statements, as named constants. |
| `output.py` | Formats user-facing alerts and operational logs. |
| `schema.sql` | The tables. |
| `fixtures.sql` | Seed data for the tests. |
| `docker-compose.yml` | The local PostgreSQL. |

## Deploying

See [DEPLOY.md](DEPLOY.md).
