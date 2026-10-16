"""`mt mock`: unattended end-to-end test on a NULL sink — nothing plays aloud, other audio
stays up, your screen is untouched. The mock audio is paplay'd into `mt_test`, `mt live` taps
`mt_test.monitor`, then the session is scored against the human captions.

Load is SYNTHETIC (a realtime 720p x264 encode ≈ a video call's encoder); real-call load is M2.
"""
import asyncio
import json
import os
import subprocess
import time
import types

from .live import Live
from .score import score

SINK = 'mt_test'


def run(args):
    mod = subprocess.run(['pactl', 'load-module', 'module-null-sink', f'sink_name={SINK}',
                          f'sink_properties=device.description={SINK}'],
                         capture_output=True, text=True, check=True).stdout.strip()
    audio = args.audio
    tmp = None
    load = None
    try:
        if args.seconds:
            tmp = os.path.join(os.path.dirname(os.path.abspath(args.audio)), f'.cut-{args.seconds}.wav')
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', args.audio, '-t', str(args.seconds), tmp], check=True)
            audio = tmp
        if args.load:
            load = subprocess.Popen(
                ['ffmpeg', '-v', 'error', '-re', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30',
                 '-c:v', 'libx264', '-preset', 'veryfast', '-threads', str(args.load), '-f', 'null', '-'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        state = {}

        def on_ready(live):
            state['t0'] = time.time()
            state['play'] = subprocess.Popen(['paplay', f'--device={SINK}', audio])

        async def stop_after():
            while 'play' not in state:
                await asyncio.sleep(0.1)
            while state['play'].poll() is None:
                await asyncio.sleep(0.5)
            await asyncio.sleep(3)

        largs = types.SimpleNamespace(
            out=None, name='mock', context=args.context, sink=SINK, source=None, no_me=True,
            model=args.model, partial_model=args.partial_model, threads=args.threads, no_fixer=args.no_fixer, fixer_mode=args.fixer_mode, lan=False,
            port=args.port, voice_gate=False, verbose=args.verbose)
        live = Live(largs)
        sdir, meta = asyncio.run(live.run(stop_after=stop_after, on_ready=on_ready))
        res = score(sdir, args.vtt, state['t0'],
                    extra_terms=[t.strip() for t in args.terms.split(',') if t.strip()])
        res.update({'engine': meta['engine'], 'synthetic_load_threads': args.load or 0,
                    'stt': meta['stt'], 'fixer': meta['fixer'],
                    'fixer_calls_per_hour': round(meta['fixer'].get('calls', 0) /
                                                  max(res['ref_seconds'], 1) * 3600, 1)})
        json.dump(res, open(os.path.join(sdir, 'score.json'), 'w'), indent=1)
        print(json.dumps(res, indent=1))
        return res
    finally:
        if load:
            load.terminate()
        subprocess.run(['pactl', 'unload-module', mod], capture_output=True)
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
