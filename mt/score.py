"""Score a live session against human captions: terms right / N, WER, lag.

terms-right: a term occurrence in the human subs counts when the exact spelling (case-insensitive,
word-bounded) appears in a caption line whose audio overlaps ±5 s of the term's position.
Scored twice: raw STT, and after the fixer's corrections.
"""
import json
import re

from .fixer import ACRO
from .vocab import Vocab

WINDOW_S = 5.0


def _dict():
    try:
        return {w.strip().lower() for w in open('/usr/share/dict/words', encoding='utf-8', errors='ignore')}
    except OSError:
        return set()


def ref_terms(ref_text, vocab):
    """Terms present in the human subs, matched case-INsensitively (some human subs are all
    lowercase). Plain dictionary words ("switch", "case") and 2-letter caps ("IT", "US") are left
    out — they would swamp the measure with words any engine gets right."""
    words = _dict()
    low = ref_text.lower()
    out = set()
    for t in vocab.terms:
        for n in [t.name] + t.aliases:
            k = n.lower()
            common = (' ' not in k and k in words and not (n.isupper() and len(n) >= 3))
            if len(k) < 3 or common:
                continue
            if re.search(r'(?<![a-z0-9])' + re.escape(k) + r'(?![a-z0-9])', low):
                out.add(n)
    for m in ACRO.finditer(ref_text):                         # acronyms not in the list
        a = m.group(0)
        if len(a) >= 3 and a.lower() not in words and a.lower().rstrip('s') not in words:
            out.add(a)
    return out


def parse_vtt(path):
    cues = []
    ts = re.compile(r'(\d+):(\d+):(\d+)\.(\d+)\s+-->\s+(\d+):(\d+):(\d+)\.(\d+)')
    cur = None
    for line in open(path, encoding='utf-8'):
        line = line.rstrip('\n')
        m = ts.search(line)
        if m:
            g = [int(x) for x in m.groups()]
            cur = [g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000,
                   g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000, []]
            cues.append(cur)
        elif cur is not None and line.strip():
            cur[2].append(re.sub(r'<[^>]+>', '', line).replace('&nbsp;', ' ').strip())
        elif not line.strip():
            cur = None
    return [(a, b, ' '.join(t)) for a, b, t in cues if t]


def norm_words(text):
    text = text.lower().replace('’', "'")
    text = re.sub(r"[^a-z0-9'./ -]+", ' ', text)
    return [w.strip(".'-/") for w in text.split() if w.strip(".'-/")]


def wer(ref, hyp):
    r, h = ref, hyp
    prev = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        cur = [i] + [0] * len(h)
        for j in range(1, len(h) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r[i - 1] != h[j - 1]))
        prev = cur
    return prev[-1] / max(len(r), 1)


def term_occurrences(cues, terms):
    """[(term, t_mid)] for every occurrence of every term in the human subs."""
    occ = []
    for a, b, text in cues:
        for term in terms:
            for _ in re.finditer(r'(?<![\w-])' + re.escape(term) + r'(?![\w-])', text, re.I):
                occ.append((term, (a + b) / 2))
    return occ


def load_session(path_jsonl, media_t0):
    lines, fixes, cards = {}, {}, []
    for raw in open(path_jsonl):
        e = json.loads(raw)
        if e['type'] == 'commit':
            lines[e['id']] = {**e, 'm_start': e['t_start'] - media_t0, 'm_end': e['t_end'] - media_t0}
        elif e['type'] == 'fix':
            fixes[e['id']] = e
        elif e['type'] == 'card':
            cards.append(e)
    return lines, fixes, cards


def score(session_dir, vtt, media_t0, extra_terms=(), vocab=None):
    vocab = vocab or Vocab.load()
    cues = parse_vtt(vtt)
    lines, fixes, cards = load_session(session_dir + '/live.jsonl', media_t0)
    end = max((l['m_end'] for l in lines.values()), default=0) + 2
    cues = [c for c in cues if c[0] < end]                      # partial runs (--seconds)

    ref_text = ' '.join(c[2] for c in cues)
    terms = set(extra_terms) | ref_terms(ref_text, vocab)
    occ = term_occurrences(cues, sorted(terms))

    def hit(term, t, use_fix):
        for cid, l in lines.items():
            if l['m_end'] >= t - WINDOW_S and l['m_start'] <= t + WINDOW_S:
                text = fixes[cid]['text'] if use_fix and cid in fixes else l['text']
                if re.search(r'(?<![\w-])' + re.escape(term) + r'(?![\w-])', text, re.I):
                    return True
        return False

    raw_hits = [(term, t, hit(term, t, False)) for term, t in occ]
    fix_hits = [(term, t, hit(term, t, True)) for term, t in occ]
    ordered = sorted(lines.values(), key=lambda l: l['t_start'])
    hyp_raw = ' '.join(l['text'] for l in ordered)
    hyp_fix = ' '.join(fixes[l['id']]['text'] if l['id'] in fixes else l['text'] for l in ordered)
    rw = norm_words(ref_text)
    lag = sorted(l['t_commit'] - l['t_end'] for l in ordered)
    fix_lag = sorted(fixes[c]['t'] - lines[c]['t_commit'] for c in fixes if c in lines)
    card_lag = []
    for c in cards:
        heard = [l for l in ordered if c['term'].lower() in (fixes.get(l['id'], l)['text']).lower()]
        if heard:
            card_lag.append(c['t'] - heard[0]['t_commit'])

    def pct(xs):
        return {'n': len(xs), 'median': round(xs[len(xs) // 2], 2) if xs else None,
                'p90': round(xs[int(len(xs) * 0.9)], 2) if xs else None,
                'max': round(xs[-1], 2) if xs else None}

    return {
        'ref_seconds': round(end, 1), 'ref_words': len(rw), 'lines': len(lines),
        'terms_N': len(occ), 'distinct_terms': len({t for t, _ in occ}),
        'terms_right_raw': sum(h for _, _, h in raw_hits),
        'terms_right_fixed': sum(h for _, _, h in fix_hits),
        'missed_raw': sorted({t for t, _, h in raw_hits if not h}),
        'missed_fixed': sorted({t for t, _, h in fix_hits if not h}),
        'wer_raw': round(wer(rw, norm_words(hyp_raw)), 3),
        'wer_fixed': round(wer(rw, norm_words(hyp_fix)), 3),
        'commit_lag_s': pct(lag), 'fix_after_commit_s': pct(fix_lag),
        'card_after_commit_s': pct(sorted(card_lag)),
        'fixes': len(fixes), 'cards': len(cards), 'card_terms': [c['term'] for c in cards],
    }
