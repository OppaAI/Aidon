"""Isolated-process example tool for the manifest in this directory.

Unlike the inline example, this module is imported by the sandbox
runner, never by Aidon's process. That is why ``subprocess`` is allowed
here: it runs inside the bubblewrap sandbox, not on the host. The tool
is still adoption-gated, and the owner's card lists the ``network``
capability before any of it exists.
"""

import os
import subprocess


def example_sandbox(args: dict) -> str:
    # A real subprocess call, contained by the sandbox namespace.
    uid = subprocess.run(
        ["/usr/bin/id", "-u"], capture_output=True, text=True
    ).stdout.strip()
    return f"sandbox pid={os.getpid()} uid={uid} text={args.get('text', '')}"
