import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot
import hunter
from engine import CheckingEngine, ResultStore, Settings, POTENTIAL, TAKEN
from test_engine import PROFILE, error


class SilentNotifications:
    def __init__(self):
        self.tags = []
    async def start(self):
        pass
    def enqueue(self, result):
        if result.status == POTENTIAL:
            self.tags.append(result.tag)
    async def close(self):
        pass


def interaction():
    live = SimpleNamespace(edit=AsyncMock())
    i = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                        followup=SimpleNamespace(send=AsyncMock()),
                        channel=SimpleNamespace(send=AsyncMock(return_value=live)))
    return i, live


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(bot, 'state', bot.HuntState())
    monkeypatch.setattr(bot, 'notifications', SilentNotifications())


def test_count_schema_and_default():
    option = next(o for o in bot.hunt_cmd.to_dict(bot.tree)['options'] if o['name'] == 'count')
    assert option['min_value'] == 1 and option['max_value'] == 1000
    assert inspect.signature(bot.hunt_cmd.callback).parameters['count'].default == 30
    assert '1000' in option['description']


@pytest.mark.asyncio
async def test_sync_uses_current_tree(monkeypatch):
    sync = AsyncMock()
    monkeypatch.setattr(bot.tree, 'sync', sync)
    await bot.bot.setup_hook()
    sync.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [0, 1001])
async def test_invalid_count_does_not_start(count, monkeypatch):
    get = AsyncMock()
    monkeypatch.setattr(bot, 'runtime', SimpleNamespace(get=get))
    i, _ = interaction()
    await bot.hunt_cmd.callback(i, count=count)
    get.assert_not_awaited()
    assert not bot.state.running


@pytest.mark.asyncio
async def test_mock_1000_hunt_progress_and_complete_saving(tmp_path, monkeypatch):
    calls = []
    active = maximum = 0
    async def lookup(tag):
        nonlocal active, maximum
        calls.append(tag)
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        active -= 1
        if len(calls) % 5 == 0:
            raise error(404)
        return PROFILE
    engine = CheckingEngine(lookup, Settings(concurrency=2, interval=0), ResultStore(tmp_path))
    monkeypatch.setattr(bot, 'runtime', SimpleNamespace(get=AsyncMock(return_value=engine)))
    original_reporter = bot.progress_reporter
    async def reporter(live, progress, done, recent=()):
        await original_reporter(live, progress, done, interval=0.01, recent=recent)
    monkeypatch.setattr(bot, 'progress_reporter', reporter)
    i, live = interaction()
    messages = []
    async def edit(*, content):
        messages.append(content)
        await asyncio.sleep(0.003)
    live.edit.side_effect = edit
    await bot.hunt_cmd.callback(i, length=4, count=1000)
    assert len(calls) == len(set(calls)) == 1000
    assert maximum <= 2
    assert not bot.state.running
    assert len(engine.store.seen) == 1000
    assert len((tmp_path / 'checked.txt').read_text().splitlines()) == 1000
    records = [json.loads(line) for line in (tmp_path / 'results.jsonl').read_text().splitlines()]
    assert len(records) == 1000
    assert {r['tag'] for r in records} == set(calls)
    hits = [r for r in records if r['status'] == POTENTIAL]
    assert len((tmp_path / 'free.txt').read_text().splitlines()) == len(hits)
    assert all(r['status'] in (POTENTIAL, TAKEN) for r in records)
    assert 1 < len(messages) < 100
    assert any('checked' in m and 'candidate hits' in m and 'elapsed' in m for m in messages[:-1])
    assert 'finished: 1000/1000 checked' in messages[-1]
    assert '```ansi' in messages[-1]
    assert sum('(xbox)' in line for line in messages[-1].splitlines()) == 14
    assert any('(xbox)' in message for message in messages[:-1])
    assert all(len(message) < 2000 for message in messages)
    files = [call.kwargs['file'] for call in i.channel.send.await_args_list if 'file' in call.kwargs]
    i.followup.send.assert_not_awaited()  # No expiring interaction webhooks in long-hunt reporting.
    assert len(files) == 1
    assert len(files[0].fp.getvalue().decode().splitlines()) == 1000


@pytest.mark.asyncio
async def test_stop_during_backoff_clears_state(tmp_path, monkeypatch):
    throttled = asyncio.Event()
    async def lookup(tag):
        throttled.set()
        raise error(429, {'Retry-After': '3600'})
    engine = CheckingEngine(lookup, Settings(interval=0), ResultStore(tmp_path))
    monkeypatch.setattr(bot, 'runtime', SimpleNamespace(get=AsyncMock(return_value=engine)))
    i, live = interaction()
    hunt = asyncio.create_task(bot.hunt_cmd.callback(i, count=1000))
    await asyncio.wait_for(throttled.wait(), 1)
    stopper, _ = interaction()
    await bot.stop_cmd.callback(stopper)
    await asyncio.wait_for(hunt, 0.3)
    assert not bot.state.running
    assert 'cancelled' in live.edit.await_args.kwargs['content']
    assert not engine.store.seen


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['acknowledge', 'send', 'auth'])
async def test_startup_failures_reliably_clear_running(failure, monkeypatch):
    i, live = interaction()
    get = AsyncMock(side_effect=RuntimeError('do not expose this detail'))
    monkeypatch.setattr(bot, 'runtime', SimpleNamespace(get=get))
    if failure == 'acknowledge':
        i.response.send_message.side_effect = RuntimeError('mock')
    elif failure == 'send':
        i.channel.send.side_effect = RuntimeError('mock')
    await bot.hunt_cmd.callback(i, count=1000)
    assert not bot.state.running
    if failure == 'auth':
        assert 'failed' in live.edit.await_args.kwargs['content']
        assert 'do not expose' not in str(i.channel.send.await_args_list)


@pytest.mark.asyncio
async def test_check_uses_same_engine(monkeypatch, tmp_path):
    async def lookup(tag):
        return PROFILE
    engine = CheckingEngine(lookup, Settings(interval=0), ResultStore(tmp_path))
    get = AsyncMock(return_value=engine)
    monkeypatch.setattr(bot, 'runtime', SimpleNamespace(get=get))
    i, _ = interaction()
    await bot.check_cmd.callback(i, 'abcd, ABCD, efgh')
    get.assert_awaited_once()
    assert engine.store.seen == {'abcd', 'efgh'}


@pytest.mark.parametrize('flag', ['--browser', '--assist', '--open'])
def test_removed_cli_flags(flag):
    with pytest.raises(SystemExit):
        hunter.make_parser().parse_args(['hunt', flag])


def test_live_tags_distinguish_all_classifications():
    from engine import FAILED, UNKNOWN, Progress, Result
    p = Progress(4, 4)
    text = bot.render(p, [Result('abcd', TAKEN), Result('efgh', POTENTIAL),
                          Result('ijkl', UNKNOWN), Result('mnop', FAILED)])
    assert '\x1b[31m[-] Taken: abcd (xbox)' in text
    assert '\x1b[32m[+] potentially available — manual verification required: efgh (xbox)' in text
    assert '[!] unknown: ijkl' in text and '[!] failed: mnop' in text
    assert text.startswith('```ansi') and len(text) < 2000
