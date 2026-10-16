"""English word list for "is this a plain word?" checks. /usr/share/dict/words where it exists
(Debian's wamerican), else a tiny built-in fallback list (install `wamerican` for the real thing)."""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATHS = ('/usr/share/dict/words',)
_FALLBACK = ('the be to of and a in that have it for not on with he as you do at this but his by from they we say '
             'her she or an will my one all would there their what so up out if about who get which go me when '
             'make can like time no just him know take people into year your good some could them see other '
             'than then now look only come its over think also back after use two how our work first well way '
             'even new want because any these give day most us call team case switch group image update').split()
_WORDS = None


def words():
    global _WORDS
    if _WORDS is None:
        _WORDS = set(_FALLBACK)
        for p in _PATHS:
            if os.path.exists(p):
                with open(p, encoding='utf-8', errors='ignore') as f:
                    _WORDS = {w.strip().lower() for w in f}
                break
    return _WORDS
