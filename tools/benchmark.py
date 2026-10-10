"""Measured scheduling benchmark using simulated latency only, no Xbox requests."""
import asyncio
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import CheckingEngine, Settings


async def benchmark():
    count, scale = 100, 0.002
    latency = 0.5 * scale
    async def lookup(tag):
        await asyncio.sleep(latency)
        return SimpleNamespace(profile_users=[object()])
    tags = [f'mock{i}' for i in range(count)]
    started = time.perf_counter()
    for tag in tags:
        await lookup(tag)
        await asyncio.sleep(10 * scale)  # Old hunt/bot: delay after every result.
    old = time.perf_counter() - started
    started = time.perf_counter()
    engine = CheckingEngine(lookup, Settings(concurrency=2, interval=1 * scale))
    results, p = await engine.run(tags)
    new = time.perf_counter() - started
    assert len(results) == count and p.checked == count
    return {'kind': 'SIMULATED: mocked network latency, scaled wall-clock scheduling',
            'tags': count, 'latency_seconds_before_scaling': 0.5, 'time_scale': scale,
            'old_delay_seconds_before_scaling': 10, 'new_interval_seconds_before_scaling': 1,
            'new_concurrency': 2, 'old_measured_seconds': round(old, 4),
            'new_measured_seconds': round(new, 4), 'measured_ratio': round(old / new, 2),
            'limitations': 'No Xbox or Discord calls. Scaling and event-loop overhead affect timings. Not a live throughput prediction.'}


if __name__ == '__main__':
    print(json.dumps(asyncio.run(benchmark()), indent=2))
