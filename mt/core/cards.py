"""Word-list term cards: instant, no model."""
from .words import words


class ListCards:
    def __init__(self, vocab):
        self.vocab = vocab
        self.carded = set()

    def scan(self, text):
        """Cards for list terms in `text` not carded yet: [{term, note, aka}]."""
        out = []
        common = words()
        for key, t in self.vocab.hit_keys(text):
            if t.name.lower() in self.carded or not (t.note or t.aliases):
                continue
            orig = next((n for n in [t.name] + t.aliases if n.lower() == key), key)
            if ' ' not in orig and orig.islower() and key in common:
                continue                 # "case", "request", "ticket": too common to card on sight
            self.carded.update(n.lower() for n in [t.name] + t.aliases)
            heard = orig if orig.lower() != t.name.lower() and orig.islower() else ''
            # aka = real other names; lowercase non-words ("tachax") are mis-hearings, not names
            aka = [a for a in t.aliases if a.lower() != key
                   and not (' ' not in a and a.islower() and a.lower() not in common)]
            out.append({'term': t.name, 'note': t.note, 'aka': aka,
                        'heard': heard, 'src': 'list'})
        return out
