"""CLI and authenticated runtime for profile checking. Never opens a browser."""
import argparse
import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
from contextlib import asynccontextmanager

import httpx
from xbox.webapi.api.client import XboxLiveClient
from xbox.webapi.api.client import Session
from xbox.webapi.authentication.manager import AuthenticationManager
from xbox.webapi.authentication.models import OAuth2TokenResponse
from xbox.webapi.common.signed_session import SignedSession

from engine import AuthenticationFailure, CheckingEngine, POTENTIAL, Progress, ResultStore, Settings, candidates, interruptible

# Public desktop-app identifier used by xbox-webapi 2.1.0; no secret required.
CLIENT_ID = '388ea51c-0b25-4029-aae2-17df49d23905'
HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / 'checked.txt'
FREE_FILE = HERE / 'free.txt'


def load_env(path=None):
    """Runtime only: never load secrets during imports, tests, or --help."""
    path = Path(path) if path else HERE / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class LockedAuthenticationManager(AuthenticationManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.refresh_lock = asyncio.Lock()

    async def refresh_tokens(self):
        async with self.refresh_lock:
            try:
                return await super().refresh_tokens()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (408, 429, 500, 502, 503, 504):
                    raise AuthenticationFailure('Authentication endpoint rejected the request') from None
                raise


class ConcurrentSession(Session):
    """xbox-webapi 2.1.0 safety adapter.

    Session mutates the provider's class-level headers and refreshes auth on each
    request. Copy headers before awaiting; refresh is locked on the manager. Its
    correlation vector increments synchronously on the single asyncio loop.
    HTTPX AsyncClient pools connections and supports concurrent async requests.
    """
    async def request(self, method, url, **kwargs):
        kwargs['headers'] = dict(kwargs.get('headers') or {})
        return await super().request(method, url, **kwargs)


async def get_client(session):
    from xbox.webapi.scripts import CLIENT_SECRET, TOKENS_FILE
    auth = LockedAuthenticationManager(session,
        os.getenv('XBOX_CLIENT_ID') or CLIENT_ID,
        os.getenv('XBOX_CLIENT_SECRET') or CLIENT_SECRET, '')
    with open(TOKENS_FILE, encoding='utf-8') as f:
        auth.oauth = OAuth2TokenResponse.model_validate_json(f.read())
    await auth.refresh_tokens()
    with open(TOKENS_FILE, 'w', encoding='utf-8') as f:
        f.write(auth.oauth.model_dump_json())
    client = XboxLiveClient(auth)
    client.session = ConcurrentSession(auth)
    return client


class CredentialLease:
    """Nonblocking Windows/POSIX process guard for this OAuth application.

    Conservatively excludes even different accounts using the same application.
    No credential contents are read, written, or logged by the guard.
    """
    def __enter__(self):
        identity = os.getenv('XBOX_CLIENT_ID') or CLIENT_ID
        digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.file = open(Path(tempfile.gettempdir()) / f'gamertag-hunter-{digest}.lock', 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b'0'); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Another checker is running for this Xbox application. Stop it first.') from None
        return self

    def __exit__(self, *args):
        if os.name == 'nt':
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


def settings_from_env():
    return Settings(concurrency=int(os.getenv('HUNT_CONCURRENCY', '2')),
                    interval=float(os.getenv('HUNT_DELAY', '1')),
                    timeout=float(os.getenv('HUNT_TIMEOUT', '20')))


@asynccontextmanager
async def open_engine(settings=None, stop=None):
    settings = settings or settings_from_env()
    stop = stop or asyncio.Event()
    with CredentialLease():
        async with SignedSession() as session:
            session.timeout = httpx.Timeout(settings.timeout)
            client = await interruptible(get_client(session), stop, settings.timeout)
            async def lookup(tag):
                return await client.profile.get_profile_by_gamertag(tag, timeout=settings.timeout)
            yield CheckingEngine(lookup, settings, ResultStore(HERE))


class SharedRuntime:
    """Bot lifetime: one authentication, pool, engine, limiter, and result writer."""
    def __init__(self):
        self.engine = None
        self.context = None
        self.lock = asyncio.Lock()

    async def get(self, stop=None):
        stop = stop or asyncio.Event()
        async with self.lock:
            if self.engine is None:
                context = open_engine(stop=stop)
                self.engine = await context.__aenter__()
                self.context = context
            return self.engine

    async def close(self):
        async with self.lock:
            if self.context:
                await self.context.__aexit__(None, None, None)
                self.context = self.engine = None


async def check_many(tags, delay=None, concurrency=None):
    settings = settings_from_env()
    if delay is not None or concurrency is not None:
        settings = Settings(concurrency=concurrency or settings.concurrency,
                            interval=settings.interval if delay is None else delay,
                            timeout=settings.timeout)
    async with open_engine(settings) as engine:
        results, _ = await engine.run(tags)
        return results


def build_ping():
    parts = []
    for item in (os.getenv('DISCORD_PING') or '').split(','):
        item = item.strip()
        if item.lower() in ('everyone', 'here'):
            parts.append('@' + item.lower())
        elif item.startswith('&') and item[1:].isdigit():
            parts.append(f'<@&{item[1:]}>' )
        elif item.isdigit():
            parts.append(f'<@{item}>')
    return ' '.join(parts)


async def notify(tag, client):
    msg = f'{tag}: {POTENTIAL}'
    targets = []
    if os.getenv('DISCORD_WEBHOOK'):
        targets.append((os.environ['DISCORD_WEBHOOK'], {'json': {
            'content': f'{build_ping()} {msg}'.strip(),
            'allowed_mentions': {'parse': ['users', 'roles', 'everyone']}}}))
    if os.getenv('NTFY_TOPIC'):
        targets.append((f'https://ntfy.sh/{os.environ["NTFY_TOPIC"]}', {
            'content': msg.encode(), 'headers': {'Title': 'Gamertag hunter'}}))
    for url, kwargs in targets:
        try:
            response = await client.post(url, **kwargs)
            response.raise_for_status()
        except httpx.HTTPError:
            # URLs can contain credentials, so never print exception details.
            print('Notification failed; the candidate is saved locally.')


class Notifications:
    """An independent consumer keeps network notifications off the checking path."""
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=1000)
        self.task = None

    def enqueue(self, result):
        if result.status == POTENTIAL:
            try:
                self.queue.put_nowait(result.tag)
            except asyncio.QueueFull:
                print('Notification queue full; candidate preserved in free.txt.')

    async def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.consume())

    async def consume(self):
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                tag = await self.queue.get()
                try:
                    await notify(tag, client)
                finally:
                    self.queue.task_done()

    async def close(self):
        if self.task:
            try:
                await asyncio.wait_for(self.queue.join(), 15)
            except asyncio.TimeoutError:
                print('Pending notifications timed out; candidates remain saved locally.')
            finally:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)


async def run_cli(args):
    settings = Settings(args.concurrency, args.delay, args.timeout)
    stop = asyncio.Event()
    notifications = Notifications()
    if args.notify:
        await notifications.start()
    def report(result):
        if result.status == POTENTIAL or not args.quiet:
            print(f'{result.tag}: {result.status}')
        if args.notify:
            notifications.enqueue(result)
    try:
        async with open_engine(settings, stop) as engine:
            while True:
                if args.cmd == 'check':
                    tags = Path(args.file).read_text(encoding='utf-8').splitlines()
                    requested = len(set(t.strip().casefold() for t in tags if t.strip()))
                else:
                    requested = args.count
                    tags = candidates(args.length, requested, args.letters_only, engine.store.seen)
                if not tags:
                    print('No unchecked candidates remain.'); return
                progress = Progress(requested, len(tags))
                async def ticker():
                    while True:
                        await asyncio.sleep(5)
                        print(progress.summary())
                ui = asyncio.create_task(ticker())
                try:
                    _, progress = await engine.run(tags, stop, progress=progress, on_result=report)
                finally:
                    ui.cancel(); await asyncio.gather(ui, return_exceptions=True)
                print(progress.summary())
                if len(tags) < requested:
                    print('Candidate namespace exhausted; fewer candidates were scheduled.')
                if not args.forever or progress.outcome != 'finished' or args.cmd == 'check':
                    return
    finally:
        await notifications.close()


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    for command in ('check', 'hunt'):
        p = sub.add_parser(command)
        p.add_argument('--delay', type=float, default=1.0, help='minimum seconds between request starts; increase to slow down')
        p.add_argument('--concurrency', type=int, default=2, help='bounded workers, 1..8 (default 2)')
        p.add_argument('--timeout', type=float, default=20)
        p.add_argument('--notify', action='store_true')
        p.add_argument('--quiet', action='store_true')
        p.set_defaults(forever=False)
        if command == 'check':
            p.add_argument('file')
        else:
            p.add_argument('--length', type=int, choices=range(1, 9), default=4)
            p.add_argument('--count', type=int, default=500)
            p.add_argument('--letters-only', action='store_true')
            p.add_argument('--forever', action='store_true', help='repeat successful batches until cancelled or exhausted')
    return parser


def main():
    parser = make_parser()
    args = parser.parse_args()
    if args.cmd == 'hunt' and args.count < 1:
        parser.error('count must be positive')
    load_env()
    try:
        asyncio.run(run_cli(args))
    except KeyboardInterrupt:
        print('Cancelled. Completed results were preserved.')
    except Exception as exc:
        print(f'Failed: {type(exc).__name__}. Check authentication/setup. Completed results were preserved.')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
