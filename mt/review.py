"""`mt review <session>`: after the call — full transcript of both tracks, metrics, notes.

Metrics that come from TIMING are computed here in code and are solid: talk ratio, reply length
per question, pause before replying, "can you repeat that" count. Filler words are approximate
at best: whisper is trained on cleaned captions and drops most um/uh, so they are reported as a
count with that caveat and never as a rate. The model writes only the qualitative parts (recap, commitments, new terms).

Several speakers on one THEM track are not told apart — names come from the invite.
"""
import asyncio
import json
import os
import re
import time
import wave

import numpy as np

from .vocab import DATA, LEARNED, Vocab

HISTORY = os.path.join(DATA, 'history.jsonl')
TURN_GAP_S = 2.0
SPLIT_GAP_S = 1.0                 # a word gap this long starts a new segment
QUESTION = re.compile(r"\?\s*$|^(what|how|why|when|where|which|who|can you|could you|tell me|walk me|"
                      r"describe|have you|do you|did you|would you|give me|talk me|explain)\b", re.I)
REPEAT = re.compile(r"\b(repeat (that|the question)|say that again|come again|didn'?t (catch|hear)|"
                    r"sorry,? what|pardon|you cut out|could you rephrase)\b", re.I)
FILLER = re.compile(r"\b(um+|uh+|erm|you know|i mean|kind of|sort of)\b", re.I)
REVIEW_MODEL = 'sonnet'


def load_wav(path):
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def transcribe_track(model, path, t0, who, prompt, beam=5):
    """Word-level, regrouped by real gaps. Segment-level is wrong here: vad_filter splices the
    speech chunks together before decoding, so one whisper segment can span a 10 s gap and
    swallow the start of the NEXT answer into the previous turn (synthetic test 10-12: "I've
    used ServiceNow…" landed 10 s early, inside the outage answer). Word timestamps are mapped
    back to real time, so splitting on word gaps keeps turns where they were spoken."""
    a = load_wav(path)
    if a.size == 0 or float(np.abs(a).max()) < 0.003:
        return []
    segs, _ = model.transcribe(
        a, beam_size=beam, language='en', vad_filter=True, condition_on_previous_text=False,
        initial_prompt='Um, uh, so, ' + prompt, word_timestamps=True)
    words = []
    for s in segs:
        if s.no_speech_prob > 0.6 and s.avg_logprob < -0.8:
            continue
        words += [(w.start, w.end, w.word) for w in (s.words or [])]
    out, cur = [], None
    for ws, we, w in words:
        if cur and t0 + ws - cur['end'] > SPLIT_GAP_S:
            out.append(cur)
            cur = None
        if cur is None:
            cur = {'who': who, 'start': t0 + ws, 'end': t0 + we, 'text': w.strip()}
        else:
            cur['end'] = t0 + we
            cur['text'] += w
        cur['end'] = t0 + we
    if cur:
        out.append(cur)
    for o in out:
        o['text'] = o['text'].strip()
    return [o for o in out if o['text']]


def drop_bleed(segs):
    """Remove ME segments that are their voice leaking into the mic: same words as a THEM
    segment at the same time. On speakers the speakerphone's echo canceller needed ~20 s to
    converge (live test: the first 20 s of the video landed on ME word for word)."""
    them = [s for s in segs if s['who'] == 'them']
    out = []
    for s in segs:
        if s['who'] == 'me':
            w = set(re.findall(r"[a-z']+", s['text'].lower()))
            for t in them:
                if t['end'] >= s['start'] - 2 and t['start'] <= s['end'] + 2 and w:
                    tw = set(re.findall(r"[a-z']+", t['text'].lower()))
                    if len(w & tw) / len(w) >= 0.6:
                        break
            else:
                out.append(s)
            continue
        out.append(s)
    return out


def turns(segs):
    """Merge consecutive same-speaker segments into turns."""
    out = []
    for s in sorted(segs, key=lambda x: x['start']):
        if out and out[-1]['who'] == s['who'] and s['start'] - out[-1]['end'] < TURN_GAP_S:
            out[-1]['end'] = s['end']
            out[-1]['text'] += ' ' + s['text']
        else:
            out.append(dict(s))
    return out


def _question_sentence(text):
    # STT can attach the next turn's opening fragment after the actual question.
    for sentence in reversed(re.split(r'(?<=[.?!])\s+', text.strip())):
        if QUESTION.search(sentence):
            return sentence
    return None


def metrics(tr, t_zero):
    talk = {'them': 0.0, 'me': 0.0}
    for t in tr:
        talk[t['who']] += t['end'] - t['start']
    qa = []
    for i, t in enumerate(tr):
        if t['who'] != 'them':
            continue
        question = _question_sentence(t['text'])
        if not question:
            continue
        answer = []
        for u in tr[i + 1:]:
            if u['who'] == 'them' and ((u['end'] - u['start']) > 3 or _question_sentence(u['text'])):
                break                     # a new question ends the answer even when spoken quickly
            if u['who'] == 'me':
                answer.append(u)
        # "could you repeat that?" is not an answer: the question gets asked again and the
        # answer belongs to the repeat — counted separately as a repeat request
        if answer and REPEAT.search(answer[0]['text']) and len(answer[0]['text'].split()) < 12:
            continue
        if not answer:
            continue
        qa.append({
            'at': round(t['start'] - t_zero),
            'question': question,
            'pause_s': round(answer[0]['start'] - t['end'], 1),
            'answer_s': round(sum(u['end'] - u['start'] for u in answer), 1),
            'answer': ' '.join(u['text'] for u in answer),
        })
    me_text = ' '.join(t['text'] for t in tr if t['who'] == 'me')
    total = talk['them'] + talk['me']
    return {
        'talk_s': {k: round(v) for k, v in talk.items()},
        'my_share': round(talk['me'] / total, 2) if total else None,
        'questions': len(qa),
        'repeat_requests': len(REPEAT.findall(me_text)),
        'fillers_approx': len(FILLER.findall(me_text)),
        'qa': qa,
    }


def fmt_t(sec):
    return f'{int(sec) // 60:02d}:{int(sec) % 60:02d}'


def transcript_md(tr, t_zero):
    return '\n'.join(f"[{fmt_t(t['start'] - t_zero)}] **{'THEM' if t['who'] == 'them' else 'ME'}:** {t['text']}"
                     for t in tr)


PROMPT = """You write notes for a recorded two-sided call, for the participant (ME).
THEM = the other participant(s), all on one track. Asking for a repeat is normal; do not comment on it.
The transcript is machine speech-to-text: odd words and misspellings ("Slokaport" for nslookup)
are transcription errors, NOT how they spoke; silently use the right term (the word list is
included) in the recap, notes and quotes. You get the transcript, timing metrics computed in code,
and optionally a context document (agenda, notes).

Return ONE JSON object, nothing else:
{
 "recap": "<markdown: who was there (names/roles as said), what was discussed, decisions,
            next steps + dates. Short bullets.>",
 "qa": [{"at": <seconds from metrics>, "question": "<clean>", "answer_gist": "<one line>",
         "note": "<one line, optional>"}],
 "commitments": ["<things ME said they would do or send>"],
 "new_terms": [{"term": "<acronym/product/jargon THEM used>", "note": "<<= 12 words>"}]
}
Be specific and factual; no generic advice.
If the recording has no questions and answers (a lecture, a training video, ME silent), still return
the JSON: recap = what it covered as short bullets (key points, terms, names, decisions, action items),
new_terms filled, qa/commitments empty. Never ask for a different file."""


async def llm(payload, model=REVIEW_MODEL):
    import claude_agent_sdk as s
    opts = s.ClaudeAgentOptions(model=model, system_prompt=PROMPT, tools=[], max_turns=2,
                                setting_sources=[], thinking={'type': 'disabled'},
                                mcp_servers={}, strict_mcp_config=True, env={'MT_CHILD': '1'})
    out = []
    async for m in s.query(prompt=json.dumps(payload), options=opts):
        if isinstance(m, s.AssistantMessage):
            for b in m.content:
                if isinstance(b, s.TextBlock):
                    out.append(b.text)
    text = ''.join(out)
    m = re.search(r'\{.*\}', text, re.S)
    return json.loads(m.group(0)) if m else {'recap': text}


def run(args):
    sdir = args.session.rstrip('/')
    meta = json.load(open(os.path.join(sdir, 'session.json')))
    t0 = meta.get('t0', {})
    vocab = Vocab.load(meta.get('context'))
    from faster_whisper import WhisperModel
    t_load = time.time()
    try:
        model = WhisperModel(args.model, device='cpu', compute_type='int8', cpu_threads=args.threads)
        model_used = args.model
    except Exception as e:
        print(f'[review] {args.model} unavailable ({e!r}) — falling back to small.en', flush=True)
        model = WhisperModel('small.en', device='cpu', compute_type='int8', cpu_threads=args.threads)
        model_used = 'small.en'
    segs = []
    timing = {}
    for who in ('them', 'me'):
        path = os.path.join(sdir, who + '.wav')
        if os.path.exists(path) and t0.get(who):
            ts = time.time()
            segs += transcribe_track(model, path, t0[who], who, vocab.prompt(), beam=args.beam)
            timing[who] = round(time.time() - ts, 1)
    if not segs:
        raise SystemExit('nothing transcribed — both tracks silent?')
    t_zero = min(v for v in t0.values() if v)
    segs = drop_bleed(segs)
    tr = turns(segs)
    m = metrics(tr, t_zero)
    open(os.path.join(sdir, 'transcript.md'), 'w').write(transcript_md(tr, t_zero) + '\n')

    payload = {'transcript': transcript_md(tr, t_zero), 'metrics': {k: v for k, v in m.items() if k != 'qa'},
               'qa_timing': [{k: q[k] for k in ('at', 'question', 'pause_s', 'answer_s')} for q in m['qa']],
               'word_list': [t.name for t in vocab.terms]}
    if meta.get('context') and os.path.exists(meta['context']):
        payload['context'] = open(meta['context'], errors='replace').read()[:6000]
    r = {} if args.no_llm else asyncio.run(llm(payload))

    timing_by_at = {q['at']: q for q in m['qa']}
    qa_rows = []
    for q in r.get('qa') or m['qa']:
        t = timing_by_at.get(q.get('at'), {})
        qa_rows.append(f"| {fmt_t(q.get('at', 0))} | {q.get('question', '')} | {t.get('pause_s', '')} | "
                       f"{t.get('answer_s', '')} | {q.get('note', '')} |")
    name = meta.get('name') or os.path.basename(sdir)
    notes = [f"# Call notes — {name}", '',
             r.get('recap', '_(no model recap — run without --no-llm)_'), '']
    if r.get('commitments'):
        notes += ['## Your commitments', ''] + [f'- {p}' for p in r['commitments']] + ['']
    open(os.path.join(sdir, 'notes.md'), 'w').write('\n'.join(notes))

    mm = m
    out = [f"# Call metrics — {name}", '',
           '| measure | value |', '|---|---|',
           f"| your talk time | {mm['talk_s']['me']} s of {mm['talk_s']['me'] + mm['talk_s']['them']} s"
           f" ({int((mm['my_share'] or 0) * 100)} %) |",
           f"| questions answered | {mm['questions']} |",
           f"| asked them to repeat | {mm['repeat_requests']} |",
           f"| filler words (approx — whisper drops most) | {mm['fillers_approx']} |", '',
           '| at | question | pause s | reply s | note |', '|---|---|---|---|---|'] + qa_rows + ['']
    open(os.path.join(sdir, 'metrics.md'), 'w').write('\n'.join(out))

    review = {'model': model_used, 'stt_seconds': timing, 'load_s': round(time.time() - t_load, 1),
              'metrics': m, 'llm': r}
    json.dump(review, open(os.path.join(sdir, 'review.json'), 'w'), indent=1)

    if not args.no_history:
        os.makedirs(DATA, exist_ok=True)
        with open(HISTORY, 'a') as h:
            h.write(json.dumps({'session': os.path.basename(sdir), 'name': meta.get('name'),
                                'date': time.strftime('%Y-%m-%d', time.localtime(meta.get('started', time.time()))),
                                'metrics': {k: v for k, v in m.items() if k != 'qa'},
                                'questions': [q.get('question') for q in (r.get('qa') or m['qa'])],
                                'new_terms': [t.get('term') for t in r.get('new_terms', [])]}) + '\n')
        known = {t.name.lower() for t in vocab.terms} | {a.lower() for t in vocab.terms for a in t.aliases}
        new = [t for t in r.get('new_terms', []) if t.get('term') and t['term'].lower() not in known]
        if new:
            with open(LEARNED, 'a') as f:
                for t in new:
                    f.write(f"{t['term']} :: {t.get('note', '')}\n")
    print(f"[review] {sdir}: transcript.md notes.md metrics.md review.json ({model_used}, stt {timing})")
    return review
