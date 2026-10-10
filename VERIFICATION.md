# Verification of this update

- Python 3.12.14 on Linux; xbox-webapi 2.1.0, discord.py 2.7.1,
  httpx 0.28.1, pytest 9.1.1 and pytest-asyncio 1.4.0.
- `python -m pytest -q`: **51 passed**. The only warning is discord.py's
  Python 3.12 `audioop` deprecation notice.
- The mocked 1000-tag Discord run completed all unique checks, saved all 1000
  classifications and history entries, saved every candidate hit, exercised
  live progress with throttled edits, and attached the full result list.
- Coverage includes the 1000 count boundary/default 30 and sync hook; exhausted
  candidate spaces; shared concurrency/throttling/deduplication; seconds and
  HTTP-date Retry-After; retries/timeouts; prompt cancellation during requests
  and backoff; failed authentication (including an auth-endpoint 404); safe result
  classification; append preservation and disk-write failure; startup cleanup;
  pooled/header-safe sessions; nonblocking notifications; POSIX process exclusion
  and a mocked Windows msvcrt process-lock branch.
- Long-hunt progress and output use ordinary bot channel messages, not expiring
  interaction followup webhooks. The mocked 1000-tag test asserts no followup
  webhook is used for reporting.
- CLI imports and `--help` work; removed browser-assistance flags are rejected.
- `tools/benchmark.py` compared scheduling with identical mocked latency.
  See `simulated-benchmark.json`: 2.1308 seconds old, 0.2351 seconds new,
  9.06 times in the scaled simulation. This is not measured live Xbox throughput.
- The uploaded `checked.txt` and `free.txt` are byte-for-byte unchanged.
  `.env`, token files and browser_profile were not opened during development,
  and private data is not included in the updated archive. The original uploaded
  ZIP is unchanged and remains separate.

No live Xbox calls, real Discord connections/command synchronization,
notification deliveries or native Windows execution were performed. Those need
verification with the user's local credentials, permissions and Windows system.
This report records local verification; no cloud environment was published.
