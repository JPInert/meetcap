#!/usr/bin/env python3
"""Score `meetcap review` on the synthetic session against what the script put there."""
import json, sys
d = sys.argv[1]
r = json.load(open(d + '/review.json')); m = r['metrics']; llm = r.get('llm', {})
notes = open(d + '/notes.md').read().lower()
qa = m['qa']
def q_has(s): return [q for q in qa if s in q['question'].lower()]
checks = [
    ('5 answered questions (repeat merged)', m['questions'] == 5, m['questions']),
    ('1 repeat request', m['repeat_requests'] == 1, m['repeat_requests']),
    ('DMARC answered once, pause ~3.0 s', len(q_has('dmarc')) == 1 and abs(q_has('dmarc')[0]['pause_s'] - 3.0) <= 0.8,
     [q['pause_s'] for q in q_has('dmarc')]),
    ('update pause ~1.2 s', bool(q_has('update')) and abs(q_has('update')[0]['pause_s'] - 1.2) <= 0.8, [q['pause_s'] for q in q_has('update')]),
    ('outage pause ~1.5 s', bool(q_has('outage')) and abs(q_has('outage')[0]['pause_s'] - 1.5) <= 0.8, [q['pause_s'] for q in q_has('outage')]),
    ('tools pause ~0.9 s', any(abs(q['pause_s'] - 0.9) <= 0.8 for q in qa if 'used' in q['question'].lower()), [q['pause_s'] for q in qa if 'used' in q['question'].lower()]),
    ('commitment: RMM write-up', any('rmm' in p.lower() or 'write' in p.lower() for p in llm.get('commitments', [])), llm.get('commitments')),
    ('recap names Dana', 'dana' in notes, None),
    ('recap names Northwind', 'northwind' in notes, None),
    ('recap names Marco', 'marco' in notes, None),
    ('ServiceNow/NinjaOne spelled right in notes', 'servicenow' in notes and 'ninjaone' in notes, None),
]
k = sum(1 for _, ok, _ in checks if ok)
for name, ok, val in checks:
    print(('HIT ' if ok else 'MISS'), name, '' if val is None else val)
print(f'{k}/{len(checks)}')
