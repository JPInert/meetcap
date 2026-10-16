"""The tap: record what the speakers play (them) and what the mic hears (me), app-agnostic.

Each stream is one `parec` writing raw s16le 16 kHz mono. Raw, not WAV: a WAV header is only
right once the file is closed, and a crash mid-call must not cost the one recording that
mattered — `wrap()` turns raw into WAV at the end, or later from `mt wrap` after a crash.
"""
import json
import os
import subprocess
import threading
import time
import wave

import numpy as np

RATE = 16000
CHUNK = 1600                     # samples per read = 100 ms
MEETING_APPS = ('zoom', 'teams', 'librewolf', 'firefox', 'chrom', 'electron', 'webrtc')


def pactl(*args):
    return subprocess.run(['pactl', *args], capture_output=True, text=True, timeout=5).stdout.strip()


def default_devices():
    return pactl('get-default-sink') + '.monitor', pactl('get-default-source')


class Stream:
    """One parec process → raw file + a callback per 100 ms chunk."""

    def __init__(self, name, device, outdir, on_chunk=None):
        self.name, self.device = name, device
        self.path = os.path.join(outdir, name + '.s16le')
        self.on_chunk = on_chunk
        self.t0 = None               # wall time of sample 0 (first chunk's arrival − its length)
        self.samples = 0
        self.peak = 0
        self.proc = None
        self.thread = None

    def start(self):
        self.proc = subprocess.Popen(
            ['parec', '--device', self.device, '--format=s16le', f'--rate={RATE}',
             '--channels=1', '--latency-msec=50', '--raw'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.thread = threading.Thread(target=self._run, name='tap-' + self.name, daemon=True)
        self.thread.start()

    def _run(self):
        with open(self.path, 'ab') as f:
            while True:
                buf = self.proc.stdout.read(CHUNK * 2)
                if not buf:
                    break
                now = time.time()
                a = np.frombuffer(buf, dtype=np.int16)
                if self.t0 is None:
                    self.t0 = now - len(a) / RATE
                f.write(buf)
                f.flush()
                self.samples += len(a)
                self.peak = max(self.peak, int(np.abs(a).max()) if len(a) else 0)
                if self.on_chunk:
                    try:
                        self.on_chunk(a, self.t0 + (self.samples - len(a)) / RATE)
                    except Exception as e:      # a consumer bug must not stop the recording
                        print(f'[tap {self.name}] consumer error: {e!r}', flush=True)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.thread:
            self.thread.join(3)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None


def wrap(raw_path):
    """raw s16le → .wav next to it. Safe to run on a crashed session."""
    wav_path = raw_path[:-len('.s16le')] + '.wav'
    with open(raw_path, 'rb') as r, wave.open(wav_path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            w.writeframes(b)
    return wav_path


def app_streams():
    """Meeting apps' live playback/capture streams and the device each one is on.

    Returns [(kind, app, device_name)] where kind is 'play' or 'mic'. Used to warn when the
    meeting app was pointed at a different device than the one we tap (earbuds picked in
    Zoom's own settings while the default is still the other device = we record silence)."""
    out = []
    try:
        sinks = {s['index']: s['name'] for s in json.loads(pactl('-f', 'json', 'list', 'sinks') or '[]')}
        sources = {s['index']: s['name'] for s in json.loads(pactl('-f', 'json', 'list', 'sources') or '[]')}
        for kind, listing, key, names in (('play', 'sink-inputs', 'sink', sinks),
                                          ('mic', 'source-outputs', 'source', sources)):
            for s in json.loads(pactl('-f', 'json', 'list', listing) or '[]'):
                p = s.get('properties', {})
                app = (p.get('application.name') or p.get('application.process.binary') or '').lower()
                if any(m in app for m in MEETING_APPS) and 'parec' not in app:
                    out.append((kind, app, names.get(s.get(key), str(s.get(key)))))
    except Exception as e:
        print(f'[tap] app_streams: {e!r}', flush=True)
    return out


def mismatches(them_dev, me_dev):
    """Human-readable warnings for meeting streams not on the tapped devices."""
    warns = []
    for kind, app, dev in app_streams():
        if kind == 'play' and dev + '.monitor' != them_dev:
            warns.append(f'{app} is playing to {dev}, but we tap {them_dev} — their voice may be missing')
        if kind == 'mic' and dev != me_dev and not dev.endswith('.monitor'):
            warns.append(f'{app} uses mic {dev}, but we tap {me_dev} — your voice may be missing')
    return warns
