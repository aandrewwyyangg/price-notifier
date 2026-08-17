# Steam Notifier

A bot that watches Steam game prices and sends a Discord DM when one drops.

It started as a script with a `prices.json` file. Most of what is interesting
about it is *why* that file had to go, and what the replacement had to get right
to avoid spamming people or silently losing their data.

**Status:** the storage layer, the poller, and the Discord command layer are
complete and tested against a real PostgreSQL and the live Steam API. What
remains is going live: a bot token, a Supabase database, and a Replit Reserved
VM, all configuration rather than code — the exact steps are in
[DEPLOY.md](DEPLOY.md). See [Current status](#current-status).

---

## How it works

```
                    ┌──────────────────┐
                    │  Steam store API │
                    └────────┬─────────┘
                             │ HTTP, 1 req/sec
                             ▼
  ┌───────────────────────────────────────────────┐
  │ poller  (steampricingproject.py)              │
  │   1. ask the DB which appids anyone watches   │
  │   2. fetch each one                           │
  │   3. hand each observation to the DB layer    │
  └────────────────────┬──────────────────────────┘
                       │ list[UserAlert]
                       ▼
  ┌───────────────────────────────────────────────┐
  │ database.py — one transaction per observation │
  │   record fetch → insert price if changed      │
  │   → seed new ratchets → ratchet + return      │
  │     the users to notify                       │
  └────────────────────┬──────────────────────────┘
                       │ COMMIT happens here,
                       │ before any DM is sent
                       ▼
  ┌───────────────────────────────────────────────┐
  │ two channels, kept separate                   │
  │   UserAlert  → returned → bot.py sends the DM │
  │   log_operation → printed → stderr            │
  └───────────────────────────────────────────────┘
```

| File | Role |
| --- | --- |
| `bot.py` | The Discord client: slash commands, the poll loop, DM delivery. |
| `steampricingproject.py` | Polls Steam. Owns the retry/skip behaviour and the currency guard. |
| `database.py` | Every connection and transaction. The only module that talks to PostgreSQL. |
| `queries.py` | The nine SQL statements, as named constants. |
| `output.py` | Formats user-facing alerts and operational logs, and keeps them apart. |
| `schema.sql` | The DDL, written by hand. |
| `fixtures.sql` | Repeatable seed data chosen to cover every nullable combination. |
| `docker-compose.yml` | The local PostgreSQL, reproducible from the repo — first boot applies the schema. |
| `verify_*.py` | Four test scripts (see [Tests](#tests)). |
| `DEPLOY.md` / `.replit` | The deployment path: Replit Reserved VM + Supabase. |

---

## Why a database?

The original version stored watches in a JSON file. Three independent problems
made that unfixable — not slow, *unfixable*, in the sense that no restructuring
of the JSON solves them.

**1. Concurrent writes destroy data.** The Discord bot writes when someone runs
`/watch`. The poll loop writes when a price changes. Both rewrite the whole
file, so the later write silently discards the earlier one — and the data being
discarded is the user's watch list, the one thing in the system that cannot be
regenerated. This alone forces a database.

**2. The lookup runs the wrong way.** The file stored user → games. Every price
drop asks the opposite question: game → users. That is an O(users × watches)
scan of the entire file on every observation. The JSON "fix" is a hand-
maintained reverse index, which means two copies of the truth that can disagree.

**3. A watch is an entity, not a string.** It needs a price threshold and its own
dedupe state. That is a row with columns, keyed by (user, game).

PostgreSQL over SQLite because the deployment target is a managed database, and
porting SQLite → PostgreSQL later would have produced nothing but work.

---

## Schema

```sql
games    appid PK, name (nullable), last_checked_at
prices   appid FK, cents, checked_at   PK (appid, checked_at)   -- history
watches  discord_user_id BIGINT, appid FK, alert_below, last_alerted_cents
                                       PK (discord_user_id, appid)
         INDEX watches_appid_idx ON (appid)
```

**`prices` is a history table, not a `last_price` column.** Current price is
`ORDER BY checked_at DESC LIMIT 1`. Nothing is duplicated, and the price history
is free.

**Rows are inserted only when the price changes.** So a row in `prices` means the
price *moved*. A useful consequence falls out for free: the first row for an
appid is the baseline, which is exactly the "first observation never alerts"
rule, with no code implementing it.

**`watches_appid_idx` is the fix for failure #2 above.** The index that serves
game → users is the reason the database exists at all.

**Derived data cascades; authoritative data restricts.** `prices` is
`ON DELETE CASCADE` — it is re-fetchable and dies with its game. `watches` is
`ON DELETE RESTRICT` — deleting a watched game is *blocked*, because removing it
must be a deliberate, user-facing act (DM the watchers, then remove), never a
silent side effect of a cleanup script. This one line is the spine of the schema.

**`discord_user_id` is `BIGINT`.** Discord IDs are snowflakes and overflow
`INTEGER`.

---

## The decisions worth defending

### Alert spam is prevented by a ratchet, not a cooldown

`watches.last_alerted_cents` records the price a user was last told about. An
alert fires only when the new price is *strictly* lower. A user alerted at
$14.99 hears nothing about a rise back to $16.99, and nothing about a second
observation at $14.99 — only about a new low.

Strict `<` matters: a freshly seeded baseline must not alert at an equal price.

### `/watch` is a pure database write, and both nullable columns are why

`last_alerted_cents` and `games.name` are both nullable. The alternative — seed
them at watch time — requires a real price and a real name, which requires a
**synchronous Steam API call inside an interactive Discord command handler**.
That puts a network timeout on a path with a ~3 second deadline and makes
`/watch` fail whenever Steam is down.

Nullable keeps `/watch` to two `INSERT`s in one transaction, and moves the
seeding to the poller, where Steam being down is already an expected and handled
condition. The cost is one documented NULL branch in one predicate. The benefit
is that a user can add a watch while Steam is unreachable.

A placeholder (`"Unknown Game"`) was rejected instead of nullable `name`: fake
data leaks into DMs and fails *silently*, where a NULL fails visibly at the one
place that must handle it. Every output path falls back to `appid 292030` rather
than inventing a name.

> The trap this creates: `new_cents < NULL` evaluates to NULL, and `WHERE` drops
> NULL rows. A forgotten NULL branch does not error — the watch just never fires,
> invisibly, until a user asks why they got nothing. A `CHECK` constraint will
> not catch it either, because a CHECK passes when its expression is NULL.
> `fixtures.sql` seeds one row for each of the four nullable combinations for
> exactly this reason.

### "Successful fetch" means a usable price, not an HTTP 200

`games.last_checked_at` exists to distinguish "this price is stable" from "the
fetcher died three days ago." If it advanced on any HTTP response, then a week of
price-less replies from Steam would leave every game looking freshly checked
while no price was being recorded — the column would mask the exact failure it
was added to reveal.

So a response without a usable integer price writes **nothing at all**, and the
stalled timestamp becomes the visible symptom.

(The guard checks `isinstance(price, bool)` first, because `bool` subclasses
`int` in Python and `True` would otherwise be stored as 1 cent.)

### Delivery is at-most-once, on purpose

The ratchet is committed **before** the caller learns who to DM. This is enforced
structurally: finding watchers and ratcheting them is a single
`UPDATE ... RETURNING`, so there is no window in which the code could send a DM
it has not already recorded.

A crash between the commit and the DM therefore loses that alert. The
alternative — send first, record after — loses the crash but risks DMing the same
user repeatedly, which is much worse for a bot whose entire value is not being
annoying. The proper fix is a transactional outbox; it is documented below as a
known limitation rather than built.

Corollary: `UPDATE ... RETURNING` has **no guaranteed row order**. The caller
sorts.

### Database errors: a startup preflight, not exception sniffing

The rule:

- **Startup:** connect once before the bot starts. If it fails for any reason,
  log and exit non-zero. One rule makes wrong password, wrong user, wrong
  database, wrong host and wrong port all fatal.
- **Runtime:** every `psycopg.OperationalError` is transient. Log it and retry
  next cycle — the poll loop skips a cycle, the per-game loop skips a game.
- **Everything else fails fast.** `ProgrammingError`, `DataError`,
  `IntegrityError` and a missing environment variable are siblings of
  `OperationalError` in the DBAPI hierarchy, not subclasses, so the existing
  handlers already let them through. Nothing to add.

The plan had been to catch SQLSTATE class 28 (auth failure) at runtime. **That
check cannot fire.** Measured on psycopg 3.3.4: wrong password, unknown user and
unknown database all raise `OperationalError` with `sqlstate=None`, because they
happen at *connection* time and connection-time errors carry no SQLSTATE. Writing
it would have produced code that reads as if auth failure were handled while
wrong-password logged the same line every 60 seconds forever. Matching on the
message text was rejected too: `lc_messages` is a server setting, so a managed
database in another locale breaks it silently.

Retrying a wrong password is not resilience. Recovery requires correcting the
secret, so the process should stop and say so.

> Operational consequence: fail-fast plus `Restart=always` is a crash loop that
> logs once per restart — the same spam, relocated. Pair it with
> `StartLimitBurst`/`StartLimitIntervalSec` so the unit gives up and sits in
> `failed`.

### One process, not two

The Discord client and the poller run in the same process. A separate poller
process would have no Discord connection and could not send DMs, which would
force a message queue between them — the transactional outbox this milestone
deliberately deferred. Splitting them would have silently expanded the scope.

Blocking work (`psycopg`, `requests`) goes through `asyncio.to_thread` so it
stays off the event loop. Nothing shares a connection between threads, which is
true for free because every function opens and closes its own — the "connection
per call" limitation is what buys the thread safety.

### Two output channels

User alerts are **returned** as `UserAlert` values so the Discord layer decides
delivery. Operational logs are **printed** to stderr. Nothing prints an alert and
nothing returns a log, so the separation is enforceable at the shell
(`2>/dev/null`) rather than being a convention inside the code.

Money is integer cents everywhere, including on the display path —
`format_price` uses `divmod`, never `cents / 100`.

---

## Known limitations

These are deliberate. Each was reasoned about and left unbuilt because building
it would have cost more than the problem it solves, at this scale.

| Limitation | Why it is acceptable | The real fix |
| --- | --- | --- |
| **A crash between commit and DM loses that alert.** | At-most-once was chosen over at-least-once: a lost alert is better than a repeated DM. | Transactional outbox. |
| **One connection per call.** A 20-game watch list opens 20 connections per cycle. | Fine at a handful of users; connection setup is not the bottleneck at this scale. | `psycopg_pool`. |
| **Cycle time grows with the watch list.** Steam's rate limit costs ~1s per game, so N games make the effective poll period N seconds, not 60. | This bot is not expected to reach a watch list where that matters. | Async fetching or a real scheduler. |
| **A stalled fetcher is recorded but not detected.** `last_checked_at` is written; nothing reads it. | Storing the signal is what makes detection *possible* later; detecting it needs alerting infrastructure that does not exist. | A staleness check with somewhere to report to. |
| **Free-to-play and unreleased games stall permanently.** They have no price, so nothing is ever written and `last_checked_at` never advances — which under the rule above is indistinguishable from a dead fetcher. | The rule conflates "Steam failed to return a price" with "Steam has no price," and only the first is transient. Separating them means a second kind of not-a-price in the schema, for a case that affects games nobody watches for sale alerts. | Record *why* a fetch produced no price, not just that it didn't. |
| **A user with DMs closed holds a watch that can never fire,** and its ratchet keeps advancing past prices they will never see. | Reversing the ratchet needs a second transaction after the commit, which reintroduces the duplicate-DM risk that at-most-once exists to avoid. | Track delivery failures and disable the watch. |
| **A password rotated while running** is not caught by a startup check, so it degrades to the transient branch and retries forever. | Rotation is rare, operator-driven, and visible in the logs. | Re-run the preflight on repeated failures. |
| **No migration tooling.** `schema.sql` is applied by hand. | One table set, one deploy, no production data to preserve yet. | Alembic, when there is a second migration. |

---

## Running it locally

Requires Docker and Python 3.11+ (developed on 3.14).

```powershell
cp .env.example .env          # then fill in POSTGRES_PASSWORD
docker compose up -d
docker compose ps             # wait for "healthy" — "Up" is not enough

python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt

.\venv\Scripts\python.exe steampricingproject.py   # poller alone, no Discord
.\venv\Scripts\python.exe bot.py                   # the real thing (needs DISCORD_BOT_TOKEN)
```

A fresh volume applies `schema.sql` and `fixtures.sql` automatically on first
boot. To re-apply them to an existing database:

```powershell
Get-Content schema.sql   -Raw | docker compose exec -T db psql -U postgres -d steam_notifier
Get-Content fixtures.sql -Raw | docker compose exec -T db psql -U postgres -d steam_notifier
```

PowerShell has no `<` redirect, hence the `Get-Content | ` form. Keep `-T` on
`compose exec` so no TTY is allocated when stdin is a pipe.

Configuration is entirely environment-driven through `database.py::connection_args()`,
whose defaults are the local compose values. Pointing at a managed database is a
change to `.env`, not to code.

---

## Tests

There is no test runner. Each verifier is a plain script that prints its cases
and ends with `All ... passed.`, or dies on an assertion.

```powershell
.\venv\Scripts\python.exe verify_errors.py      # no DB, no network, instant
.\venv\Scripts\python.exe verify_prices.py      # needs the container
.\venv\Scripts\python.exe verify_watches.py     # needs the container
.\venv\Scripts\python.exe verify_app_query.py   # needs the container
```

All four are **hermetic**. The three database tests load `fixtures.sql`
themselves, run everything on one connection, and roll back in a `finally`, so
order does not matter and nothing needs reloading afterwards. Three things make
that work:

- They strip `BEGIN;`/`COMMIT;` from the fixture file — a `COMMIT` there would
  persist the reload and split the run across transactions.
- They use a bare `psycopg.connect()`, not `with psycopg.connect(...)`, because
  the context manager commits on clean exit.
- `TRUNCATE` is transactional in PostgreSQL. This approach would leak state on
  MySQL.

`verify_app_query.py` uses `conn.transaction()` as a SAVEPOINT so an expected
`UniqueViolation` does not poison the outer transaction — that is the trick that
lets one connection test failure paths.

A test that cannot fail is not evidence: `verify_errors.py`'s "continued to the
next game" case uses **two** appids, the first failing. With one appid, `continue`
and `return` are observationally identical and the assertion proves nothing. The
fix was confirmed by mutation — changing the `continue` to `return` makes the new
test fail and made the old one pass.

### Mutation testing the suite

Applying that idea systematically — deliberately introducing twelve bugs to see
which ones the green suite would notice — was the most useful hour spent on this
project. It caught the ones that mattered most: flipping the ratchet's `<` to
`<=`, reversing the `COALESCE` direction, negating `IS DISTINCT FROM`, and
deleting the `alert_below IS NULL` branch.

Four survived, and one of them was a **documentation** bug rather than a code
bug. I had written that the seed-then-ratchet statement ordering is what stops a
brand-new watch from alerting immediately. Reversing the order proved otherwise —
the new watch stays silent either way, because a second, independent guard takes
over. The real mechanism is the **strict `<`** against the just-seeded price; it
is the only one of the three candidate guards that cannot be removed without
alerts firing.

The code was right. My explanation of *why* it was right was wrong, and nothing
short of breaking it on purpose would have surfaced that.

---

## Current status

**Done and proven.** One live cycle against the real Steam API exercised the
whole path in a single run: a stable price wrote no new row; a game with no
history stored its baseline and alerted nobody; a NULL name was filled in from
Steam; one user's ratchet moved and she got the only DM; three other ratchets
seeded silently; every `last_checked_at` advanced. Exactly one line went to
stdout and seven to stderr, with no crossover.

**Also done.** The Discord layer is implemented and its startup paths are
proven — a missing token raises, a wrong database password fails the preflight
with one clean line and exit 1. All nine queries are covered by the verifiers,
including `/list`'s join with both of its NULLs (a game with no name yet, a
game with no price yet) and `/unwatch`'s removed-versus-absent distinction.
Prices are guarded by currency: Steam geo-gates on the requesting IP, so a
deployed host quoted anything but the expected currency treats the response as
a read failure rather than storing a wrong denomination.

**Not live yet.** The bot has not connected to Discord (no token created) and
nothing is deployed. Both are configuration rather than code, thanks to
`connection_args()`: the path — Replit Reserved VM background worker for the
bot, Supabase for PostgreSQL, secrets in place of `.env` — is written down
step by step in [DEPLOY.md](DEPLOY.md). Request-driven platforms (Cloud Run,
Replit Autoscale) were rejected because a persistent gateway connection looks
idle to a scale-to-zero platform.
