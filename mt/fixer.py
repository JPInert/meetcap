"""Fixer: a fast model reads committed caption lines, fixes mis-heard terms and drops term cards.

One worker, one queue, coalescing: each wake sends ALL pending lines in one call, at most one
call in flight, and at least MIN_GAP_S between calls — the cost is metered quota, so a local
prefilter skips lines with nothing a model could help with. One persistent ClaudeSDKClient keeps
the CLI process alive between calls (a fresh `query()` spawns a process each time); it is
recycled every RECYCLE calls so the conversation does not grow without bound.

Hard rule in the prompt and here: spelling, meaning, aliases — never advice or suggested answers.
"""
import asyncio
import difflib
import json
import re
import time

MAX_NON_TERM_EDITS = 2
MIN_GAP_S = 8.0                    # 4 s gave 409 calls/h on jargon-dense audio (n=42 calls, 6 min); 8 s coalesces ~2 lines/call
RECYCLE = 30
MODEL = 'haiku'
DICT = '/usr/share/dict/words'
# a dictionary-word line can still hide a mis-heard term ("into" for Intune, "the mark" for
# DMARC); lines with this kind of context word go to the model too
TECH = re.compile(r'\b(record|server|domain|deploy\w*|device|network|email|mail|policy|tenant|'
                  r'account|password|login|sign.?in|laptop|printer|ticket|user|admin|cloud|'
                  r'firewall|switch|router|license|script|patch\w*|update|backup|encrypt\w*|'
                  r'portal|console|group|permission|access|install\w*|image|configure|setup)\b', re.I)
ACRO = re.compile(r'\b[A-Z][A-Z0-9]{1,6}s?\b|\b[A-Z][a-z]+[A-Z]\w*\b')   # DNS, DMARCs, SharePoint

SYSTEM = """You assist a listener who follows a live technical call through captions.
You receive live speech-to-text lines of what the OTHER PARTY said, plus the listener's word list.

Do two things:
1. FIX each line only where speech-to-text clearly mis-heard a technical term, product or name
   (e.g. "the mark record" -> "DMARC record", "into" -> "Intune" when the context is device
   management). Do not rephrase, summarise or correct grammar. If nothing is wrong, leave it out.
2. CARDS for terms the listener may not catch or may know under another name: acronyms,
   products, jargon, regional/company synonyms ("service desk" = help desk). A card is
   spelling + what it is in <= 12 words + other names. Skip everyday words and terms you
   already carded in this conversation, and words used in their everyday sense ("teams" of people,
   "case" as in situation).

NEVER suggest what the listener should say, never answer the speaker's question,
never give tips. Spelling, meaning and aliases only.

Reply with ONE JSON object and nothing else:
{"lines":[{"id":<id>,"fixed":"<corrected line>"}],
 "cards":[{"term":"DMARC","heard":"the mark","note":"email auth policy on SPF + DKIM","aka":["D-mark"]}]}
Use empty lists when there is nothing to do."""


def _load_dict():
    try:
        return {w.strip().lower() for w in open(DICT, encoding='utf-8', errors='ignore')}
    except OSError:
        return set()


class Fixer:
    def __init__(self, vocab, emit, enabled=True, model=MODEL, mode='smart'):
        self.vocab, self.emit, self.enabled, self.model = vocab, emit, enabled, model
        self.mode = mode
        self.words = _load_dict()
        self.pending = []                # (id, text)
        self.context = []                # last few lines, sent along for disambiguation
        self.carded = set()
        self.event = asyncio.Event()
        self.client = None
        self.calls_on_client = 0
        self.client_cost = 0.0
        self.stats = {'lines_in': 0, 'lines_sent': 0, 'calls': 0, 'errors': 0, 'rt_s': [],
                      'cards': 0, 'local_cards': 0, 'fixes': 0, 'fixes_rejected': 0, 'cost_usd': 0.0}

    # ---- prefilter ----
    def worth_sending(self, text):
        if self.mode == 'all':
            return True
        if ACRO.search(text) or self.vocab.hits(text) or TECH.search(text):
            return True
        for w in re.findall(r"[A-Za-z][a-z']{3,}", text):
            lw = w.lower().strip("'")
            if lw.endswith("'s"):
                lw = lw[:-2]
            if self.words and lw not in self.words and lw.rstrip('s') not in self.words:
                return True
        return False

    def local_cards(self, text):
        """Terms already in the word list get their card instantly, no model call."""
        for key, t in self.vocab.hit_keys(text):
            if t.name.lower() in self.carded or not (t.note or t.aliases):
                continue
            orig = next((n for n in [t.name] + t.aliases if n.lower() == key), key)
            if ' ' not in orig and orig.islower() and key in self.words:
                continue                 # "case", "request", "ticket": too common to card on sight
            self.carded.update(n.lower() for n in [t.name] + t.aliases)
            self.stats['local_cards'] += 1
            self.emit({'type': 'card', 'term': t.name, 'heard': '', 'note': t.note,
                       'aka': t.aliases, 't': time.time(), 'src': 'list'})

    def submit(self, cid, text):
        self.stats['lines_in'] += 1
        self.context = (self.context + [text])[-4:]
        # word-list cards come from the core session now (one implementation, every device)
        if not self.enabled or not self.worth_sending(text):
            return
        self.pending.append((cid, text))
        self.event.set()

    # ---- worker ----
    async def run(self):
        last = 0.0
        while True:
            await self.event.wait()
            gap = MIN_GAP_S - (time.time() - last)
            if gap > 0:
                await asyncio.sleep(gap)
            batch, self.pending = self.pending, []
            self.event.clear()
            if not batch:
                continue
            last = time.time()
            try:
                await self._call(batch)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats['errors'] += 1
                print(f'[fixer] call failed: {e!r}', flush=True)
                await self._drop_client()

    async def _client(self):
        import claude_agent_sdk as s
        if self.client is None or self.calls_on_client >= RECYCLE:
            await self._drop_client()
            opts = s.ClaudeAgentOptions(
                model=self.model, system_prompt=SYSTEM + '\n\nWORD LIST:\n' + self.vocab.for_model(),
                tools=[], max_turns=2, setting_sources=[], thinking={'type': 'disabled'},
                effort='low', env={'MT_CHILD': '1'},
                mcp_servers={}, strict_mcp_config=True)
            self.client = s.ClaudeSDKClient(opts)
            await self.client.connect()
            self.calls_on_client = 0
            self.client_cost = 0.0
        return self.client

    async def _drop_client(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def _call(self, batch):
        import claude_agent_sdk as s
        self.stats['lines_sent'] += len(batch)
        ids = {c for c, _ in batch}
        ctx = [t for t in self.context if t not in {x for _, x in batch}][-2:]
        prompt = json.dumps({'earlier': ctx, 'already_carded': sorted(self.carded)[-40:], 'lines': [{'id': c, 'text': t} for c, t in batch]})
        t0 = time.time()
        client = await self._client()
        await client.query(prompt)
        out = []
        async for m in client.receive_response():
            if isinstance(m, s.ResultMessage) and m.total_cost_usd:
                # total_cost_usd is CUMULATIVE for the client's session — add the delta only
                # (summing it raw read $1.47 for a 6-min run that really cost ~$0.15).
                # API-list equivalent; billed against quota.
                self.stats['cost_usd'] += max(0.0, m.total_cost_usd - self.client_cost)
                self.client_cost = m.total_cost_usd
            if isinstance(m, s.AssistantMessage):
                for b in m.content:
                    if isinstance(b, s.TextBlock):
                        out.append(b.text)
        self.calls_on_client += 1
        self.stats['calls'] += 1
        self.stats['rt_s'].append(time.time() - t0)
        data = _json(''.join(out))
        raw = dict(batch)
        for ln in data.get('lines', []):
            if ln.get('id') in ids and ln.get('fixed'):
                extra = self.non_term_edits(raw[ln['id']], ln['fixed'])
                if extra > MAX_NON_TERM_EDITS:
                    self.stats['fixes_rejected'] += 1
                    print(f'[fixer] rejected fix (rewrote {extra} non-term words): {ln["fixed"]!r}', flush=True)
                    continue
                self.stats['fixes'] += 1
                self.emit({'type': 'fix', 'id': ln['id'], 'text': ln['fixed']})
        for c in data.get('cards', []):
            term = (c.get('term') or '').strip()
            if not term or term.lower() in self.carded:
                continue
            if ' ' not in term and term.islower() and term in self.words:
                continue                 # "ticket", "token": everyday words, not jargon cards
            self.carded.add(term.lower())
            self.stats['cards'] += 1
            heard = (c.get('heard') or '').strip()
            said = ' '.join(t for _, t in batch).lower()
            if heard.lower() == term.lower() or heard.lower() not in said:
                heard = ''               # the model invents "heard as" text ("not mentioned yet")
            self.emit({'type': 'card', 'term': term, 'heard': heard,
                       'note': c.get('note', ''), 'aka': c.get('aka', []), 't': time.time()})

    def non_term_edits(self, raw, fixed):
        """Words the model changed that are NOT part of a term fix. The prompt says spelling
        only; this enforces it (10-12 mocks: wer_fixed > wer_raw = it was rephrasing)."""
        a, b = raw.split(), fixed.split()
        n = 0
        sm = difflib.SequenceMatcher(None, [w.lower().strip('.,?!') for w in a],
                                     [w.lower().strip('.,?!') for w in b])
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == 'equal':
                continue
            seg = ' '.join(b[j1:j2])
            if seg and (ACRO.search(seg) or self.vocab.hits(seg) or any(c.isupper() for c in seg[1:])):
                continue                 # the change introduces a term: that's the job
            n += max(i2 - i1, j2 - j1)
        return n

    async def close(self):
        await self._drop_client()


def _json(text):
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
