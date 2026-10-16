"""Optional, desktop only: keep a voice-assistant user service out of the call.

If you run a text-to-speech announcer as a systemd user service, it would speak into the call
through the speaker and onto the THEM track. Set MEETCAP_VOICE_UNIT to that unit name (for
example `my-voice.service`) and the service is stopped for the call. A transient systemd timer
to start it again is armed FIRST, so a crash of this process cannot leave it off. With the
variable unset this does nothing.
"""
import os
import subprocess

UNIT = os.environ.get('MEETCAP_VOICE_UNIT', '')
SAFETY = 'mt-voice-restore'


def _sc(*args):
    return subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=15)


class VoiceGate:
    def __init__(self, hours=3):
        self.hours = hours
        self.stopped = False

    def engage(self):
        if not UNIT or _sc('is-active', UNIT).stdout.strip() != 'active':
            return None
        _sc('stop', SAFETY + '.timer')             # a stale one from a previous crash…
        _sc('reset-failed', SAFETY + '.timer', SAFETY + '.service')   # …or systemd-run says "already exists"
        armed = subprocess.run(
            ['systemd-run', '--user', f'--unit={SAFETY}', f'--on-active={self.hours}h',
             'systemctl', '--user', 'start', UNIT], capture_output=True, text=True, timeout=15)
        if armed.returncode != 0:                  # no safety net → don't stop it
            return f'voice service LEFT ON: could not arm restore timer ({armed.stderr.strip()[:120]})'
        r = _sc('stop', UNIT)
        if r.returncode != 0:
            return f'voice service LEFT ON: stop failed ({r.stderr.strip()[:120]})'
        self.stopped = True
        return f'voice service off until mt exits (auto-restore in {self.hours} h)'

    def release(self):
        if not self.stopped:
            return None
        r = _sc('start', UNIT)
        _sc('stop', SAFETY + '.timer')
        self.stopped = False
        return 'voice service back on' if r.returncode == 0 else f'voice service restart FAILED: {r.stderr.strip()[:120]}'
