"""Discord /hunt length:4 count:1000, /check, and prompt /stop."""
import asyncio
import io
import os
from collections import deque
from dataclasses import dataclass, field

import discord
from discord import app_commands

from engine import POTENTIAL, TAKEN, Progress, candidates, interruptible
from hunter import Notifications, SharedRuntime, load_env


@dataclass
class HuntState:
    running: bool = False
    stop: asyncio.Event = field(default_factory=asyncio.Event)


state = HuntState()
runtime = SharedRuntime()
notifications = Notifications()


class HunterBot(discord.Client):
    async def setup_hook(self):
        # Global schema includes count max_value=1000; sync once on every launch.
        await tree.sync()

    async def close(self):
        state.stop.set()
        await notifications.close()
        await runtime.close()
        await super().close()


bot = HunterBot(intents=discord.Intents.default())
tree = app_commands.CommandTree(bot)


def render(progress, recent=()):
    summary = progress.summary()
    note = 'Candidate hits are potentially available — manual verification required.'
    if not recent:
        return summary + '\n' + note
    lines = []
    for result in recent:
        color = '32' if result.status == POTENTIAL else ('31' if result.status == TAKEN else '33')
        marker = '[+]' if result.status == POTENTIAL else ('[-]' if result.status == TAKEN else '[!]')
        label = 'Taken' if result.status == TAKEN else result.status
        lines.append(f'\x1b[{color}m{marker} {label}: {result.tag} (xbox)\x1b[0m')
    # A rolling window keeps updates readable and within Discord's message limit.
    while lines and len('\n'.join(lines)) + len(summary) + len(note) + 40 > 1900:
        lines.pop(0)
    return '```ansi\n' + '\n'.join(lines) + f'\n\x1b[36m[i] {summary}\x1b[0m\n```\n' + note


async def progress_reporter(live, progress, done, interval=3.0, recent=()):
    """Independent throttled edits; a slow Discord request never holds up checking."""
    while not done.is_set():
        try:
            await asyncio.wait_for(done.wait(), interval)
        except asyncio.TimeoutError:
            try:
                await asyncio.wait_for(live.edit(content=render(progress, recent)), 10)
            except Exception:
                pass


async def send_results(interaction, results, progress):
    lines = [f'{r.tag}: {r.status}' + (f' ({r.reason})' if r.reason else '')
             for r in results.values()]
    text = render(progress)
    if len(text + '\n' + '\n'.join(lines)) <= 1900:
        await interaction.channel.send(text + ('\n' + '\n'.join(lines) if lines else ''),
                                        allowed_mentions=discord.AllowedMentions.none())
    else:
        data = ('\n'.join(lines) + '\n').encode('utf-8')
        await interaction.channel.send(text, file=discord.File(io.BytesIO(data), filename='hunt-results.txt'),
                                        allowed_mentions=discord.AllowedMentions.none())


@tree.command(name='hunt', description='Check up to 1000 random gamertags; candidate hits need manual verification')
@app_commands.describe(length='Characters per tag (3 to 8)',
                       count='How many to check (1 to 1000; default 30)',
                       letters_only='Letters only, no numbers')
async def hunt_cmd(interaction: discord.Interaction,
                   length: app_commands.Range[int, 3, 8] = 4,
                   count: app_commands.Range[int, 1, 1000] = 30,
                   letters_only: bool = False):
    if state.running:
        await interaction.response.send_message('A hunt is running. Use /stop to cancel.', ephemeral=True)
        return
    # Also validate direct calls so the advertised boundary and runtime agree.
    if not 1 <= count <= 1000 or not 3 <= length <= 8:
        await interaction.response.send_message('Use length 3..8 and count 1..1000.', ephemeral=True)
        return
    state.running = True
    state.stop = asyncio.Event()
    stop = state.stop
    progress = Progress(count, 0)
    results = {}
    recent = deque(maxlen=14)
    live = None
    reporter = None
    done = asyncio.Event()
    try:
        await interruptible(interaction.response.send_message(
            'Starting hunt; progress and results will be posted in this channel.'), stop, 10)
        live = await interruptible(interaction.channel.send(render(progress)), stop, 10)
        reporter = asyncio.create_task(progress_reporter(live, progress, done, recent=recent))
        engine = await interruptible(runtime.get(stop), stop, 30)
        tags = candidates(length, count, letters_only, engine.store.seen)
        progress.scheduled = len(tags)
        await notifications.start()
        def record_result(result):
            recent.append(result)
            notifications.enqueue(result)
        results, progress = await engine.run(tags, stop, progress=progress,
                                              on_result=record_result)
    except asyncio.CancelledError:
        progress.outcome = 'cancelled'
        stop.set()
    except Exception:
        # Never send exception text which might contain a token or webhook URL.
        progress.outcome = 'failed'
    finally:
        done.set()
        if reporter:
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)
        # Release state before final message delivery.
        state.running = False
        if live:
            try:
                await asyncio.wait_for(live.edit(content=render(progress, recent)), 10)
            except Exception:
                pass
        try:
            await asyncio.wait_for(send_results(interaction, results, progress), 10)
            if progress.scheduled < count and progress.outcome == 'finished':
                await asyncio.wait_for(interaction.channel.send('Unchecked candidate namespace exhausted.'), 10)
        except Exception:
            pass


@tree.command(name='stop', description='Cancel the running hunt, including request and backoff waits')
async def stop_cmd(interaction: discord.Interaction):
    if state.running:
        state.stop.set()
        await interaction.response.send_message('Cancelling; completed results are preserved.')
    else:
        await interaction.response.send_message('No hunt is running.', ephemeral=True)


@tree.command(name='check', description='Check up to 10 specific gamertags; shares the hunt rate limiter')
async def check_cmd(interaction: discord.Interaction, tags: str):
    await interaction.response.send_message('Checking profiles; results will be posted in this channel.')
    names = list(dict.fromkeys(t.strip().casefold() for t in tags.split(',') if t.strip()))
    if not names or len(names) > 10 or any(len(t) > 16 for t in names):
        await interaction.channel.send('Provide 1..10 tags, each at most 16 characters.')
        return
    progress = Progress(len(names), len(names))
    results = {}
    try:
        engine = await runtime.get()
        results, progress = await engine.run(names, progress=progress)
    except Exception:
        progress.outcome = 'failed'
    await send_results(interaction, results, progress)


@bot.event
async def on_ready():
    print('Bot ready; global command schema synced during startup.')


if __name__ == '__main__':
    load_env()
    bot.run(os.environ['DISCORD_TOKEN'])
