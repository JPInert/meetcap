"""`meetcap widget`: a desktop window with Start/Stop, settings, the live captions + term cards,
and Review afterwards.

Portable on purpose (desktop XFCE/X11 and Plasma/Wayland): plain GTK3 from
python3-gi, a normal top-level window (no POPUP windows, no xdotool, no WebKit), big touch-sized
buttons, nothing from ~/dotfiles. It runs `meetcap live` as a child process and reads the same
event stream the web panel uses, so the widget and a browser can watch one session at once.
"Keep on top" works on X11; on Wayland KWin ignores it — use a KWin window rule instead.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.expanduser('~/.config/meetcap/widget.json')
QUESTION_START = ('what', 'how', 'why', 'when', 'where', 'which', 'who', 'can you', 'could you',
                  'tell me', 'walk me', 'describe', 'have you', 'do you', 'did you', 'would you',
                  'give me')

CSS = b"""
window.meetcap { background: #0e0e12; color: #f2f2f2; }
.meetcap label { color: #f2f2f2; }
.meetcap .dim { color: #8a8a96; }
.meetcap .head { font-size: 11px; letter-spacing: 1px; color: #8a8a96; font-weight: bold; }
.meetcap button, .meetcap combobox button, .meetcap filechooserbutton button {
    padding: 6px 12px; border-radius: 8px; background-image: none; background-color: #23232d;
    color: #f2f2f2; border: 1px solid #34343f; box-shadow: none; text-shadow: none; }
.meetcap button:hover { background-color: #2e2e3a; }
.meetcap button:disabled, .meetcap button:disabled label { color: #5a5a66; background-color: #17171d; }
.meetcap entry { background-color: #1b1b24; color: #f2f2f2; border: 1px solid #34343f; border-radius: 8px; }
.meetcap checkbutton label { color: #d6d6de; }
/* after the generic button rule so they win */
.meetcap button.start { background-color: #b48cff; color: #000; font-weight: bold; font-size: 18px; min-height: 44px; }
.meetcap button.stop { background-color: #ff6b6b; color: #000; font-weight: bold; font-size: 18px; min-height: 44px; }
.meetcap button.repeat { background-color: #b48cff; color: #000; font-weight: bold; font-size: 16px; min-height: 40px; }
.meetcap button.start label, .meetcap button.stop label, .meetcap button.repeat label { color: #000; }
.meetcap .card { background: #1b1b24; border-radius: 8px; padding: 6px 8px; }
.meetcap .gcard { background: #22183a; border-left: 4px solid #b48cff; border-radius: 8px; padding: 6px 10px; }
.meetcap .term { color: #b48cff; font-weight: bold; font-size: 16px; }
.meetcap .warn { background: #3a2a10; color: #ffb347; padding: 6px 10px; }
.meetcap .consent { background: #2a2140; padding: 6px 10px; }
.meetcap textview, .meetcap textview text { background: #0e0e12; color: #f2f2f2; }
.meetcap .bigline { font-size: 34px; }
"""


def pactl_list(kind):
    """[(name, description)] for 'sinks' or 'sources' (monitors left out of sources)."""
    try:
        out = subprocess.run(['pactl', '-f', 'json', 'list', kind], capture_output=True,
                             text=True, timeout=5).stdout
        rows = json.loads(out or '[]')
    except Exception:
        return []
    return [(r['name'], r.get('description') or r['name']) for r in rows
            if not (kind == 'sources' and r['name'].endswith('.monitor'))]


def pactl_default(kind):
    try:
        return subprocess.run(['pactl', f'get-default-{kind}'], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return ''


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def lan_ip():
    """This computer's LAN address (no packet is sent: UDP connect only picks the route)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('203.0.113.1', 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return '127.0.0.1'


def has_house_voice():
    from .gate import UNIT
    if not UNIT:
        return False
    r = subprocess.run(['systemctl', '--user', 'cat', UNIT],
                       capture_output=True, text=True)
    return r.returncode == 0


def load_settings():
    try:
        return json.load(open(SETTINGS))
    except Exception:
        return {}


def save_settings(d):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    json.dump(d, open(SETTINGS, 'w'), indent=1)


class Widget(Gtk.Window):
    def __init__(self):
        super().__init__(title='meetcap')
        self.get_style_context().add_class('meetcap')
        self.set_default_size(980, 640)
        self.set_wmclass('meetcap', 'meetcap')
        icon = os.path.join(REPO, 'share', 'meetcap.png')
        if os.path.exists(icon):
            self.set_icon_from_file(icon)
        self.connect('delete-event', self.on_close)
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.st = load_settings()
        self.proc = None
        self.session = None
        self.port = None
        self.started = None
        self.sse_gen = 0                 # bumps per run so an old SSE thread stops writing
        self.lines = {}                  # id → text
        self.order = []
        self.terms = []
        self.carded = set()
        self.font = self.st.get('font', 22)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        root.pack_start(self.build_controls(), False, False, 0)
        self.consent = Gtk.Box(spacing=8)
        self.consent.get_style_context().add_class('consent')
        self.consent.pack_start(Gtk.Label(label='Say first: "Mind if I use a transcription tool for my notes?"',
                                          xalign=0), True, True, 0)
        done = Gtk.Button(label='done')
        done.connect('clicked', lambda *_: self.consent.hide())
        self.consent.pack_end(done, False, False, 0)
        self.consent.set_no_show_all(True)
        root.pack_start(self.consent, False, False, 0)
        self.warnbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.pack_start(self.warnbox, False, False, 0)
        root.pack_start(self.build_body(), True, True, 0)
        root.pack_start(self.build_footer(), False, False, 0)
        GLib.timeout_add_seconds(1, self.tick)
        self.set_running(False)

    # ---------- layout ----------
    def build_controls(self):
        g = Gtk.Grid(column_spacing=8, row_spacing=6, margin=8)
        self.btn = Gtk.Button(label='Start')
        self.btn.connect('clicked', self.on_start_stop)
        self.btn.set_size_request(130, -1)
        g.attach(self.btn, 0, 0, 1, 2)

        self.name = Gtk.Entry(placeholder_text='session name, e.g. weekly-sync')
        self.name.set_hexpand(True)
        g.attach(self.name, 1, 0, 1, 1)
        self.ctx = Gtk.FileChooserButton(title='Agenda / notes (optional)')
        self.ctx.set_width_chars(14)
        self.ctx.set_tooltip_text('Agenda or notes: its acronyms join the word list')
        if self.st.get('context') and os.path.exists(self.st['context']):
            self.ctx.set_filename(self.st['context'])
        g.attach(self.ctx, 3, 0, 1, 1)

        self.sink = Gtk.ComboBoxText()
        self.source = Gtk.ComboBoxText()
        for c in (self.sink, self.source):             # long device names must not widen the window
            cell = c.get_cells()[0]
            cell.set_property('ellipsize', Pango.EllipsizeMode.END)
            cell.set_property('width-chars', 18)
            c.set_hexpand(True)
        self.fill_devices()
        refresh = Gtk.Button(label='↻')
        refresh.set_tooltip_text('re-read audio devices (plugged in earbuds?)')
        refresh.connect('clicked', lambda *_: self.fill_devices())
        lab1 = Gtk.Label(label='Their audio', xalign=1)
        lab1.get_style_context().add_class('dim')
        lab2 = Gtk.Label(label='Your mic', xalign=1)
        lab2.get_style_context().add_class('dim')
        dev = Gtk.Box(spacing=6)
        dev.pack_start(lab1, False, False, 0)
        dev.pack_start(self.sink, True, True, 0)
        dev.pack_start(lab2, False, False, 0)
        dev.pack_start(self.source, True, True, 0)
        dev.pack_start(refresh, False, False, 0)
        g.attach(dev, 1, 1, 3, 1)

        opts = Gtk.Box(spacing=12)
        self.fixer = Gtk.CheckButton(label='AI term help')
        self.fixer.set_tooltip_text('Haiku fixes mis-heard terms and adds term cards (uses model quota)')
        self.fixer.set_active(self.st.get('fixer', True))
        opts.pack_start(self.fixer, False, False, 0)
        self.voice = Gtk.CheckButton(label='Silence voice service')
        self.voice.set_tooltip_text('Stop the MEETCAP_VOICE_UNIT service for the call so announcements don\'t speak into it')
        self.voice.set_active(self.st.get('voice_gate', True))
        self.voice.set_no_show_all(not has_house_voice())
        opts.pack_start(self.voice, False, False, 0)
        self.lan = Gtk.CheckButton(label='2nd screen (LAN)')
        self.lan.set_tooltip_text('Serve the web panel on the LAN. No login: anyone on the network can read it')
        self.lan.set_active(self.st.get('lan', False))
        opts.pack_start(self.lan, False, False, 0)
        self.ontop = Gtk.CheckButton(label='Keep on top')
        self.ontop.set_active(self.st.get('ontop', False))
        self.ontop.connect('toggled', lambda b: self.set_keep_above(b.get_active()))
        self.set_keep_above(self.ontop.get_active())
        opts.pack_start(self.ontop, False, False, 0)
        g.attach(opts, 1, 2, 3, 1)
        return g

    def build_body(self):
        pan = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        pan.set_position(680)
        # captions
        self.tv = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                               left_margin=14, right_margin=14, top_margin=8, bottom_margin=8)
        self.buf = self.tv.get_buffer()
        self.t_old = self.buf.create_tag('old', foreground='#c9c9d1')
        self.t_term = self.buf.create_tag('term', foreground='#b48cff', weight=Pango.Weight.BOLD)
        self.t_partial = self.buf.create_tag('partial', foreground='#8a8a96', style=Pango.Style.ITALIC)
        self.t_fixed = self.buf.create_tag('fixed', foreground='#b48cff', scale=0.6)
        self.pmark = self.buf.create_mark('partial', self.buf.get_end_iter(), True)   # grey text lives after it
        self.set_font(self.font, save=False)
        sw = Gtk.ScrolledWindow()
        sw.add(self.tv)
        self.cap_sw = sw
        pan.pack1(sw, True, False)
        # side: cards + questions
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=6)
        h1 = Gtk.Label(label='TERMS', xalign=0)
        h1.get_style_context().add_class('head')
        side.pack_start(h1, False, False, 0)
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        csw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        csw.add(self.cards)
        side.pack_start(csw, True, True, 0)
        h2 = Gtk.Label(label='QUESTIONS ASKED', xalign=0)
        h2.get_style_context().add_class('head')
        side.pack_start(h2, False, False, 0)
        self.qs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        qsw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        qsw.set_size_request(-1, 160)
        qsw.add(self.qs)
        side.pack_start(qsw, False, True, 0)
        pan.pack2(side, False, False)
        return pan

    def build_footer(self):
        f = Gtk.Box(spacing=8, margin=6)
        self.rep = Gtk.Button(label='What did they say?')
        self.rep.get_style_context().add_class('repeat')
        self.rep.connect('clicked', self.on_repeat)
        f.pack_start(self.rep, False, False, 0)
        for lab, d in (('A−', -2), ('A+', 2)):
            b = Gtk.Button(label=lab)
            b.set_tooltip_text('caption text size')
            b.connect('clicked', lambda _b, d=d: self.set_font(self.font + d))
            f.pack_start(b, False, False, 0)
        self.status = Gtk.Label(label='', xalign=0)
        self.status.get_style_context().add_class('dim')
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        f.pack_start(self.status, True, True, 0)
        self.web = Gtk.Button(label='Web panel')
        self.web.connect('clicked', lambda *_: self.port and subprocess.Popen(
            ['xdg-open', f'http://127.0.0.1:{self.port}/']))
        f.pack_end(self.web, False, False, 0)
        self.metrics = Gtk.Button(label='Metrics')
        self.metrics.connect('clicked', lambda *_: self.open_file('metrics.md'))
        f.pack_end(self.metrics, False, False, 0)
        self.notes = Gtk.Button(label='Notes')
        self.notes.connect('clicked', lambda *_: self.open_file('notes.md'))
        f.pack_end(self.notes, False, False, 0)
        self.review = Gtk.Button(label='Review')
        self.review.set_tooltip_text('Full transcript of both sides, notes and timing metrics (takes ~half the call length)')
        self.review.connect('clicked', self.on_review)
        f.pack_end(self.review, False, False, 0)
        self.connect('key-press-event', self.on_key)
        return f

    # ---------- helpers ----------
    def fill_devices(self):
        for combo, kind, default, saved in ((self.sink, 'sinks', pactl_default('sink'), self.st.get('sink')),
                                            (self.source, 'sources', pactl_default('source'), self.st.get('source'))):
            combo.remove_all()
            combo.append('__default__', 'System default')
            names = []
            for name, desc in pactl_list(kind):
                combo.append(name, desc + ('  (default)' if name == default else ''))
                names.append(name)
            combo.set_active_id(saved if saved in names else '__default__')

    def set_font(self, size, save=True):
        self.font = max(14, min(48, size))
        self.tv.override_font(Pango.FontDescription(f'Sans {self.font}'))
        if save:
            self.st['font'] = self.font
            save_settings(self.st)

    def set_running(self, running):
        ctx = self.btn.get_style_context()
        ctx.remove_class('start' if running else 'stop')
        ctx.add_class('stop' if running else 'start')
        self.btn.set_label('Stop' if running else 'Start')
        for w in (self.name, self.ctx, self.sink,
                  self.source, self.fixer, self.voice, self.lan):
            w.set_sensitive(not running)
        self.web.set_sensitive(running)
        have = bool(self.session and os.path.isdir(self.session))
        self.review.set_sensitive(not running and have and os.path.exists(os.path.join(self.session, 'session.json')))
        self.notes.set_sensitive(have and os.path.exists(os.path.join(self.session or '', 'notes.md')))
        self.metrics.set_sensitive(have and os.path.exists(os.path.join(self.session or '', 'metrics.md')))
        if running:
            self.consent.set_no_show_all(False)
            self.consent.show_all()
            self.consent.set_no_show_all(True)

    def say(self, text):
        self.base_status = text
        self.status.set_text(text)

    def open_file(self, name):
        if self.session:
            p = os.path.join(self.session, name)
            if os.path.exists(p):
                subprocess.Popen(['xdg-open', p])

    def tick(self):
        if self.proc and self.started:
            el = int(time.time() - self.started)
            self.status.set_text(f'{el // 60:02d}:{el % 60:02d}  ·  {getattr(self, "base_status", "")}')
        return True

    # ---------- start / stop ----------
    def on_start_stop(self, *_):
        if self.proc:
            self.stop()
        else:
            self.start()

    def start(self):
        self.clear()
        # LAN: a fixed port, so a bookmark on another device keeps working; local only: any free port
        self.port = 7797 if self.lan.get_active() else free_port()
        args = [sys.executable, '-m', 'mt', 'live', '--port', str(self.port)]
        name = self.name.get_text().strip().replace(' ', '-')
        if name:
            args += ['--name', name]
        ctx = self.ctx.get_filename()
        if ctx:
            args += ['--context', ctx]
        if self.sink.get_active_id() not in (None, '__default__'):
            args += ['--sink', self.sink.get_active_id()]
        if self.source.get_active_id() not in (None, '__default__'):
            args += ['--source', self.source.get_active_id()]
        if not self.fixer.get_active():
            args.append('--no-fixer')
        if not (self.voice.get_visible() and self.voice.get_active()):
            args.append('--no-voice-gate')
        if self.lan.get_active():
            args.append('--lan')
        args += os.environ.get('MEETCAP_EXTRA', '').split()          # test hook
        self.st.update({'context': ctx, 'sink': self.sink.get_active_id(), 'source': self.source.get_active_id(),
                        'fixer': self.fixer.get_active(), 'voice_gate': self.voice.get_active(),
                        'lan': self.lan.get_active(), 'ontop': self.ontop.get_active()})
        save_settings(self.st)
        self.session = None
        self.proc = subprocess.Popen(args, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, start_new_session=True)
        self.started = time.time()
        self.set_running(True)
        self.say('loading speech models …')
        self.sse_gen += 1
        threading.Thread(target=self.read_proc, args=(self.proc, self.sse_gen), daemon=True).start()
        threading.Thread(target=self.wait_proc, args=(self.proc,), daemon=True).start()

    def read_proc(self, proc, gen):
        for line in proc.stdout:
            line = line.rstrip()
            if 'session ' in line and '[mt] panel' in line:
                self.session = line.split('session ', 1)[1].strip()
                threading.Thread(target=self.read_sse, args=(gen,), daemon=True).start()
            if line.startswith('[mt]'):
                GLib.idle_add(self.say, line[5:])
            if line.startswith('[mt] saved '):
                self.session = line[len('[mt] saved '):].strip()

    def wait_proc(self, proc):
        # exit is detected by wait(), not by stdout EOF: a grandchild that inherited the pipe
        # (the fixer's CLI) kept EOF from ever arriving and the window sat on "stopping …"
        rc = proc.wait()
        GLib.idle_add(self.on_exited, rc)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.say('stopping — finishing the last lines …')
            self.btn.set_sensitive(False)
            os.kill(self.proc.pid, signal.SIGINT)   # main process only: SIGINT to the group also killed the claude CLI child and stop hung
            print(f'[widget] stop sent at {time.strftime("%T")}', flush=True)

    def on_exited(self, rc):
        print(f'[widget] meetcap exited rc={rc} at {time.strftime("%T")}', flush=True)
        self.proc = None
        self.started = None
        self.btn.set_sensitive(True)
        self.set_running(False)
        if self.session and os.path.exists(os.path.join(self.session, 'session.json')):
            self.say('saved ' + self.session + ' — Review when you are ready')
        else:
            self.say(f'meetcap exited ({rc}) before saving — run "meetcap wrap" on the session folder')
        self.set_running(False)
        if getattr(self, 'auto_review', False) and self.session:
            self.on_review()

    def on_close(self, *_):
        if self.proc and self.proc.poll() is None:
            d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                  buttons=Gtk.ButtonsType.YES_NO, text='A recording is running. Stop it and close?')
            ok = d.run() == Gtk.ResponseType.YES
            d.destroy()
            if not ok:
                return True
            os.kill(self.proc.pid, signal.SIGINT)   # main process only: SIGINT to the group also killed the claude CLI child and stop hung
            try:
                self.proc.wait(90)
            except subprocess.TimeoutExpired:
                pass
        Gtk.main_quit()
        return False

    # ---------- event stream ----------
    def read_sse(self, gen):
        url = f'http://127.0.0.1:{self.port}/events'
        while gen == self.sse_gen and self.proc:
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    for raw in r:
                        if gen != self.sse_gen:
                            return
                        raw = raw.decode('utf-8', 'replace').strip()
                        if raw.startswith('data: '):
                            GLib.idle_add(self.handle, json.loads(raw[6:]))
            except Exception:
                time.sleep(1)

    def handle(self, e):
        t = e.get('type')
        if t == 'reset':
            self.clear()
        elif t == 'partial':
            self.set_partial(e.get('text', ''))
        elif t == 'commit':
            self.lines[e['id']] = (e['text'], False)
            self.order.append(e['id'])
            self.set_partial('')
            self.render()
            if e['text'].rstrip().endswith('?') or e['text'].strip().lower().startswith(QUESTION_START):
                self.add_question(e['id'], e['text'])
        elif t == 'fix' and e.get('id') in self.lines:
            self.lines[e['id']] = (e['text'], True)
            self.render()
            q = getattr(self, 'qlabels', {}).get(e['id'])
            if q:
                q.set_text(e['text'])
        elif t == 'card':
            self.add_card(e)
        elif t == 'status':
            if e.get('terms'):
                self.terms = e['terms']
            if e.get('text') == 'live':
                second = f' · 2nd screen: http://{lan_ip()}:{self.port}/' if self.lan.get_active() else ''
                self.say('live' + second + (f" · {e['voice']}" if e.get('voice') else ''))
        elif t == 'warn':
            for c in self.warnbox.get_children():
                c.destroy()
            for m in e.get('items', []):
                lab = Gtk.Label(label='⚠ ' + m, xalign=0)
                lab.set_line_wrap(True)
                lab.get_style_context().add_class('warn')
                self.warnbox.pack_start(lab, False, False, 0)
                lab.show()
        return False

    def clear(self):
        self.lines, self.order, self.carded = {}, [], set()
        self.qlabels = {}
        self.buf.set_text('')
        for box in (self.cards, self.qs, self.warnbox):
            for c in box.get_children():
                c.destroy()

    def render(self):
        """Last ~40 lines; newest two at full colour, older ones dimmer; terms purple."""
        self.buf.set_text('')
        ids = self.order[-40:]
        for i, cid in enumerate(ids):
            text, fixed = self.lines[cid]
            end = self.buf.get_end_iter()
            start_off = end.get_offset()
            self.buf.insert(end, text)
            if i < len(ids) - 2:
                self.buf.apply_tag(self.t_old, self.buf.get_iter_at_offset(start_off), self.buf.get_end_iter())
            self.tag_terms(start_off, text)
            if fixed:
                self.buf.insert_with_tags(self.buf.get_end_iter(), '  fixed', self.t_fixed)
            self.buf.insert(self.buf.get_end_iter(), '\n')
        self.buf.move_mark(self.pmark, self.buf.get_end_iter())
        self.scroll_end()

    def tag_terms(self, off, text):
        low = text.lower()
        for term in self.terms + sorted(self.carded):
            caps = term.isupper()
            hay, needle = (text, term) if caps else (low, term.lower())
            i = hay.find(needle)
            while i >= 0:
                before = hay[i - 1] if i else ' '
                after = hay[i + len(needle)] if i + len(needle) < len(hay) else ' '
                if not before.isalnum() and not after.isalnum():
                    self.buf.apply_tag(self.t_term, self.buf.get_iter_at_offset(off + i),
                                       self.buf.get_iter_at_offset(off + i + len(needle)))
                i = hay.find(needle, i + 1)

    def set_partial(self, text):
        self.buf.delete(self.buf.get_iter_at_mark(self.pmark), self.buf.get_end_iter())
        if text:
            self.buf.insert_with_tags(self.buf.get_end_iter(), text, self.t_partial)
        self.scroll_end()

    def scroll_end(self):
        adj = self.cap_sw.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper() - adj.get_page_size()) and False)

    def add_card(self, e):
        self.carded.add(e['term'])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.get_style_context().add_class('card')
        term = Gtk.Label(label=e['term'], xalign=0)
        term.get_style_context().add_class('term')
        box.pack_start(term, False, False, 0)
        if e.get('heard') and e['heard'].lower() != e['term'].lower():
            h = Gtk.Label(label=f"heard as “{e['heard']}”", xalign=0)
            h.get_style_context().add_class('dim')
            box.pack_start(h, False, False, 0)
        if e.get('note'):
            n = Gtk.Label(label=e['note'], xalign=0)
            n.set_line_wrap(True)
            box.pack_start(n, False, False, 0)
        if e.get('aka'):
            a = Gtk.Label(label='aka ' + ', '.join(e['aka']), xalign=0)
            a.set_line_wrap(True)
            a.get_style_context().add_class('dim')
            box.pack_start(a, False, False, 0)
        self.cards.pack_start(box, False, False, 0)
        self.cards.reorder_child(box, 0)          # newest on top
        box.show_all()

    def add_question(self, cid, text):
        lab = Gtk.Label(label=f'{len(self.qs.get_children()) + 1}. {text}', xalign=0)
        lab.set_line_wrap(True)
        self.qs.pack_start(lab, False, False, 0)
        lab.show()
        self.qlabels = getattr(self, 'qlabels', {})
        self.qlabels[cid] = lab

    # ---------- repeat / review ----------
    def on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_space and not self.name.has_focus():
            self.on_repeat()
            return True
        return False

    def on_repeat(self, *_):
        if not self.order:
            return
        text = self.lines[self.order[-1]][0]
        d = Gtk.Window(title='They said', transient_for=self, modal=True)
        d.get_style_context().add_class('meetcap')
        d.set_default_size(820, 300)
        lab = Gtk.Label(label=text, wrap=True, margin=30)
        lab.override_font(Pango.FontDescription('Sans 34'))
        ev = Gtk.EventBox()
        ev.add(lab)
        ev.connect('button-press-event', lambda *_: d.destroy())
        d.connect('key-press-event', lambda *_: d.destroy())
        d.add(ev)
        d.show_all()

    def on_review(self, *_):
        if not self.session:
            return
        self.review.set_sensitive(False)
        self.btn.set_sensitive(False)
        t0 = time.time()
        self.say('review running — full transcript of both sides (about half the call length) …')

        def work():
            r = subprocess.run([sys.executable, '-m', 'mt', 'review', self.session], cwd=REPO,
                               capture_output=True, text=True)
            GLib.idle_add(self.review_done, r.returncode, (r.stdout + r.stderr).strip().splitlines()[-1:] or [''],
                          int(time.time() - t0))
        threading.Thread(target=work, daemon=True).start()

    def review_done(self, rc, last, secs):
        self.btn.set_sensitive(True)
        self.set_running(False)
        self.say(f'review done in {secs // 60} min — Notes / Metrics' if rc == 0 else f'review failed: {last[0][:160]}')
        if rc == 0:
            self.open_file('notes.md')


def main():
    GLib.set_prgname('meetcap')          # Wayland app-id → matches meetcap.desktop (icon, KWin rules)
    w = Widget()
    w.show_all()
    # test hooks (unattended runs under Xvfb): MEETCAP_AUTOSTART=1, MEETCAP_AUTOSTOP=<s after start>
    if os.environ.get('MEETCAP_AUTOSTART'):
        w.voice.set_active(False)        # a test run must never stop the voice service
        GLib.timeout_add(500, lambda: w.start() and False)
        if os.environ.get('MEETCAP_AUTOREVIEW'):
            w.auto_review = True
        if os.environ.get('MEETCAP_AUTOSTOP'):
            GLib.timeout_add_seconds(int(os.environ['MEETCAP_AUTOSTOP']), lambda: w.stop() and False)
    Gtk.main()
