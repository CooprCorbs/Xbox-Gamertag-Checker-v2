# Gamertag hunter

A Python CLI and Discord bot that look up Xbox profiles. **A 404 means only that
no profile was found.** Results are labelled **“potentially available — manual
verification required.”** This does not establish whether a gamertag can be
claimed. The application performs no browser opening, form filling, clipboard
assistance or claiming actions.

## Setup on Windows

Python 3.12 is recommended. From this project's directory:

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
xbox-authenticate
```

Authenticate interactively using the Xbox library's supported login flow.
If you use your own Azure application, give `xbox-authenticate` its matching
`--client-id` and `--client-secret` options, then configure the same application
in your local `.env`. Do not paste secrets into support messages or commit them.
The checker itself never opens a browser; the separate one-time authentication
utility may use an interactive login flow.

Copy `.env.example` to `.env` **only if you do not already have a `.env`**.
For the bot, set `DISCORD_TOKEN` locally. Invite it with the `bot` and
`applications.commands` scopes and permission to send messages and attach files.
Webhook/ntfy notifications are optional: configure `DISCORD_WEBHOOK`,
`DISCORD_PING` and/or `NTFY_TOPIC`. `DISCORD_PING` accepts comma-separated user IDs,
`&roleID`, `here` or `everyone`.

Do not overwrite your existing `.env`, token files, `browser_profile`,
`checked.txt` or `free.txt` when updating. Old browser data is left untouched and
is never used by this version. Playwright and browser installation are no longer
needed. Repository downloads omit credentials, browser data and local results. Retain
your existing `checked.txt` and `free.txt` when applying this update. Extract it into a separate
folder first; copying only changed source/config-example files into an existing
installation avoids replacing newer local results. Delete the old `claim.py`
source file when applying the update; leave private browser data alone.

## Discord

```bat
python bot.py
```

Or double-click `run_all.bat`, which starts **only the bot**, not a second hunter.

```text
/hunt length:4 count:1000
/hunt length:4 count:30 letters_only:true
/check tags:abcd, efgh
/stop
```

`/hunt` accepts 1–1000 candidates, with a default of 30. The bot synchronizes the
updated global command schema once during startup; Discord may take time to
refresh cached commands. It shows completed checked/requested counts, candidate
hits, attempted counts, failures, unknowns and elapsed time. Edits happen at most
once every three seconds during checking, plus the final edit. Slow/failed edits
do not block Xbox checks. Progress and final reports use ordinary bot channel
messages, so hunts can continue after the 15-minute interaction-token lifetime. Long reports are attached as UTF-8 text files.

`/stop` interrupts active hunt requests, authentication waits and retry backoff;
completed results stay saved. Outcomes are explicitly finished, cancelled or
failed. A depleted namespace reports how many candidates could actually be
scheduled. `/check` shares the same authenticated engine and rate limiter with
`/hunt`. Webhook/ntfy delivery runs independently and can continue after the hunt
finishes; candidate hits also appear in Discord's live/final reports. Failed
Discord delivery does not discard local results.

## CLI

Stop the bot before starting the CLI:

```bat
python hunter.py hunt --length 4 --count 1000 --notify
python hunter.py check list.txt --concurrency 2 --delay 1
python hunter.py hunt --forever --quiet --notify
python hunter.py hunt --count 100 --concurrency 1 --delay 10
```

`Ctrl+C` cancels work and preserves completed results. `--forever` repeats
successful batches, ending on cancellation, exhausted candidates or a failed
batch; it does not endlessly reauthenticate after errors.

Both interfaces default to **2 workers, at least 1 second between request
starts, a 20-second request timeout, and at most 3 retries**. Concurrency is
bounded to 1–8, regardless of the requested hunt size. These are conservative
starting settings, not a claim about an Xbox quota or guaranteed throughput.
Use `--concurrency`, `--delay`, `--timeout` for the CLI; use
`HUNT_CONCURRENCY`, `HUNT_DELAY`, `HUNT_TIMEOUT` in the bot's environment.
Increasing delay and reducing concurrency explicitly slows requests down.
There is no unnecessary delay after the final result.

The shared limiter honours `Retry-After` seconds and HTTP dates across commands
and workers. Transient network failures, timeouts, HTTP 408, 429, 500, 502, 503
and 504 have bounded exponential backoff with jitter. A 429 doubles the effective
start interval (minimum 1 second, maximum 60 seconds); the slower interval lasts
for the engine's lifetime. Server cooldowns are not capped or bypassed. HTTP
401/403 fails authentication and suppresses further requests for that engine;
reauthenticate separately and restart the application. No proxy or account
rotation is used. A Windows/POSIX process guard prevents separate instances using
the same OAuth application from competing; this conservatively also excludes
different accounts using that application. It coordinates this machine, not
checkers on other machines. Do not run independent checkers using the same
credentials elsewhere.

`xbox-webapi` is pinned to 2.1.0 because its request adapter was inspected and
tested: token refresh is locked, provider headers are copied per request, and
HTTPX's authenticated connection pool is reused. There are no threaded calls to
the client. Revalidate this adapter before upgrading that dependency.

## Results and privacy

- `free.txt` retains its historical filename and contains candidate tags requiring
  manual verification. Old entries are not reclassified or removed.
- `checked.txt` is case-insensitive history for completed **taken or potentially
  available** checks only. Hunts skip that history; explicit list checks may
  intentionally recheck it. Historical entries are preserved as supplied.
- `results.jsonl` records each finished attempt's classification, attempt count
  and safe diagnostic reason. Unknown/failed attempts stay eligible for future
  hunts and are never added to `checked.txt`.

Candidates are unique within each hunt and start with a letter. A random start
and coprime stride traverse the finite namespace without a rejection loop;
exhaustion returns fewer candidates instead of hanging. This is a permutation,
not a cryptographically random or uniform sample. Overlapping commands reuse
in-flight checks for the same case-insensitive tag. Result appends are flushed
and synced before progress is reported; existing result files are not truncated.
Disk-write failures stop a hunt with a failed outcome. Saved completed results
remain available even if notifications or Discord messages fail.

Notification requests use a separate pooled client and timeouts. The queue is
bounded to 1000; overflow or delivery failures report a safe warning and leave
the candidate in local results. Shutdown drains notifications for up to 15
seconds, then stops them; pending notifications are not guaranteed to be delivered.
Never share `.env`, auth tokens or private browser data. Imports, help commands,
tests and the benchmark do not load `.env` or make live Xbox requests.

## Verification

```bat
python -m pip install -r requirements-dev.txt
python -m pytest -q
python tools\benchmark.py
```

Mocked tests cover the 1000 boundary and synchronized schema, unique and
exhausted namespaces, both Retry-After formats, bounded retries, coordinated
throttling, request timeouts, cancellation during requests/backoff, classification,
process/session coordination, startup cleanup, and a complete 1000-tag Discord
hunt with live progress, bounded concurrency, saved results and a file attachment.

`simulated-benchmark.json` records an actual measurement using mocked latency:
100 tags, identical 0.5-second simulated network latency, time scaled by 0.002.
Old sequential scheduling uses a 10-second post-result delay; new scheduling uses
two workers and a 1-second start interval. Measured scaled wall time was
**2.1308 seconds old vs 0.2351 seconds new (9.06× in this simulation)**.
This tests scheduling with simulated latency, not live Xbox throughput; event-loop
overhead and time scaling affect results. Rerun the benchmark to measure your
machine. Authentication, live Xbox response semantics/quotas, real notification
delivery, Discord command propagation and native Windows execution still need
verification with your credentials and Windows installation. No live testing is
claimed.
