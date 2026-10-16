# Measured results

These are the only numbers the README quotes. They come from the author's private working log
(RESULTS.md in the private repo, entries dated 2026-10-12), copied here with their sample sizes.
The audio clip behind them is a public jargon-dense security training video with human captions;
it is not redistributed (see `meetcap mock`). All runs used a NULL audio sink with synthetic CPU
load, so this is speech-to-text on a clean recording, not a live call.

## Live captions (base.en commits + tiny.en partials)

| machine | date | clip | lines (n) | WER | terms right (raw) | commit lag median / p90 / max |
|---|---|---|---|---|---|---|
| desktop (12 threads, busy: load average 9-14, 4-thread synthetic encode) | 2026-10-12 | 369 s, 1116 words | 53 | 8.3 % | 38/44 | 2.8 / 3.7 / 6.6 s |
| tablet (Core Ultra 5 134U, Debian 13, PipeWire), 2-thread synthetic encode | 2026-10-12 | same clip | 53 | 6.7 % | 43/49 | 1.6 / 2.5 / 2.8 s |

The two rows are not like for like: different machine, different background load.
Lag = time from the end of the spoken line to the caption being final.
Per speech-to-text call on the tablet: base.en 0.77 s median, tiny.en 0.32 s.

## Review (`meetcap review`, synthetic two-track call, scripted timeline, TTS voices)

`mock/check_review.py` against the script: 12 of 13 checks hit (2026-10-12). Caveat: this was measured on an earlier version of the mock script and check list (different wording and TTS voices, and 13 checks, some of which covered features removed from this public copy). The shipped script and its 11 checks have not been re-run. Pauses before answers were measured 0.1 to 0.4 s high. The miss: a TTS voice was transcribed as a different name.
Review speed: large-v3-turbo int8, 8 threads, 59 s for 2 x 88 s of audio.

## Known issues seen

- A line cut mid-word sometimes becomes an invented sentence (1-2 of 53 lines in 2 of 4 runs).
- Acronyms spoken as letters ("triple A" for AAA) are rarely caught.
- The term fixer sometimes makes a card for a name instead of jargon.
