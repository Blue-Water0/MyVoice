#!/usr/bin/env bash
# MyVoice — Linux Mint installer.
# Idempotent. Prints apt commands and asks for confirmation before sudo.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# ---- Python version check ------------------------------------------------
if ! command -v "$PYTHON_BIN" >/dev/null; then
    echo "ERROR: python3 not found." >&2
    exit 1
fi
PYV=$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PYMAJ=$("$PYTHON_BIN" -c 'import sys; print(sys.version_info[0])')
PYMIN=$("$PYTHON_BIN" -c 'import sys; print(sys.version_info[1])')

if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 11 ]; }; then
    echo "ERROR: Python 3.11+ required (found $PYV). On Mint 21 install python3.11 via deadsnakes or use Mint 22." >&2
    exit 1
fi
echo "==> Using Python $PYV ($("$PYTHON_BIN" -c 'import sys; print(sys.executable)'))"

# ---- APT deps ------------------------------------------------------------
APT_PACKAGES=(
    python3-venv python3-pip python3-dev
    python3-gi python3-gi-cairo gir1.2-gtk-3.0 libgirepository1.0-dev
    gir1.2-notify-0.7
    python3-pyatspi at-spi2-core
    portaudio19-dev libportaudio2
    xdotool
    libcairo2-dev pkg-config
)
# tray backends (Cinnamon-friendly)
APT_TRAY=(gir1.2-ayatanaappindicator3-0.1)
# fallback if ayatana not available on older Mint:
APT_TRAY_LEGACY=(gir1.2-appindicator3-0.1)

need_apt=0
for pkg in "${APT_PACKAGES[@]}"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then need_apt=1; break; fi
done

echo
echo "==> System package check"
if [ "$need_apt" -eq 1 ]; then
    echo "The following apt packages are required and appear to be missing:"
    printf '  %s\n' "${APT_PACKAGES[@]}"
    echo
    echo "Suggested command:"
    echo "  sudo apt update && sudo apt install -y ${APT_PACKAGES[*]}"
    echo
    read -r -p "Install them now with sudo? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        sudo apt update
        sudo apt install -y "${APT_PACKAGES[@]}"
        # Try Ayatana; fall back to legacy AppIndicator3 if unavailable.
        if ! sudo apt install -y "${APT_TRAY[@]}" 2>/dev/null; then
            echo "Ayatana AppIndicator not available; installing legacy AppIndicator3..."
            sudo apt install -y "${APT_TRAY_LEGACY[@]}" || true
        fi
    else
        echo "Skipping apt install. You must install these before MyVoice will work."
    fi
else
    echo "All required apt packages appear present."
    # Best-effort ensure at least one tray package
    if ! dpkg -s gir1.2-ayatanaappindicator3-0.1 >/dev/null 2>&1 \
       && ! dpkg -s gir1.2-appindicator3-0.1 >/dev/null 2>&1; then
        echo "No tray indicator library installed. Suggested:"
        echo "  sudo apt install -y gir1.2-ayatanaappindicator3-0.1"
    fi
fi

# ---- Virtualenv ----------------------------------------------------------
echo
echo "==> Creating virtualenv at $VENV"
if [ ! -d "$VENV" ]; then
    "$PYTHON_BIN" -m venv --system-site-packages "$VENV"
fi
# system-site-packages lets us reuse python3-gi/pyatspi from apt (they are
# not on pip in a usable form).

# shellcheck disable=SC1091
source "$VENV/bin/activate"

python -m pip install --upgrade pip wheel setuptools

echo
echo "==> Installing Python dependencies from requirements.txt"
pip install -r "$HERE/requirements.txt"

# ---- Post-install checks -------------------------------------------------
echo
echo "==> Verifying imports"
python - <<'PY'
missing = []
for mod in ("faster_whisper", "sounddevice", "numpy", "webrtcvad", "Xlib"):
    try:
        __import__(mod)
    except Exception as e:
        missing.append((mod, str(e)))
try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk  # noqa
except Exception as e:
    missing.append(("gi/Gtk-3.0", str(e)))
try:
    import pyatspi  # noqa
except Exception as e:
    missing.append(("pyatspi (optional but recommended)", str(e)))
if missing:
    print("Import warnings:")
    for m, e in missing:
        print(f"  - {m}: {e}")
else:
    print("All core imports OK.")
PY

# ---- Desktop launcher ----------------------------------------------------
echo
echo "==> Installing desktop entry"
APP_DIR="$HOME/.local/share/applications"
mkdir -p "$APP_DIR"

RUN_SCRIPT="$HERE/run.sh"
cat > "$APP_DIR/myvoice.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MyVoice
GenericName=Voice Dictation
Comment=Privacy-first local voice dictation
Exec=$RUN_SCRIPT
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Accessibility;AudioVideo;
Keywords=dictation;speech;voice;whisper;
StartupNotify=false
EOF
chmod 0644 "$APP_DIR/myvoice.desktop"
echo "Installed launcher: $APP_DIR/myvoice.desktop"

update-desktop-database "$APP_DIR" 2>/dev/null || true

echo
echo "==> Done."
echo
echo "To run MyVoice:      $HERE/run.sh"
echo "Or launch 'MyVoice' from the applications menu."
echo
echo "Notes:"
echo "  - First-time speech will download the selected Whisper model to"
echo "    ~/.cache/Myvoice/models (only Internet-required step)."
echo "  - Default global shortcut: Super+Shift+Space."
echo "  - Logs live in ~/.config/Myvoice/logs/myvoice.log"
