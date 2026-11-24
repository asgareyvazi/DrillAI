"""Small PID-namespace init for opt-in Linux OCR subprocess isolation.

The supervisor, not the OCR executable, remains PID 1 of a fresh namespace. Linux destroys the
namespace's remaining processes when its init exits, including ordinary descendants that call
setsid(). This is a process isolation primitive, not a complete document deadline or a defence
against arbitrary privileged code. It must only be invoked inside the namespace wrapper.
"""
from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    # Refuse accidental direct use outside a PID namespace. No shell interprets the argument list.
    if os.getpid() != 1 or len(sys.argv) < 2:
        return 125
    try:
        child = subprocess.Popen(sys.argv[1:], close_fds=True)
        status = child.wait()
    except OSError:
        return 126
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    raise SystemExit(main())
