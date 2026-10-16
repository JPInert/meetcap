"""Word list: base IT terms + ~/.config/meetcap/vocab.txt + terms learned from past
reviews + a per-call context file.

The list feeds three places: whisper's initial_prompt (spelling bias), the fixer's prefilter
(which lines are worth a model call) and the fixer's prompt (what to map mis-hearings onto).
"""
import os
import re
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, '..', 'vocab', 'base.txt')
SPOKEN = os.path.join(HERE, '..', 'vocab', 'spoken.txt')   # mis-hearings → term, all devices
DATA = os.path.expanduser('~/.local/share/meetcap')
LEARNED = os.path.join(DATA, 'vocab-learned.txt')      # written by `mt review`
CONFIG = os.path.expanduser('~/.config/meetcap')
USER_VOCAB = os.path.join(CONFIG, 'vocab.txt')          # your own terms, same line format as base.txt

ACRONYM = re.compile(r'\b[A-Z][A-Z0-9]{1,6}s?\b')


@dataclass
class Term:
    name: str
    aliases: list = field(default_factory=list)
    note: str = ''


class Vocab:
    def __init__(self, terms):
        self.terms = terms
        self.index = {}                  # lowercased key → Term
        self.caps = set()                # keys that are acronyms: matched case-sensitively
        for t in terms:
            for n in [t.name] + t.aliases:
                self.index[n.lower()] = t
                if n.isupper() and len(n) <= 6:
                    self.caps.add(n.lower())

    @classmethod
    def load(cls, context=None):
        terms, seen = [], set()

        def add(t):
            if t.name.lower() not in seen:
                seen.add(t.name.lower())
                terms.append(t)

        for path in (BASE, SPOKEN, USER_VOCAB, LEARNED):
            if os.path.exists(path):
                for t in parse(open(path, encoding='utf-8').read()):
                    add(t)
        # acronyms that appear in the --context file (agenda, notes)
        for path in (context,):
            if path and os.path.exists(path):
                txt = open(path, encoding='utf-8', errors='replace').read()
                for a in sorted(set(ACRONYM.findall(txt))):
                    if a.rstrip('s') not in ('I', 'A', 'OK', 'US', 'USA'):
                        add(Term(a.rstrip('s') if a.endswith('s') and a[:-1].isupper() else a))
        return cls(terms)

    def prompt(self, limit=900):
        """whisper initial_prompt: names only, most specific first (acronyms + multi-word),
        capped by characters — whisper's prompt window is ~224 tokens."""
        names = []
        for t in self.terms:
            names.append(t.name)
            names.extend(a for a in t.aliases if any(c.isupper() for c in a))
        out, n = [], 0
        for name in names:
            if n + len(name) + 2 > limit:
                break
            out.append(name)
            n += len(name) + 2
        return 'Technical call about IT support: ' + ', '.join(out) + '.'

    def hits(self, text):
        return list({id(t): t for _, t in self.hit_keys(text)}.values())

    def hit_keys(self, text):
        """Acronyms match case-sensitively ("IT" is not "it"), everything else ignores case."""
        low = text.lower()
        out = []
        for k, t in self.index.items():
            if k in self.caps:
                if re.search(r'(?<![A-Za-z0-9])' + re.escape(k.upper()) + r's?(?![A-Za-z0-9])', text):
                    out.append((k, t))
            elif re.search(r'(?<![a-z0-9])' + re.escape(k) + r'(?![a-z0-9])', low):
                out.append((k, t))
        return out

    def for_model(self):
        lines = []
        for t in self.terms:
            s = ' = '.join([t.name] + t.aliases)
            if t.note:
                s += ' :: ' + t.note
            lines.append(s)
        return '\n'.join(lines)


def parse(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        note = ''
        if '::' in line:
            line, note = (x.strip() for x in line.split('::', 1))
        names = [x.strip() for x in line.split('=') if x.strip()]
        out.append(Term(names[0], names[1:], note))
    return out
