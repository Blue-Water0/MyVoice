# MyVoice

Privacy-first, system-wide **local voice dictation** for Linux Mint (Cinnamon,
X11). English, Hebrew, and Arabic. No cloud. No account. No API key.

- Press a global shortcut → speak → transcribed text is typed into whatever
  editable field was focused.
- Works while the main window is hidden or minimized.
- Speech runs 100% on your machine using
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2).
- Microphone audio is never persisted and never sent over the network.

Status: **v0.1** — first working release. See the “Known limitations” section
below.

---

## Requirements

- Linux Mint 21.3+ or 22.x (Cinnamon).
- Session type **X11** for global hotkey + cross-app injection.
  (Wayland works for AT-SPI-enabled apps but the global hotkey is limited.)
- Python **3.11+**.
- ~4 GB free RAM for the recommended `medium` model, or more for `large-v3-turbo`.
- ~500 MB – 1.6 GB free disk for the model, one-time download.

## Install

```bash
git clone <your-repo-url> MyVoice
cd MyVoice
./install.sh
```

The installer:
1. Checks Python 3.11+.
2. Lists required apt packages and prompts before running `sudo apt install`.
3. Creates a venv at `./.venv` (with `--system-site-packages` so
   `python3-gi`, `pyatspi`, `Gtk-3.0` bindings are picked up from apt).
4. Installs pip requirements (`faster-whisper`, `sounddevice`, `webrtcvad`,
   `python-xlib`, `numpy`).
5. Writes a desktop launcher to `~/.local/share/applications/myvoice.desktop`.

## Run

```bash
./run.sh
```

or, once installed:

```bash
# from the applications menu, search "MyVoice"
```

Options:

```bash
./run.sh --log-level DEBUG
./run.sh --start-listening   # begin dictating immediately
```

## Using it

1. Click into any editable field (browser search box, text editor, terminal,
   chat, LibreOffice document…).
2. Press **Super+Shift+Space** (default) *or* click **Start Listening** in
   MyVoice.
3. Speak. A small overlay appears near the bottom-center of your screen —
   typically within 50 ms of pressing the shortcut.
4. Press the shortcut again *or* click **Stop Listening**.

If no editable target was detected, MyVoice copies the final transcript to
your clipboard and shows a notification explaining why.

**Closing vs quitting**: clicking the window's ✕ button hides MyVoice to
the system tray — it keeps running so the global shortcut still works.
To fully exit, use **Tray icon → Quit VoiceType** (or the Quit menu item
in the main window). If dictation is active when you quit, MyVoice stops
the microphone, finishes any pending transcription (up to ~11 seconds),
and releases all resources before exiting. It will never crash, hang, or
leave a zombie process.

**Startup behaviour**: when MyVoice launches it preloads the selected Whisper
model in the background so the *first* Start Listening is fast. If you press
Start before preload finishes, the overlay still appears immediately and
recording begins; the app shows "Preparing model… (recording)" and
transcribes your speech as soon as the model is ready. You never wait on a
frozen UI. Turn on debug logging (`./run.sh --log-level DEBUG`) to see the
per-phase timing in `~/.config/Myvoice/logs/myvoice.log`.

## Text insertion

MyVoice uses two insertion strategies per chunk, tried in order:

1. **AT-SPI direct** — inserts at the exact caret position via the
   accessibility API. Works in browsers, LibreOffice, native GTK apps
   with accessibility enabled.
2. **Clipboard + XTEST paste** — writes the chunk to the clipboard,
   then sends the paste shortcut via XTEST (kernel-level input injection,
   not XSendEvent). Works in Sublime Text, VS Code, and any application
   that cannot be reached via AT-SPI.

### Terminal support

Terminals use `Ctrl+Shift+V` instead of `Ctrl+V`. MyVoice automatically
detects terminals by reading the X11 `WM_CLASS` property and comparing
it against a built-in list of known terminal emulators:

> gnome-terminal, xfce4-terminal, xterm, kitty, alacritty, konsole,
> tilix, terminator, mate-terminal, lxterminal, wezterm, rxvt, foot, …

If your terminal is not on the list, add its `WM_CLASS` name to the
settings file (`~/.config/Myvoice/settings.json`) under the key
`"terminal_wm_classes"`:

```json
{
  "terminal_wm_classes": ["myfancyterm", "another-term"]
}
```

To find the WM_CLASS of any window:

```bash
xprop WM_CLASS   # then click the window
```

### Diagnostics

```bash
./run.sh --log-level DEBUG
```

Shows per-chunk logs: chunk number, character count, XID, WM_CLASS,
app class, backend used (AT-SPI or clipboard/XTEST), paste shortcut,
and xdotool exit code.

**Verbose diagnostic mode** (logs a 20-character sample of each dictated
chunk — **do not use in production**):

```bash
MYVOICE_VERBOSE_DIAG=1 ./run.sh --log-level DEBUG
```

> **Warning:** `MYVOICE_VERBOSE_DIAG=1` writes partial transcript content
> to the log file. Keep it disabled unless actively debugging insertion
> issues. Never enable it in shared or sensitive environments.

## Live transcript actions

The main window shows a "Live transcript preview:" area with two small
icon buttons pinned to its top-right corner:

| Icon | Action | Shortcut behaviour |
|------|--------|--------------------|
| Copy | **Copy transcript** — copies the full current-session transcript (not just the visible preview) to the system clipboard. | Overrides the injector's end-of-session clipboard restoration so your copy sticks. |
| Open | **Open in Text App** — writes the full transcript as a UTF-8 `.txt` file and opens it in the text editor selected in Settings. | Snapshot semantics: the file is fixed at click time and won't be modified by later speech. |

The transcript stays visible after **Stop Listening** until you start a
new dictation session. Buttons are disabled when there is no transcript
yet.

Saved transcripts live under:

```
~/Documents/VoiceType Transcripts/VoiceType Transcript YYYY-MM-DD HH-MM-SS.txt
```

Explicit UTF-8, no BOM. English / Hebrew / Arabic and any other
Unicode text are preserved exactly.

### Choosing a text editor

Open **Settings** → **Transcript text app**. The dropdown contains:

* `System default text editor (…)` — uses whatever your desktop has
  configured as the default handler for `text/plain`.
* Every installed windowed application registered for `text/plain`
  (Xed / Text Editor, Gedit, Kate, Mousepad, Sublime Text, VS Code, …).
  Terminal-only editors (vim, nvim, nano invoked from a terminal) are
  filtered out because they can't reasonably open a windowed file.

Selection is persisted through `SettingsService`. If the configured app
is later uninstalled, MyVoice silently falls back to the system default
and shows a status message. If no text application is registered at
all, the `.txt` file is still saved and the transcripts folder is
opened in the file manager so you can find it.

The **refresh** button next to the dropdown re-scans installed
applications without needing to reopen the dialog.

## Language

- Default: matches your OS locale (English / Hebrew / Arabic).
- Change from the main window: **Language** dropdown.
- Arabic (`ar`) supports colloquial and MSA — the multilingual Whisper models
  handle a range of regional dialects; use `medium` or `large-v3-turbo` for
  best results.
- Right-to-left is preserved because we insert logical-order Unicode, never
  re-render it.

## Model selection

Open Settings → **Model quality**. Presets:

| Preset            | Approx. download | Approx. RAM | Best for                          |
|-------------------|-----------------:|------------:|------------------------------------|
| `small`           |            470 MB|     1.2 GB  | modest CPUs, English mostly       |
| `medium` (default)|          1500 MB |     2.6 GB  | recommended for Hebrew & Arabic   |
| `large-v3-turbo`  |          1620 MB |     4.2 GB  | best accuracy/speed tradeoff      |

The first Start after choosing a new model triggers a one-time download
(handled by `huggingface_hub`) — the only time internet is used.

Clear the on-disk cache from Settings → **Clear downloaded models cache**.

## Global shortcut

Default: `Super+Shift+Space`. To change it:

1. Open **Settings** (gear icon in the main window).
2. Under **Global shortcut**, click **Record Shortcut**.
3. A small dialog appears — press the exact key combination you want.
   Press **Esc** to cancel.
4. On success the new shortcut is grabbed immediately and saved.
5. If the combination is already claimed by Cinnamon or another app, the
   dialog says "already in use" and your previous shortcut stays active.

Click **Reset to Default** to return to `Super+Shift+Space`.

Modifier-only presses (Shift, Ctrl, Alt, Super on their own) are refused —
a valid shortcut must include at least one non-modifier key. Caps Lock and
Num Lock state is ignored so the shortcut works either way.

For power users: MyVoice accepts both `Super+Shift+Space` and the legacy
GTK form `<Super><Shift>space` if you hand-edit `settings.json`.

## Text insertion

Every transcribed chunk is routed independently to whichever editable
field is focused at the moment it arrives:

1. **AT-SPI2** (`EditableText.insertText` at the caret). Works in GTK
   apps, LibreOffice, and any Qt/Java app that exposes its text field
   over AT-SPI. Character offsets are used so Hebrew, Arabic, and
   other non-ASCII text lands correctly.
2. **Clipboard + xdotool `Ctrl+V`** into the currently-focused X11
   window. Used when the current focus doesn't expose an
   `EditableText` interface but is still an X11 window we can address
   (terminals, Sublime Text, some Electron apps). Because we only
   paste into what the user has focused *right now*, the Ctrl+V lands
   where the user's own keyboard would type. The pre-dictation
   clipboard is saved at Start and restored at Stop.
3. **Buffer + clipboard drop on Stop.** If neither above is possible
   at the moment a chunk is ready (nothing writable focused), the
   chunk is safely buffered in-session. When you Stop, the joined
   transcript is copied to your clipboard with a notification.

If you click a different editable field mid-session, subsequent
chunks follow the new focus. There is no "lock" and no memory of a
previous target — MyVoice always writes to what's focused *now*.

Password fields (AT-SPI role `PASSWORD_TEXT`) are refused in all
cases. MyVoice's own accessibles are refused as targets by PID
comparison, so accidental focus on our own transcript preview never
turns into a dictation destination.

### Start button and focus

The Start Listening button is deliberately made unfocusable
(`can-focus = false`), so clicking it does not move keyboard focus
off whatever text field you were in. In practice:

* If you already have your target field focused, click Start and
  speak — the first chunk lands in your target field.
* If you were focused on something non-editable (desktop, browser
  chrome), start MyVoice first and then click the target field —
  MyVoice will pick up the change of focus for the next chunk.

The global shortcut is the same, minus the click. Pressing the
shortcut does not disturb focus, so whatever you had focused stays
focused.

### Known limitations

* **VTE terminals (GNOME Terminal, xterm)** only expose read-only
  `Text` accessibility, not `EditableText`. Dictation into them uses
  the clipboard+Ctrl+V path (path 2 above).
* **Sublime Text** doesn't expose an accessibility tree at all. Same
  clipboard+Ctrl+V path.
* **Chromium-family browsers** (Brave, Chrome, Chromium, Edge, Opera,
  Vivaldi) do not expose their webpage input fields over AT-SPI
  without the `--force-renderer-accessibility` flag. Without it,
  dictation into their web content falls back to clipboard+Ctrl+V —
  which works if the browser window is the currently-focused window.
* **Firefox** exposes web content over AT-SPI as long as
  `accessibility.force_disabled` is `0` in `about:config` (default).

## Files & paths

- Settings: `~/.config/Myvoice/settings.json`
- Logs: `~/.config/Myvoice/logs/myvoice.log` (rotated, no audio, no transcripts)
- Model cache: `~/.cache/Myvoice/models/`
- Autostart entry (if enabled): `~/.config/autostart/myvoice.desktop`
- Saved transcripts: `~/Documents/VoiceType Transcripts/*.txt` (created on
  first use of **Open in Text App**)

## Testing

Pure-logic unit tests (no audio hardware required):

```bash
./.venv/bin/pip install pytest
./.venv/bin/pytest -q
```

Covered:
- `settings_service` — save/load, corruption tolerance, atomic write
- `language_service` — locale detection, resolve()
- `vad_service` — segmentation, pre-roll, max-length cut, flush
- `transcript_buffer` — joining, whitespace, RTL Unicode
- `hotkey_service.parse_accel` — accelerator parsing
- `paths` — XDG directory creation
- `transcript_export` — safe/timestamped filenames, UTF-8 writes
  (English + Hebrew + Arabic), text-app discovery (excluding terminal
  editors), system-default resolution, fallback when configured app
  is uninstalled, launcher failure preserves the saved file
- `clipboard_backend.write_user_text` / `mark_user_override` —
  explicit "Copy Transcript" survives injector end-of-session
  clipboard restoration
- `transcript_actions` — full transcript is used (not the truncated
  preview), text-app selection persists across reload, missing extra
  keys are tolerated

## Known limitations

- **Wayland**: X11 `XGrabKey` is not available on Wayland; the global hotkey
  service refuses to start with a clear error. Text insertion via AT-SPI
  still works inside AT-SPI apps. This is intentional honesty, not a bug.
- **Firefox / Chrome**: their AT-SPI `EditableText` implementation is
  inconsistent — MyVoice automatically falls back to clipboard-paste for
  browsers. Result: text does insert, but AT-SPI-native caret positioning
  is not always available.
- **Very short mic clips**: chunks below `min_speech_ms` (default 300 ms)
  are dropped to avoid Whisper hallucinations on tiny audio.
- **GPU**: v1 is CPU-only. GPU support can be added later via
  faster-whisper `device="cuda"`.
- **Engine plugability**: the abstract `SpeechEngine` interface is present
  and a registry exists, but only `faster-whisper` is implemented. Adding
  `whisper.cpp` or Vosk is a matter of writing another engine class and
  registering it.

## Troubleshooting

- `Failed to grab hotkey ...` — another app (or Cinnamon) has that
  combination. Pick a different one in Settings.
- `Could not open microphone` — check volume/permissions in Cinnamon Sound
  settings; ensure the selected mic is not muted; try “System default”.
- `pyatspi` import warning during install — AT-SPI backend will be disabled;
  clipboard fallback still works. Reinstall `python3-pyatspi at-spi2-core`.
- `xdotool` not found — clipboard paste falls back to buffer-only; install
  `sudo apt install xdotool`.
- Model download stalls — delete `~/.cache/Myvoice/models/` and retry with
  a smaller preset (`small`).

## License

MIT. Copyright (c) 2026 Blue-Water0.

See `LICENSE` for the full text and `THIRD_PARTY_NOTICES.md` for third-party
component licenses.

## Development

Project layout:

```
myvoice/
  app.py
  paths.py
  engines/         base + faster_whisper engine + registry
  services/       audio, vad, transcription, hotkey, overlay,
                  text_injection, accessibility, clipboard, focus, settings, ...
  ui/             main_window, settings_dialog, overlay_window, tray, styles.css
  models/         model cache helpers
tests/
  test_*.py       pure-logic tests
install.sh
run.sh
requirements.txt
pyproject.toml
```

Contributions welcome. If you add a new engine, implement `engines.base.SpeechEngine`,
register it in `engines/registry.py`, and add a preset list.
