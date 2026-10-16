"""`mt live`: tap → live STT → fixer → panel, one session directory per call."""
import asyncio
import json
import os
import signal
import time

from aiohttp import web

from . import tap as tapmod
from .core.cards import ListCards
from .fixer import Fixer
from .gate import VoiceGate
from .stt import LiveSTT, LocalWhisper
from .vocab import DATA, Vocab

HERE = os.path.dirname(os.path.abspath(__file__))
SESSIONS = os.path.join(DATA, 'sessions')


class Live:
    def __init__(self, args):
        self.args = args
        self.loop = None
        self.events = []                 # full history, replayed to a panel that (re)connects
        self.clients = set()
        self.status = {'type': 'status', 'text': 'starting'}
        self.warns = []
        ts = time.strftime('%Y%m%d-%H%M%S')
        self.dir = args.out or os.path.join(SESSIONS, ts + ('-' + args.name if args.name else ''))
        os.makedirs(self.dir, exist_ok=True)
        self.log_f = open(os.path.join(self.dir, 'live.jsonl'), 'a', buffering=1)
        self.gate = VoiceGate() if args.voice_gate else None
        self.fixer = None
        self.cards = None                # word-list term cards (set once the vocab is loaded)

    # ---- events ----
    def emit(self, e):
        """Thread-safe: STT/tap threads call this too."""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._emit, e)

    def _emit(self, e):
        e.setdefault('t', round(time.time(), 3))
        self.log_f.write(json.dumps(e) + '\n')
        if e['type'] == 'status':
            self.status = e
        elif e['type'] == 'warn':
            self.warns = e['items']
        elif e['type'] != 'partial':
            self.events.append(e)
        if e['type'] == 'commit' and self.fixer:
            self.fixer.submit(e['id'], e['text'])
        if self.args.verbose and e['type'] in ('commit', 'fix', 'card', 'status', 'warn'):
            print(json.dumps(e), flush=True)
        data = 'data: ' + json.dumps(e) + '\n\n'
        for q in list(self.clients):
            q.put_nowait(data)
        if self.cards is not None and e['type'] in ('commit', 'fix'):
            # word-list cards come from final text only: tiny.en partials invent list terms on filler
            for c in self.cards.scan(e['text']):
                self._emit(dict(c, type='card'))

    # ---- web ----
    async def index(self, request):
        return web.FileResponse(os.path.join(HERE, 'panel.html'))

    async def sse(self, request):
        resp = web.StreamResponse(headers={'Content-Type': 'text/event-stream',
                                           'Cache-Control': 'no-cache'})
        await resp.prepare(request)
        q = asyncio.Queue()
        for e in [{'type': 'reset'}, self.status, {'type': 'warn', 'items': self.warns}] + self.events:
            q.put_nowait('data: ' + json.dumps(e) + '\n\n')
        self.clients.add(q)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    data = ': ping\n\n'
                if data is None:
                    break
                await resp.write(data.encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.clients.discard(q)
        return resp

    # ---- main ----
    async def run(self, stop_after=None, on_ready=None):
        a = self.args
        self.loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(sig, stop.set)

        vocab = Vocab.load(a.context)
        them_dev, me_dev = tapmod.default_devices()
        them_dev = a.sink + '.monitor' if a.sink else them_dev
        me_dev = a.source or me_dev

        print(f'[mt] loading {a.model} …', flush=True)
        engine = LocalWhisper(a.model, threads=a.threads, prompt=vocab.prompt())
        # no word-list prompt for tiny.en partials: with it they invent list terms on filler
        # ("Endpoint Manager, MECM" in the 10-12 widget test)
        pengine = (LocalWhisper(a.partial_model, threads=2, prompt=None)
                   if a.partial_model != 'none' else None)
        self.fixer = Fixer(vocab, self.emit, enabled=not a.no_fixer, mode=a.fixer_mode)
        self.stt = LiveSTT(engine, self.emit, partial_engine=pengine)
        self.cards = ListCards(vocab)
        self.fixer.carded = self.cards.carded       # one "already carded" set for list + model cards

        meta = {'started': time.time(), 'them_device': them_dev, 'me_device': me_dev,
                'engine': f'local {a.model} (+{a.partial_model} partials)', 'context': a.context, 'name': a.name,
                'voice_gate': None, 'vocab_terms': len(vocab.terms)}
        them = tapmod.Stream('them', them_dev, self.dir, on_chunk=self.stt.feed)
        streams = [them]
        if not a.no_me:
            streams.append(tapmod.Stream('me', me_dev, self.dir))
        for s in streams:
            s.start()

        app = web.Application()
        app.router.add_get('/', self.index)
        app.router.add_get('/events', self.sse)
        runner = web.AppRunner(app)
        await runner.setup()
        host = '0.0.0.0' if a.lan else '127.0.0.1'
        # shutdown_timeout: an open panel/widget SSE stream otherwise holds the exit for 60 s
        await web.TCPSite(runner, host, a.port, shutdown_timeout=2).start()
        fixer_task = asyncio.create_task(self.fixer.run())
        print(f'[mt] panel http://{host}:{a.port}/  session {self.dir}', flush=True)
        # stop the voice service only once everything above is up: a failed model load or a busy
        # port must exit with the voice still running, not wait 3 h for the safety timer
        voice_msg = self.gate.engage() if self.gate else None
        meta['voice_gate'] = voice_msg
        if voice_msg:
            print('[mt]', voice_msg, flush=True)
        self._emit({'type': 'status', 'text': 'live', 'engine': f'local {a.model}',
                    'voice': voice_msg or '',
                    # purple highlight: terms, not plain words like 'ticket' or 'firewall'
                    'terms': [t.name for t in vocab.terms if not (t.name.islower() and ' ' not in t.name)]})

        async def watch():
            last_peak_warn = 0
            while True:
                items = list(dict.fromkeys(tapmod.mismatches(them_dev, me_dev)))   # one per app, not per stream
                for s in streams:
                    if not s.alive():
                        items.append(f'recording of {s.name} stopped — parec exited')
                if (time.time() - meta['started'] > 60 and 'me' in [s.name for s in streams]
                        and streams[-1].peak < 50 and time.time() - last_peak_warn > 60):
                    items.append('your mic has been silent for a minute — check the input device')
                if items != self.warns:
                    self._emit({'type': 'warn', 'items': items})
                await asyncio.sleep(30 if not items else 10)
        watch_task = asyncio.create_task(watch())

        if on_ready:
            on_ready(self)
        try:
            if stop_after:
                await asyncio.wait([asyncio.create_task(stop.wait()), asyncio.create_task(stop_after())],
                                   return_when=asyncio.FIRST_COMPLETED)
            else:
                await stop.wait()
        finally:
            print('[mt] stopping …', flush=True)
            for s in streams:
                s.stop()
            self.stt.flush()
            await self.loop.run_in_executor(None, self.stt.drain, 60)
            # let the fixer finish what it has (bounded)
            for _ in range(60):
                if not self.fixer.pending:
                    break
                await asyncio.sleep(0.5)
            await asyncio.sleep(1)
            fixer_task.cancel()
            watch_task.cancel()
            try:
                await asyncio.wait_for(self.fixer.close(), 5)   # a dead CLI child must not hang the stop
            except (asyncio.TimeoutError, Exception) as e:
                print(f'[mt] fixer close: {e!r}', flush=True)
            self.stt.stop()
            for s in streams:
                if os.path.exists(s.path):
                    tapmod.wrap(s.path)
            meta.update({'ended': time.time(),
                         't0': {s.name: s.t0 for s in streams},
                         'samples': {s.name: s.samples for s in streams},
                         'peak': {s.name: s.peak for s in streams},
                         'stt': summarize(self.stt.stats), 'fixer': summarize(self.fixer.stats)})
            if self.gate:
                msg = self.gate.release()
                meta['voice_release'] = msg
                print('[mt]', msg, flush=True)
            json.dump(meta, open(os.path.join(self.dir, 'session.json'), 'w'), indent=1)
            self.log_f.close()
            for q in list(self.clients):
                q.put_nowait(None)               # sentinel: each SSE handler returns
            await runner.cleanup()
            print(f'[mt] saved {self.dir}', flush=True)
        return self.dir, meta


def summarize(stats):
    out = {}
    for k, v in stats.items():
        if isinstance(v, list):
            if v:
                sv = sorted(v)
                out[k] = {'n': len(v), 'median': round(sv[len(v) // 2], 2),
                          'p90': round(sv[int(len(v) * 0.9)], 2), 'max': round(sv[-1], 2)}
            else:
                out[k] = {'n': 0}
        else:
            out[k] = v
    return out
