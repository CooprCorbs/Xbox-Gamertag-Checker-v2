import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from xbox.webapi.authentication.manager import AuthenticationManager

import hunter


@pytest.mark.asyncio
async def test_refresh_lock_protects_concurrent_calls(monkeypatch):
    active = maximum = 0
    async def refresh(self):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        active -= 1
    monkeypatch.setattr(AuthenticationManager, 'refresh_tokens', refresh)
    manager = hunter.LockedAuthenticationManager.__new__(hunter.LockedAuthenticationManager)
    manager.refresh_lock = asyncio.Lock()
    await asyncio.gather(*(manager.refresh_tokens() for _ in range(8)))
    assert maximum == 1


@pytest.mark.asyncio
async def test_client_adapter_copies_shared_headers_and_pools_connections():
    headers = {'x-xbl-contract-version': '3'}
    recorded = []
    active = maximum = 0
    async def transport(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.003)
        recorded.append(dict(request.headers))
        active -= 1
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as raw:
        auth = SimpleNamespace(refresh_tokens=AsyncMock(), session=raw,
                               xsts_token=SimpleNamespace(authorization_header_value='mock-test-header'))
        session = hunter.ConcurrentSession(auth)
        await asyncio.gather(*(session.get('https://example.invalid', headers=headers) for _ in range(2)))
    assert headers == {'x-xbl-contract-version': '3'}
    assert maximum == 2
    assert len({r['ms-cv'] for r in recorded}) == 2
    assert all(r['authorization'] == 'mock-test-header' for r in recorded)


def test_process_guard_refuses_competing_checker():
    with hunter.CredentialLease():
        with pytest.raises(RuntimeError):
            with hunter.CredentialLease():
                pass
    with hunter.CredentialLease():
        pass


@pytest.mark.asyncio
async def test_shared_runtime_initializes_once_and_closes(monkeypatch):
    enters = exits = 0
    engine = object()
    class Context:
        async def __aenter__(self):
            nonlocal enters
            enters += 1
            await asyncio.sleep(0.001)
            return engine
        async def __aexit__(self, *args):
            nonlocal exits
            exits += 1
    monkeypatch.setattr(hunter, 'open_engine', lambda **kwargs: Context())
    runtime = hunter.SharedRuntime()
    a, b = await asyncio.gather(runtime.get(), runtime.get())
    assert a is b is engine and enters == 1
    await runtime.close()
    assert exits == 1 and runtime.engine is None


def test_windows_process_guard_branch(monkeypatch):
    import os
    import sys
    locked = set()
    def locking(fd, mode, size):
        assert size == 1
        key = os.fstat(fd).st_ino
        if mode == 1:
            if key in locked:
                raise OSError('mock busy Windows lock')
            locked.add(key)
        else:
            locked.remove(key)
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(locking=locking, LK_NBLCK=1, LK_UNLCK=2))
    monkeypatch.setattr(hunter, 'os', SimpleNamespace(name='nt', getenv=os.getenv))
    with hunter.CredentialLease():
        with pytest.raises(RuntimeError):
            with hunter.CredentialLease():
                pass
    assert not locked


@pytest.mark.asyncio
async def test_notifications_do_not_block_checking(monkeypatch):
    from engine import CheckingEngine, Settings
    from test_engine import PROFILE, error
    finished = asyncio.Event()
    async def delayed_notify(tag, client):
        await asyncio.sleep(0.2)
        finished.set()
    monkeypatch.setattr(hunter, 'notify', delayed_notify)
    notifications = hunter.Notifications()
    await notifications.start()
    async def lookup(tag):
        if tag == 'hit':
            raise error(404)
        return PROFILE
    engine = CheckingEngine(lookup, Settings(interval=0))
    try:
        _, p = await asyncio.wait_for(engine.run(['hit', 'abcd', 'efgh'], on_result=notifications.enqueue), 0.1)
        assert p.checked == 3 and not finished.is_set()
    finally:
        await notifications.close()
    assert finished.is_set()


@pytest.mark.asyncio
async def test_auth_endpoint_404_is_not_a_candidate(monkeypatch):
    from engine import AuthenticationFailure, CheckingEngine, FAILED, Settings
    from test_engine import error
    async def broken_refresh(self):
        raise error(404)
    monkeypatch.setattr(AuthenticationManager, 'refresh_tokens', broken_refresh)
    manager = hunter.LockedAuthenticationManager.__new__(hunter.LockedAuthenticationManager)
    manager.refresh_lock = asyncio.Lock()
    async def lookup(tag):
        await manager.refresh_tokens()
    engine = CheckingEngine(lookup, Settings(interval=0))
    results, p = await engine.run(['abcd'])
    assert results['abcd'].status == FAILED
    assert results['abcd'].reason == 'AuthenticationFailure'
    assert p.hits == p.checked == 0
