"""Shared, cancellation-aware profile checking. No authentication or UI side effects."""
import asyncio
import json
import math
import os
import random
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

POTENTIAL = 'potentially available — manual verification required'
TAKEN, UNKNOWN, FAILED = 'taken', 'unknown', 'failed'


class AuthenticationFailure(Exception):
    """A failure from an authentication endpoint, never a profile miss."""


@dataclass(frozen=True)
class Settings:
    concurrency: int = 2
    interval: float = 1.0
    timeout: float = 20.0
    retries: int = 3
    backoff: float = 2.0

    def __post_init__(self):
        if not 1 <= self.concurrency <= 8:
            raise ValueError('concurrency must be 1 to 8')
        if not math.isfinite(self.interval) or self.interval < 0:
            raise ValueError('delay must be finite and nonnegative')
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        if not 0 <= self.retries <= 5 or not math.isfinite(self.backoff) or self.backoff < 0:
            raise ValueError('invalid retry settings')


@dataclass(frozen=True)
class Result:
    tag: str
    status: str
    attempts: int = 1
    reason: str = ''

    @property
    def completed(self):
        return self.status in (TAKEN, POTENTIAL)


@dataclass
class Progress:
    requested: int
    scheduled: int
    checked: int = 0
    attempted: int = 0
    hits: int = 0
    failed: int = 0
    unknown: int = 0
    started: float = field(default_factory=time.monotonic)
    outcome: str = 'running'

    @property
    def elapsed(self):
        return time.monotonic() - self.started

    def summary(self):
        return (f'{self.outcome}: {self.checked}/{self.requested} checked, '
                f'{self.hits} candidate hits, {self.failed} failed, {self.unknown} unknown; '
                f'{self.attempted}/{self.scheduled} attempted; {self.elapsed:.1f}s elapsed')


async def pause(seconds, stop):
    if stop.is_set():
        raise asyncio.CancelledError
    if seconds <= 0:
        await asyncio.sleep(0)
        return
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        return
    raise asyncio.CancelledError


async def interruptible(awaitable, stop, timeout):
    task = asyncio.ensure_future(awaitable)
    waiter = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait((task, waiter), timeout=timeout,
                                    return_when=asyncio.FIRST_COMPLETED)
        if waiter in done or stop.is_set():
            raise asyncio.CancelledError
        if task not in done:
            raise TimeoutError
        return task.result()
    finally:
        for t in (task, waiter):
            if not t.done():
                t.cancel()
        await asyncio.gather(task, waiter, return_exceptions=True)


def retry_after(value, now=None):
    """Retry-After accepts seconds or an HTTP date, including past dates."""
    if not value:
        return None
    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return max(0.0, seconds)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, dt.timestamp() - (time.time() if now is None else now))
    except (TypeError, ValueError, OverflowError):
        return None


class Limiter:
    """One instance per authenticated engine, shared by all commands and workers."""
    def __init__(self, settings):
        self.settings = settings
        self.slots = asyncio.Semaphore(settings.concurrency)
        self.lock = asyncio.Lock()
        self.next_start = 0.0
        self.blocked_until = 0.0
        self.interval = settings.interval

    async def pace(self, stop):
        # Don't reserve future starts: a later 429 must affect waiting workers too.
        while True:
            async with self.lock:
                now = time.monotonic()
                delay = max(self.next_start, self.blocked_until) - now
                if delay <= 0:
                    if stop.is_set():
                        raise asyncio.CancelledError
                    self.next_start = now + self.interval
                    return
            await pause(delay, stop)

    async def throttle(self, wait):
        async with self.lock:
            self.interval = min(60.0, max(1.0, self.interval * 2))
            self.blocked_until = max(self.blocked_until, time.monotonic() + wait)


class ResultStore:
    """Append-only results; only successful classifications enter checked.txt.

    The process credential lease prevents competing writers. Appends happen without
    an await, so tasks on the same event loop cannot interleave. Every result is
    flushed before reporting progress; failures remain retryable in later hunts.
    """
    def __init__(self, root='.'):
        self.root = Path(root)
        self.seen = self._read('checked.txt')
        self.hits = self._read('free.txt')

    def _read(self, name):
        path = self.root / name
        return {x.strip().casefold() for x in path.read_text(encoding='utf-8').splitlines()
                if x.strip()} if path.exists() else set()

    def _append(self, name, text):
        path = self.root / name
        # Preserve an existing last line even when the old file has no final newline.
        with path.open('a+b') as f:
            f.seek(0, 2)
            if f.tell():
                f.seek(-1, 2)
                if f.read(1) != b'\n':
                    f.write(b'\n')
            f.write((text + '\n').encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())

    def save(self, result):
        self._append('results.jsonl', json.dumps(result.__dict__, ensure_ascii=False))
        key = result.tag.casefold()
        # Write hit first, so a subsequent checked-file write failure can be retried.
        if result.status == POTENTIAL and key not in self.hits:
            self._append('free.txt', result.tag)
            self.hits.add(key)
        if result.completed and key not in self.seen:
            self._append('checked.txt', result.tag)
            self.seen.add(key)


def candidates(length, count, letters_only=False, seen=(), rng=None):
    """Random permutation of the finite namespace: no rejection-loop exhaustion.

    A coprime stride visits each candidate exactly once, even near exhaustion.
    This permutation is not a cryptographically random or uniform subset.
    """
    if not 1 <= length <= 8 or count < 0:
        raise ValueError('length must be 1..8 and count nonnegative')
    rng = rng or random.SystemRandom()
    pool = string.ascii_lowercase if letters_only else string.ascii_lowercase + string.digits
    tail_size = len(pool) ** (length - 1)
    total = 26 * tail_size
    blocked = set()
    for tag in seen:
        tag = tag.casefold()
        if len(tag) != length or tag[0] not in string.ascii_lowercase or any(c not in pool for c in tag[1:]):
            continue
        idx = string.ascii_lowercase.index(tag[0])
        for char in tag[1:]:
            idx = idx * len(pool) + pool.index(char)
        blocked.add(idx)
    wanted = min(count, total - len(blocked))
    if not wanted:
        return []
    start = rng.randrange(total)
    stride = rng.randrange(1, total)
    while math.gcd(stride, total) != 1:
        stride = (stride + 1) % total or 1
    names = []
    for n in range(total):
        idx = (start + stride * n) % total
        if idx in blocked:
            continue
        first, rest = divmod(idx, tail_size)
        chars = []
        for _ in range(length - 1):
            rest, digit = divmod(rest, len(pool))
            chars.append(pool[digit])
        names.append(string.ascii_lowercase[first] + ''.join(reversed(chars)))
        if len(names) == wanted:
            break
    return names


class CheckingEngine:
    def __init__(self, lookup, settings=None, store=None, rng=None):
        self.lookup = lookup
        self.settings = settings or Settings()
        self.limiter = Limiter(self.settings)
        self.store = store
        self.rng = rng or random.Random()
        self.inflight = {}
        self.auth_failed = False

    async def _check(self, tag, stop):
        s = self.settings
        if self.auth_failed:
            return Result(tag, FAILED, 0, 'authentication failed; restart after reauthentication')
        for attempt in range(1, s.retries + 2):
            try:
                async with self.limiter.slots:
                    await self.limiter.pace(stop)
                    profile = await interruptible(self.lookup(tag), stop, s.timeout)
                users = getattr(profile, 'profile_users', None)
                # A successful empty/malformed profile is inconclusive, not a hit.
                if users is None or not users:
                    return Result(tag, UNKNOWN, attempt, 'empty or unrecognised profile response')
                return Result(tag, TAKEN, attempt)
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code == 404:
                    return Result(tag, POTENTIAL, attempt, 'no profile found (HTTP 404)')
                if code in (401, 403):
                    self.auth_failed = True
                if code not in (429, 408, 500, 502, 503, 504):
                    return Result(tag, FAILED if code in (401, 403) else UNKNOWN,
                                  attempt, f'HTTP {code}')
                delay = retry_after(exc.response.headers.get('Retry-After'))
                backoff = min(60, s.backoff * 2 ** (attempt - 1)) + self.rng.uniform(0, 0.5)
                wait = max(delay or 0, backoff)
                if code == 429 or delay is not None:
                    await self.limiter.throttle(wait)
                reason = f'HTTP {code}; retry limit reached'
            except (httpx.TransportError, TimeoutError):
                wait = min(60, s.backoff * 2 ** (attempt - 1)) + self.rng.uniform(0, 0.5)
                reason = 'network failure or timeout; retry limit reached'
            except Exception as exc:
                if isinstance(exc, AuthenticationFailure):
                    self.auth_failed = True
                return Result(tag, FAILED, attempt, type(exc).__name__)
            if attempt <= s.retries:
                await pause(wait, stop)
        return Result(tag, FAILED, attempt, reason)

    async def check(self, tag, stop):
        key = tag.casefold()
        # A second command awaits existing work, rather than issuing a duplicate request.
        if key in self.inflight:
            return await interruptible(asyncio.shield(self.inflight[key]), stop,
                                       timeout=None)
        task = asyncio.create_task(self._check(tag, stop))
        self.inflight[key] = task
        try:
            result = await task
            if self.store:
                self.store.save(result)
            return result
        finally:
            self.inflight.pop(key, None)

    async def run(self, tags, stop=None, requested=None, progress=None, on_result=None):
        stop = stop or asyncio.Event()
        names = list(dict.fromkeys(t.strip().casefold() for t in tags if t.strip()))
        p = progress or Progress(len(names) if requested is None else requested, len(names))
        p.scheduled = len(names)
        results = {}
        iterator = iter(names)

        async def worker():
            while not stop.is_set():
                tag = next(iterator, None)
                if tag is None:
                    return
                result = await self.check(tag, stop)
                results[tag] = result
                p.attempted += 1
                p.checked += result.completed
                p.hits += result.status == POTENTIAL
                p.failed += result.status == FAILED
                p.unknown += result.status == UNKNOWN
                # Must be a quick synchronous callback (UI/notifications use a queue).
                if on_result:
                    on_result(result)

        tasks = [asyncio.create_task(worker()) for _ in range(min(len(names), self.settings.concurrency))]
        try:
            await asyncio.gather(*tasks)
            p.outcome = 'cancelled' if stop.is_set() else ('failed' if p.failed or p.unknown else 'finished')
        except asyncio.CancelledError:
            p.outcome = 'cancelled'
            stop.set()
        except Exception:
            p.outcome = 'failed'
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return results, p
