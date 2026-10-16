"""Live speech-to-text on the THEM track.

Shape: the tap thread feeds 100 ms chunks into an energy VAD that cuts utterances. Two worker
threads: COMMITS (finished utterances, the main model) and PARTIALS (the open utterance, a tiny
model, latest-wins and never queued). Measured 10-12: faster-whisper pads every call to a 30 s
window, so a call costs the same for 3 s or 8 s of audio — small.en ≈2.5 s, base.en ≈0.9 s,
tiny.en ≈0.45 s on 6 threads. Sharing one worker made each commit wait behind a 2.5 s partial
(commit lag median 6.9 s, n=7); separate workers keep commits at ~one model call.
"""
import queue
import threading
import time

import numpy as np

from .tap import RATE

FRAME = 480                       # 30 ms VAD frames
COMMIT_SIL_S = 0.7                # silence that ends an utterance
CAP_S = 8.0                       # longest utterance before a forced cut
DIP_S = 0.3                       # a forced cut lands on the last dip this long…
DIP_SEARCH_S = 2.0                # …inside the final 2 s
MIN_UTT_S = 0.4
PARTIAL_MIN_S = 0.8               # don't bother transcribing a partial shorter than this
PRE_S = 0.3                       # audio kept before the first speech frame
HALLUCINATIONS = {'thank you.', 'thanks for watching!', 'thank you for watching.', 'you', '.',
                  'bye.', 'thank you very much.', 'so', 'okay.'}


class LocalWhisper:
    name = 'local'

    def __init__(self, model='small.en', threads=6, prompt=None):
        from faster_whisper import WhisperModel
        self.model_name = model
        self.model = WhisperModel(model, device='cpu', compute_type='int8', cpu_threads=threads)
        self.prompt = prompt

    def transcribe(self, audio_i16, final=True):
        a = audio_i16.astype(np.float32) / 32768.0
        segs, _ = self.model.transcribe(
            a, beam_size=1, language='en', condition_on_previous_text=False,
            initial_prompt=self.prompt, without_timestamps=True, vad_filter=False,
            # ONE fallback + a token cap. Default fallback (6 temperatures, 448 tokens) once took
            # 38 s on a 7.9 s line and 30 s of captions queued behind it; NO fallback turned two
            # lines into invented sentences (10-12 auth mock). 8 s of speech ≈ 60 tokens.
            temperature=[0.0, 0.4], max_new_tokens=128)
        out = []
        for s in segs:
            if s.no_speech_prob > 0.6 and s.avg_logprob < -0.8:
                continue
            # greedy decode failed (fell back to 0.4) on audio the model half-thinks is silence: on
            # one recorded call these were all filler or invented lines ("see you in the next video",
            # "EDR."), 15/51 segments; 0/178 on the two scripted clips (base.en + word list)
            if final and s.temperature > 0 and s.no_speech_prob > 0.3:
                continue
            out.append(s.text.strip())
        text = ' '.join(out).strip()
        return '' if text.lower() in HALLUCINATIONS else text


class LiveSTT:
    """Feed with `feed(chunk, t_start)` from the tap thread; events go to `emit(dict)`."""

    def __init__(self, engine, emit, thresh=None, partial_engine=None):
        self.engine, self.emit = engine, emit
        self.partial_engine = partial_engine
        self.thresh = thresh             # fixed RMS threshold, or None = adaptive
        self.noise = 60.0                # running noise-floor estimate (RMS)
        self.lock = threading.Lock()
        self.cur = []                    # frames of the open utterance
        self.cur_t0 = None               # wall time of its first sample
        self.pre = []                    # rolling pre-speech frames
        self.sil_frames = 0
        self.in_speech = False
        self.cur_version = 0             # bumps on every new frame → worker knows if partial is stale
        self.commits = queue.Queue()
        self.next_id = 1
        self._tail = np.zeros(0, dtype=np.int16)
        self.stats = {'commits': 0, 'partials': 0, 'stt_s': [], 'partial_s': [], 'commit_lag_s': []}
        self.busy = False
        self.running = True
        self.worker = threading.Thread(target=self._work, name='stt', daemon=True)
        self.worker.start()
        self.pworker = None
        if partial_engine is not None:
            self.pworker = threading.Thread(target=self._partials, name='stt-partial', daemon=True)
            self.pworker.start()

    # ---- VAD side (tap thread) ----
    def _is_speech(self, rms):
        th = self.thresh or max(150.0, self.noise * 3.0)
        if not self.in_speech:           # noise floor only learns from non-speech
            self.noise = 0.995 * self.noise + 0.005 * max(rms, 1.0)
        return rms > th

    def feed(self, chunk, t_start):
        a = np.concatenate([self._tail, chunk]) if len(self._tail) else chunk
        n = len(a) // FRAME
        t0 = t_start - len(self._tail) / RATE
        self._tail = a[n * FRAME:].copy()
        for i in range(n):
            fr = a[i * FRAME:(i + 1) * FRAME]
            ft = t0 + i * FRAME / RATE
            rms = float(np.sqrt(np.mean(fr.astype(np.float32) ** 2)))
            sp = self._is_speech(rms)
            with self.lock:
                if not self.in_speech:
                    self.pre.append((fr, ft, rms))
                    if len(self.pre) > int(PRE_S * RATE / FRAME):
                        self.pre.pop(0)
                    if sp:
                        self.in_speech = True
                        self.cur = list(self.pre)
                        self.cur_t0 = self.pre[0][1]
                        self.pre = []
                        self.sil_frames = 0
                    continue
                self.cur.append((fr, ft, rms))
                self.cur_version += 1
                self.sil_frames = 0 if sp else self.sil_frames + 1
                dur = len(self.cur) * FRAME / RATE
                if self.sil_frames * FRAME / RATE >= COMMIT_SIL_S:
                    self._commit_locked(len(self.cur) - self.sil_frames + int(0.2 * RATE / FRAME))
                elif dur >= CAP_S:
                    self._commit_locked(self._dip_cut())

    def _dip_cut(self):
        """Index to cut a capped utterance at: end of the last ≥DIP_S quiet run in the final
        DIP_SEARCH_S, else the quietest frame there — never a blind cut mid-word."""
        th = self.thresh or max(150.0, self.noise * 3.0)
        n = len(self.cur)
        lo = max(0, n - int(DIP_SEARCH_S * RATE / FRAME))
        need = int(DIP_S * RATE / FRAME)
        run, best = 0, None
        for i in range(lo, n):
            run = run + 1 if self.cur[i][2] <= th * 1.5 else 0
            if run >= need:
                best = i - run // 2
        if best is None:
            best = min(range(lo, n), key=lambda i: self.cur[i][2])
        return max(best, 1)

    def _commit_locked(self, cut):
        cut = min(max(cut, 1), len(self.cur))
        frames, rest = self.cur[:cut], self.cur[cut:]
        dur = len(frames) * FRAME / RATE
        if dur >= MIN_UTT_S:
            audio = np.concatenate([f[0] for f in frames])
            t_end = frames[-1][1] + FRAME / RATE
            self.commits.put((self.next_id, audio, frames[0][1], t_end, time.time()))
            self.next_id += 1
        if rest and any(r[2] > (self.thresh or max(150.0, self.noise * 3.0)) for r in rest):
            self.cur, self.cur_t0 = rest, rest[0][1]          # capped: speech continues
            self.sil_frames = 0
        else:
            self.cur, self.cur_t0, self.in_speech = [], None, False
        self.cur_version += 1

    def flush(self):
        with self.lock:
            if self.in_speech and self.cur:
                self._commit_locked(len(self.cur))

    # ---- worker side ----
    def _partials(self):
        last_version = -1
        while self.running:
            with self.lock:
                v = self.cur_version
                snap = (np.concatenate([f[0] for f in self.cur])
                        if self.in_speech and self.cur else None)
            if snap is None or v == last_version or len(snap) < PARTIAL_MIN_S * RATE:
                time.sleep(0.1)
                continue
            last_version = v
            ts = time.time()
            text = self.partial_engine.transcribe(snap, final=False)
            self.stats['partials'] += 1
            self.stats['partial_s'].append(time.time() - ts)
            with self.lock:
                still_open = self.in_speech
            if text and still_open:
                self.emit({'type': 'partial', 'text': text})

    def _work(self):
        while self.running:
            try:
                cid, audio, t0, t1, t_cut = self.commits.get(timeout=0.15)
            except queue.Empty:
                continue
            ts = time.time()
            self.busy = True
            try:
                text = self.engine.transcribe(audio, final=True)
            finally:
                self.busy = False
            now = time.time()
            self.stats['stt_s'].append(now - ts)
            if not text:
                continue
            self.stats['commits'] += 1
            self.stats['commit_lag_s'].append(now - t1)
            self.emit({'type': 'commit', 'id': cid, 'text': text, 't_start': round(t0, 3),
                       't_end': round(t1, 3), 't_commit': round(now, 3),
                       'stt_s': round(now - ts, 2), 'wait_s': round(ts - t_cut, 2)})

    def drain(self, timeout=30):
        end = time.time() + timeout
        time.sleep(0.3)
        while (not self.commits.empty() or self.busy) and time.time() < end:
            time.sleep(0.1)

    def stop(self):
        self.running = False
        self.worker.join(5)
        if self.pworker:
            self.pworker.join(5)
