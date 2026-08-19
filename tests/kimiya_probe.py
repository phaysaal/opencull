"""Whether this machine can actually run the Kimiya compiler.

The compiler is a separate project, checked out beside this one and put
on the path by the program store. A build machine that has only this
repository cannot check a program at all -- so the handful of tests
that assert what the compiler SAYS declare themselves skipped rather
than failing on a missing module, and everything those tests can still
prove without it goes on being proved.
"""

from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def kimiya_runs() -> bool:
    """True when `python -m kimiya` can be imported here."""
    checkout = ROOT.parent / "kimiya-lang"
    environment = dict(os.environ)
    if (checkout / "kimiya").is_dir():
        already = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(checkout)] + ([already] if already else []))
    try:
        done = subprocess.run(
            [sys.executable, "-c", "import kimiya"],
            capture_output=True, env=environment, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0
