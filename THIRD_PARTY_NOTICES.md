# Third-Party Software Notices

MyVoice is licensed under the MIT License (see `LICENSE`).
Copyright (c) 2026 Blue-Water0.

MyVoice depends on third-party software. The notices below apply only to the
identified third-party components; they do not modify MyVoice's own MIT
license.

## How dependencies reach the user

Neither the git repository nor the Linux `.deb` package bundles any
third-party binaries. `install.sh` / the `.deb`'s `postinst` script installs
Python dependencies from PyPI on the user's machine at install time
(requires internet access), and Whisper model weights are downloaded
directly from Hugging Face on first use, not redistributed by this project.
This significantly reduces (but does not eliminate) notice obligations for
the Linux distribution path.

The **Windows build is different**: PyInstaller freezes and bundles
dependency binaries — including PySide6/Qt — directly into the distributed
folder/installer. Anyone shipping that build takes on additional obligations
noted below.

## Direct dependencies (Linux)

- faster-whisper — MIT
- CTranslate2 — MIT
- sounddevice — MIT
- PortAudio (via sounddevice) — MIT-style / BSD-style
- NumPy — BSD 3-Clause
- webrtcvad — BSD 3-Clause
- python-xlib — LGPL-2.1-or-later
- setproctitle — BSD
- PyGObject / GTK 3 (system packages via apt, not bundled) — LGPL-2.1-or-later / LGPL-2.0-or-later
- xdotool (system package via apt, not bundled) — BSD-style

None of the above are copyleft in a way that requires MyVoice's own source to
be published. The LGPL components (python-xlib, PyGObject/GTK) are used via
dynamic linking / standard Python import and are not statically bundled, so
standard LGPL dynamic-linking compliance applies: retain their license and
copyright notices (this file), and do not restrict users from
replacing/upgrading those libraries independently — both already true here,
since they're installed as ordinary pip/apt packages the user can swap.

## Additional direct dependencies (Windows build only)

- PySide6 (Qt for Python) — **LGPLv3** (or commercial Qt license)
- comtypes — MIT
- plyer — MIT
- PyInstaller (build-time only, not distributed) — GPLv2-with-bootloader-exception; the exception explicitly permits distributing the frozen application under any license

**PySide6/Qt is bundled as binaries in the frozen Windows build.** Before
distributing that build publicly:
- Confirm the specific PySide6/Qt build in use is the LGPLv3 (not commercial)
  edition.
- Provide the LGPLv3 license text and Qt copyright notices alongside the
  installer.
- Ensure users can relink/replace the bundled Qt libraries with a modified
  version (e.g., by shipping the Qt components as separate DLLs the user can
  swap, rather than a single fully-static binary) or provide a written offer
  to supply the object files needed to do so, per LGPLv3 §4.

## Whisper model weights

Models are downloaded on first use directly from Hugging Face via
`faster-whisper`'s built-in preset map (e.g. `Systran/faster-whisper-medium`,
`mobiuslabsgmbh/faster-whisper-large-v3-turbo`). These are CTranslate2
conversions of OpenAI's Whisper weights and are published under the MIT
license, matching the license of the original Whisper model. MyVoice does
not redistribute these weights itself — each installation fetches them
independently from Hugging Face.

## Notes

- This file covers the dependencies declared in `pyproject.toml` /
  `requirements.txt` / `requirements-win.txt` as of the last update. If
  dependencies change, update this file to match.
- This is not legal advice. If MyVoice is ever distributed commercially, a
  qualified lawyer should review this file and the Windows LGPL compliance
  steps above.
