#!/usr/bin/env python3
"""Synthetic two-track session for exercising `meetcap review` mechanics (merge, question/reply pairing,
timing metrics, notes) — NOT a measure of real-call accuracy; that needs a real call.

Voices: any two piper models (set MEETCAP_PIPER_THEM / MEETCAP_PIPER_ME to .onnx paths; the
tracks are separate files, so the voices need not differ). Timeline is scripted, so
the expected pauses and answer lengths are known.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

RATE = 16000
PIPER_ME = os.environ.get('MEETCAP_PIPER_ME', 'en_US-lessac-medium.onnx')
PIPER_THEM = os.environ.get('MEETCAP_PIPER_THEM', PIPER_ME)

# (who, text, pause_before_s)
SCRIPT = [
    ('them', "Hi Sam, thanks for joining. I'm Dana, I run the service desk here at Northwind. "
             "Can you start with a quick update on your project?", 0.5),
    ('me', "Sure. Um, I've been doing IT support for we finished the laptop rollout last month, "
           "and now it is mostly device management and scripting.", 1.2),
    ('them', "Great. We get a lot of email spoofing tickets. How would you check a domain's "
             "DMARC and SPF setup?", 0.8),
    ('me', "Sorry, could you repeat the question?", 2.5),
    ('them', "Sure. How would you check a domain's DMARC and SPF records?", 0.6),
    ('me', "I'd pull the TXT records with nslookup or dig, check the SPF include list against "
           "the senders we actually use, and look at the DMARC policy and the aggregate reports.", 3.0),
    ('them', "How did the last outage go?", 0.7),
    ('me', "Uh, yeah, so, outages happen, you know, and we just kind of stay calm and "
           "work the problem and keep people updated.", 1.5),
    ('them', "Okay. Our tier two team uses ServiceNow and an RMM called NinjaOne. Have you used those?", 0.8),
    ('me', "I've used ServiceNow for a while. I haven't used NinjaOne, but I've used "
           "a couple of open-source remote management tools, which are similar. I can send you a write-up of my RMM setup after this.", 0.9),
    ('them', "That would be great. Next step is a technical review with our lead, Marco, probably next week. "
             "Any questions for me?", 0.6),
    ('me', "Yes, what does a typical day look like for the service desk team?", 1.0),
]


def tts(who, text, out_wav):
    model = PIPER_ME if who == 'me' else PIPER_THEM
    src = out_wav + '.raw.wav'
    subprocess.run(['piper', '--model', model, '--output_file', src],
                   input=text.encode(), check=True, capture_output=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-ar', str(RATE), '-ac', '1', out_wav], check=True)
    with wave.open(out_wav) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    tracks = {'them': [], 'me': []}
    cursor = 1.0
    expected = []
    with tempfile.TemporaryDirectory() as td:
        for i, (who, text, pause) in enumerate(SCRIPT):
            a = tts(who, text, os.path.join(td, f'{i}.wav'))
            cursor += pause
            tracks[who].append((cursor, a))
            expected.append({'who': who, 'start': round(cursor, 2), 'dur': round(len(a) / RATE, 2), 'text': text})
            cursor += len(a) / RATE
    total = int((cursor + 1.0) * RATE)
    base = time.time() - cursor - 5
    for who, parts in tracks.items():
        buf = np.zeros(total, dtype=np.int16)
        for start, a in parts:
            s = int(start * RATE)
            buf[s:s + len(a)] = a
        with wave.open(os.path.join(out_dir, who + '.wav'), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
            w.writeframes(buf.tobytes())
    json.dump({'started': base, 'name': 'synthetic-northwind', 't0': {'them': base, 'me': base},
               'engine': 'synthetic', 'context': None}, open(os.path.join(out_dir, 'session.json'), 'w'), indent=1)
    json.dump(expected, open(os.path.join(out_dir, 'expected.json'), 'w'), indent=1)
    print(out_dir)


if __name__ == '__main__':
    main(sys.argv[1])
