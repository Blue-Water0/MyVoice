"""The OS-level process name (what ps/top/GNOME System Monitor show)
must read 'myvoice', not the bare Python interpreter name -- otherwise
MyVoice is indistinguishable from any other Python process in the task
manager. Confirmed live on 2026-07-19: `ps -T` showed comm='python' for
the running MyVoice process.
"""
from __future__ import annotations

import os
import pwd
import subprocess
import sys
import time


def test_set_process_title_renames_process_for_ps_and_system_monitor():
    # conftest.py's autouse fixture points $HOME at a tmp dir for XDG
    # isolation; numpy (an app.py import) lives under the *real* home's
    # user-site-packages, so the child needs the real HOME restored or
    # it fails on `import numpy` before ever reaching our code.
    env = dict(os.environ, HOME=pwd.getpwuid(os.getuid()).pw_dir)
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "from myvoice.app import _set_process_title\n"
         "_set_process_title()\n"
         "import time; time.sleep(5)\n"],
        env=env,
    )
    try:
        comm = ""
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                with open(f"/proc/{proc.pid}/comm") as f:
                    comm = f.read().strip()
            except FileNotFoundError:
                pass
            if comm == "myvoice":
                break
            time.sleep(0.05)

        assert comm == "myvoice", (
            f"process comm was {comm!r}, expected 'myvoice' -- "
            f"System Monitor would show this process as generic Python "
            f"instead of MyVoice"
        )
    finally:
        proc.kill()
        proc.wait()
