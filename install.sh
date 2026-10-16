#!/bin/bash
# meetcap install — Debian/Ubuntu desktop or laptop (X11 or Wayland). Idempotent.
# Never runs sudo itself: it prints the apt line for anything missing.
#   ./install.sh            check deps, link the command, add the menu entry
#   ./install.sh --check    only report what is missing
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
CHECK_ONLY=${1:-}
missing_apt=()
missing_pip=()

have_deb() { dpkg -s "$1" >/dev/null 2>&1; }
for p in python3-gi gir1.2-gtk-3.0 pulseaudio-utils ffmpeg wamerican xdg-utils python3-pip; do
    have_deb "$p" || missing_apt+=("$p")
done
# numpy/aiohttp: Debian's packages, unless a pip copy is already importable
python3 -c 'import numpy'   2>/dev/null || missing_apt+=(python3-numpy)
python3 -c 'import aiohttp' 2>/dev/null || missing_apt+=(python3-aiohttp)
# pactl/parec on PipeWire systems come from pulseaudio-utils too (pipewire-pulse is the server)
python3 -c 'import faster_whisper' 2>/dev/null || missing_pip+=(faster-whisper)
python3 -c 'import claude_agent_sdk' 2>/dev/null || missing_pip+=(claude-agent-sdk)

[ ${#missing_apt[@]} -gt 0 ] && echo "MISSING apt:  sudo apt install ${missing_apt[*]}"
[ ${#missing_pip[@]} -gt 0 ] && echo "MISSING pip:  python3 -m pip install --user --break-system-packages ${missing_pip[*]}"
python3 -c 'import claude_agent_sdk' 2>/dev/null && ! [ -s ~/.claude/.credentials.json ] && \
    echo "NOTE: no Claude login found — 'AI term help' needs one (run claude once); captions work without it"
[ "$CHECK_ONLY" = "--check" ] && exit 0

chmod +x "$DIR/bin/meetcap"
mkdir -p ~/.local/bin ~/.local/share/applications ~/.local/share/icons/hicolor/256x256/apps
ln -sfn "$DIR/bin/meetcap" ~/.local/bin/meetcap
cp "$DIR/share/meetcap.png" ~/.local/share/icons/hicolor/256x256/apps/meetcap.png
sed "s#@BIN@#$HOME/.local/bin/meetcap#" "$DIR/share/meetcap.desktop" > ~/.local/share/applications/meetcap.desktop
command -v update-desktop-database >/dev/null && update-desktop-database ~/.local/share/applications 2>/dev/null
echo "linked ~/.local/bin/meetcap, menu entry 'meetcap' (Office/AudioVideo)"

# speech models download on first use; fetch them now so the first call doesn't wait
if [ ${#missing_pip[@]} -eq 0 ]; then
    python3 - <<'EOF'
from faster_whisper.utils import download_model
for m in ('tiny.en', 'base.en'):
    download_model(m)
print('speech models ready (tiny.en, base.en); large-v3-turbo downloads on the first review')
EOF
fi
