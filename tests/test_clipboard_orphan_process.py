"""Regression coverage: xclip processes spawned to hold clipboard
ownership must not outlive MyVoice if MyVoice itself dies ungracefully
(crash, freeze + force-quit, SIGKILL). Found live on 2026-07-19: two
`xclip` processes from a dead MyVoice session were still running,
reparented to init, because nothing tells them to exit when their
parent disappears without running its normal end_session() cleanup.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from myvoice.services.clipboard_backend import _pdeathsig_preexec


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_pdeathsig_preexec_kills_child_when_parent_is_sigkilled():
    """A child spawned with _pdeathsig_preexec must be terminated by the
    kernel when its parent dies, even via SIGKILL (which the parent
    cannot catch or clean up after)."""
    parent = subprocess.Popen(
        [sys.executable, "-u", "-c",
         "import subprocess, sys, time\n"
         "from myvoice.services.clipboard_backend import _pdeathsig_preexec\n"
         "p = subprocess.Popen(['sleep', '30'], preexec_fn=_pdeathsig_preexec)\n"
         "print(p.pid, flush=True)\n"
         "time.sleep(30)\n"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        child_pid_line = parent.stdout.readline().strip()
        assert child_pid_line, "helper process printed no child pid"
        child_pid = int(child_pid_line)
        assert _pid_alive(child_pid), "child did not even start"

        parent.kill()  # SIGKILL: same as a desktop "Force Quit"
        parent.wait(timeout=5)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _pid_alive(child_pid):
            time.sleep(0.05)

        assert not _pid_alive(child_pid), (
            "xclip-like child survived its parent being SIGKILLed -- "
            "it will be orphaned forever, exactly like the two leaked "
            "xclip processes found running on 2026-07-19"
        )
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
