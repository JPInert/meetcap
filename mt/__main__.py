"""mt — meeting transcriber: live captions + jargon panel, post-call notes + review."""
import argparse
import asyncio
import glob
import os


def main():
    p = argparse.ArgumentParser(prog='meetcap', description=__doc__)
    sub = p.add_subparsers(dest='cmd', required=True)

    lv = sub.add_parser('live', help='tap the call, live captions + jargon panel')
    lv.add_argument('--name', help='label for the session dir, e.g. weekly-sync')
    lv.add_argument('--context', help='notes file (agenda, invite text): its acronyms join the word list')
    lv.add_argument('--sink', help='tap this sink\'s monitor instead of the default (e.g. earbuds)')
    lv.add_argument('--source', help='record this mic instead of the default')
    lv.add_argument('--no-me', action='store_true', help='do not record your mic')
    lv.add_argument('--model', default='base.en')
    lv.add_argument('--partial-model', default='tiny.en', help="rolling grey text; 'none' = off")
    lv.add_argument('--threads', type=int, default=6)
    lv.add_argument('--no-fixer', action='store_true', help='disable AI term help')
    lv.add_argument('--fixer-mode', choices=['smart', 'all'], default='smart',
                    help='smart = only lines with an acronym, unknown word, list term or tech context word')
    lv.add_argument('--lan', action='store_true',
                    help='serve the panel on the LAN (another device as 2nd screen). UNAUTHENTICATED: '
                         'anyone on the network can read the live transcript')
    lv.add_argument('--port', type=int, default=7797)
    lv.add_argument('--no-voice-gate', dest='voice_gate', action='store_false',
                    help='leave the MEETCAP_VOICE_UNIT service running (desktop, only matters if that variable is set)')
    lv.add_argument('--out', help=argparse.SUPPRESS)
    lv.add_argument('-v', '--verbose', action='store_true')

    mk = sub.add_parser('mock', help='unattended test on a null sink, scored against human captions')
    mk.add_argument('audio')
    mk.add_argument('vtt')
    mk.add_argument('--seconds', type=int, help='only the first N seconds')
    mk.add_argument('--terms', default='', help='extra comma-separated terms to score (for lowercase subs)')
    mk.add_argument('--context')
    mk.add_argument('--model', default='base.en')
    mk.add_argument('--partial-model', default='tiny.en')
    mk.add_argument('--threads', type=int, default=6)
    mk.add_argument('--load', type=int, default=4, help='synthetic x264 load threads (0 = none)')
    mk.add_argument('--no-fixer', action='store_true')
    mk.add_argument('--fixer-mode', choices=['smart', 'all'], default='smart')
    mk.add_argument('--port', type=int, default=7798)
    mk.add_argument('-v', '--verbose', action='store_true')

    rv = sub.add_parser('review', help='after the call: transcript, notes, action items, timing metrics, history')
    rv.add_argument('session')
    rv.add_argument('--model', default='large-v3-turbo', help='whisper model for the full transcript')
    rv.add_argument('--threads', type=int, default=8)
    rv.add_argument('--beam', type=int, default=5, help='2 = faster, slightly less accurate')
    rv.add_argument('--no-llm', action='store_true', help='transcript + timing metrics only')
    rv.add_argument('--no-history', action='store_true', help='do not append to history / word list')

    sub.add_parser('widget', help='desktop window: start/stop, settings, live captions, review')

    wr = sub.add_parser('wrap', help='turn a crashed session\'s raw .s16le files into .wav')
    wr.add_argument('session')

    a = p.parse_args()
    if a.cmd == 'live':
        from .live import Live
        asyncio.run(Live(a).run())
    elif a.cmd == 'mock':
        from . import mock
        mock.run(a)
    elif a.cmd == 'review':
        from . import review
        review.run(a)
    elif a.cmd == 'widget':
        from . import widget
        widget.main()
    elif a.cmd == 'wrap':
        from .tap import wrap
        for raw in glob.glob(os.path.join(a.session, '*.s16le')):
            print(wrap(raw))


if __name__ == '__main__':
    main()
