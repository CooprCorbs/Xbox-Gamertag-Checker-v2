import asyncio
import random
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from engine import (CheckingEngine, FAILED, POTENTIAL, TAKEN, UNKNOWN, Progress,
                    Result, ResultStore, Settings, candidates, retry_after)

PROFILE = SimpleNamespace(profile_users=[object()])


class ZeroJitter:
    def uniform(self, *args):
        return 0


def error(code, headers=None):
    req = httpx.Request('GET', 'https://example.invalid/profile')
    return httpx.HTTPStatusError('mock', request=req,
                                response=httpx.Response(code, headers=headers, request=req))


def test_unique_1000_and_case_insensitive_seen():
    tags = candidates(4, 1000, seen=['AAAA', 'abcd'], rng=random.Random(4))
    assert len(tags) == len(set(tags)) == 1000
    assert not {'aaaa', 'abcd'} & set(tags)
    assert all(t[0].isalpha() and len(t) == 4 for t in tags)


def test_exhausted_and_almost_exhausted_namespace():
    tags = candidates(1, 1000, rng=random.Random(1))
    assert len(tags) == 26
    assert candidates(1, 1000, seen=tags) == []
    assert candidates(1, 1000, seen=tags[:-1]) == [tags[-1]]
    all_tags = candidates(2, 1000, letters_only=True)
    assert len(all_tags) == 26 ** 2
    assert set(candidates(2, 1000, True, all_tags[:-2])) == set(all_tags[-2:])


@pytest.mark.parametrize('value,expected', [('12', 12), ('0', 0), ('-1', 0),
                                         ('garbage', None), ('nan', None), (None, None)])
def test_retry_after_seconds(value, expected):
    assert retry_after(value, now=1000) == expected


def test_retry_after_http_date():
    dt = datetime.fromtimestamp(1030, timezone.utc)
    assert retry_after(format_datetime(dt, usegmt=True), now=1000) == 30
    assert retry_after(format_datetime(dt, usegmt=True), now=1040) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('response,status', [(PROFILE, TAKEN), (None, UNKNOWN),
    (SimpleNamespace(profile_users=[]), UNKNOWN), (404, POTENTIAL), (401, FAILED),
    (403, FAILED), (400, UNKNOWN), (429, FAILED), (503, FAILED), ('network', FAILED)])
async def test_classification(response, status, tmp_path):
    async def lookup(tag):
        if isinstance(response, int):
            raise error(response)
        if response == 'network':
            raise httpx.ConnectError('mock')
        return response
    store = ResultStore(tmp_path)
    engine = CheckingEngine(lookup, Settings(interval=0, retries=0), store, ZeroJitter())
    results, progress = await engine.run(['abcd'])
    assert results['abcd'].status == status
    assert ('abcd' in store.seen) == (status in (TAKEN, POTENTIAL))
    assert ('abcd' in store.hits) == (status == POTENTIAL)
    assert progress.checked == (status in (TAKEN, POTENTIAL))


@pytest.mark.asyncio
async def test_retry_then_success_and_exhaustion(tmp_path):
    calls = 0
    async def lookup(tag):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise error(503)
        return PROFILE
    engine = CheckingEngine(lookup, Settings(interval=0, retries=2, backoff=0),
                            ResultStore(tmp_path), ZeroJitter())
    results, _ = await engine.run(['abcd'])
    assert results['abcd'].attempts == calls == 3
    assert results['abcd'].status == TAKEN
    async def down(tag):
        raise httpx.ReadTimeout('mock')
    engine.lookup = down
    results, _ = await engine.run(['efgh'])
    assert results['efgh'].attempts == 3
    assert results['efgh'].status == FAILED
    assert 'efgh' not in engine.store.seen


@pytest.mark.asyncio
async def test_cancel_backoff_and_preserve_completed(tmp_path):
    throttled = asyncio.Event()
    async def lookup(tag):
        if tag == 'done':
            return PROFILE
        throttled.set()
        raise error(429, {'Retry-After': '3600'})
    store = ResultStore(tmp_path)
    engine = CheckingEngine(lookup, Settings(concurrency=1, interval=0), store)
    stop = asyncio.Event()
    task = asyncio.create_task(engine.run(['done', 'wait', 'later'], stop))
    await asyncio.wait_for(throttled.wait(), 1)
    await asyncio.sleep(0)
    stop.set()
    results, p = await asyncio.wait_for(task, 0.2)
    assert p.outcome == 'cancelled'
    assert set(results) == {'done'}
    assert store.seen == {'done'}
    assert not engine.inflight


@pytest.mark.asyncio
async def test_stop_interrupts_inflight_request():
    started = asyncio.Event()
    cancelled = asyncio.Event()
    async def lookup(tag):
        started.set()
        try:
            await asyncio.sleep(100)
        finally:
            cancelled.set()
    engine = CheckingEngine(lookup, Settings(interval=0))
    stop = asyncio.Event()
    task = asyncio.create_task(engine.run(['abcd'], stop))
    await started.wait()
    stop.set()
    _, p = await asyncio.wait_for(task, 0.2)
    assert p.outcome == 'cancelled' and cancelled.is_set()


@pytest.mark.asyncio
async def test_request_timeout_is_failed_not_seen(tmp_path):
    async def lookup(tag):
        await asyncio.sleep(1)
    engine = CheckingEngine(lookup, Settings(interval=0, timeout=0.01, retries=0), ResultStore(tmp_path))
    results, _ = await engine.run(['abcd'])
    assert results['abcd'].status == FAILED
    assert not engine.store.seen


@pytest.mark.asyncio
async def test_shared_commands_bound_concurrency_and_deduplicate():
    active = maximum = calls = 0
    ready = asyncio.Event()
    async def lookup(tag):
        nonlocal active, maximum, calls
        calls += 1
        active += 1
        maximum = max(maximum, active)
        ready.set()
        await asyncio.sleep(0.02)
        active -= 1
        return PROFILE
    engine = CheckingEngine(lookup, Settings(concurrency=2, interval=0))
    first = asyncio.create_task(engine.run(['same', 'ABCD', 'abcd']))
    await ready.wait()
    second = asyncio.create_task(engine.run(['same', 'efgh']))
    a, b = await asyncio.gather(first, second)
    assert calls == 3
    assert maximum <= 2
    assert a[0]['same'] == b[0]['same']


@pytest.mark.asyncio
async def test_shared_throttle_honours_retry_after_and_slows_workers():
    loop = asyncio.get_running_loop()
    starts = []
    calls = 0
    hit = asyncio.Event()
    async def lookup(tag):
        nonlocal calls
        calls += 1
        starts.append(loop.time())
        if calls == 1:
            hit.set()
            raise error(429, {'Retry-After': '0.05'})
        return PROFILE
    engine = CheckingEngine(lookup, Settings(concurrency=1, interval=0, retries=1, backoff=0), rng=ZeroJitter())
    a = asyncio.create_task(engine.run(['abcd']))
    await hit.wait()
    await asyncio.sleep(0)
    b = asyncio.create_task(engine.run(['efgh']))
    await asyncio.wait_for(asyncio.gather(a, b), 3)
    assert starts[1] - starts[0] >= 0.045
    assert starts[2] - starts[1] >= 0.95
    assert engine.limiter.interval >= 1


@pytest.mark.asyncio
async def test_failed_attempt_can_be_checked_in_later_hunt(tmp_path):
    calls = 0
    async def lookup(tag):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError('mock')
        return PROFILE
    engine = CheckingEngine(lookup, Settings(interval=0, retries=0), ResultStore(tmp_path))
    await engine.run(['abcd'])
    assert 'abcd' not in engine.store.seen
    await engine.run(['abcd'])
    assert engine.store.seen == {'abcd'}
    assert len((tmp_path / 'results.jsonl').read_text().splitlines()) == 2


def test_result_store_preserves_old_files_and_deduplicates(tmp_path):
    (tmp_path / 'checked.txt').write_text('OLD1')
    (tmp_path / 'free.txt').write_text('OLD2')
    store = ResultStore(tmp_path)
    store.save(Result('new1', POTENTIAL))
    store.save(Result('NEW1', POTENTIAL))
    store.save(Result('fail', FAILED))
    assert (tmp_path / 'checked.txt').read_text().splitlines() == ['OLD1', 'new1']
    assert (tmp_path / 'free.txt').read_text().splitlines() == ['OLD2', 'new1']
    assert 'fail' not in store.seen


@pytest.mark.asyncio
async def test_save_failure_returns_failed_hunt_with_partial_results(tmp_path):
    class BrokenStore(ResultStore):
        def save(self, result):
            if result.tag == 'fail':
                raise OSError('mock disk error')
            super().save(result)
    async def lookup(tag):
        return PROFILE
    engine = CheckingEngine(lookup, Settings(concurrency=1, interval=0), BrokenStore(tmp_path))
    results, p = await engine.run(['done', 'fail', 'later'])
    assert set(results) == {'done'} and p.outcome == 'failed'
    assert engine.store.seen == {'done'}


@pytest.mark.asyncio
async def test_http_date_retry_after_reaches_shared_throttle(monkeypatch):
    calls = 0
    waits = []
    future = format_datetime(datetime.fromtimestamp(__import__('time').time() + 30, timezone.utc), usegmt=True)
    async def lookup(tag):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error(503, {'Retry-After': future})
        return PROFILE
    engine = CheckingEngine(lookup, Settings(interval=0, backoff=0), rng=ZeroJitter())
    async def throttle(wait):
        waits.append(wait)
    async def no_wait(seconds, stop):
        assert not stop.is_set()
    monkeypatch.setattr(engine.limiter, 'throttle', throttle)
    monkeypatch.setattr('engine.pause', no_wait)
    results, _ = await engine.run(['abcd'])
    assert 28 <= waits[0] <= 30
    assert results['abcd'].status == TAKEN and calls == 2


@pytest.mark.asyncio
async def test_auth_failure_stops_further_requests():
    calls = 0
    async def lookup(tag):
        nonlocal calls
        calls += 1
        raise error(401)
    engine = CheckingEngine(lookup, Settings(concurrency=1, interval=0))
    results, p = await engine.run(['abcd', 'efgh', 'ijkl'])
    assert calls == 1
    assert all(r.status == FAILED for r in results.values())
    assert p.checked == p.hits == 0 and p.outcome == 'failed'


@pytest.mark.asyncio
async def test_no_delay_after_final_check():
    async def lookup(tag):
        return PROFILE
    engine = CheckingEngine(lookup, Settings(interval=3600))
    results, _ = await asyncio.wait_for(engine.run(['abcd']), 0.2)
    assert results['abcd'].status == TAKEN
