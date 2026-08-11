"""Small shared path helper — its own module so scanner.py and live.py don't
duplicate (or subtly diverge on) the same containment check."""

from __future__ import annotations

import os


def is_under(path: str, base: str) -> bool:
    path_n = os.path.normcase(os.path.normpath(path))
    base_n = os.path.normcase(os.path.normpath(base))
    return path_n == base_n or path_n.startswith(base_n + os.sep)
