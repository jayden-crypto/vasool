"""Minimal .env loader.

No dependency, and it never overwrites a variable already set in the real
environment — an exported key always wins over a file, which is the behaviour
you want when you are switching between test accounts.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def load(path: Path | None = None) -> list[str]:
    """Load KEY=VALUE lines. Returns the names it set, for reporting."""
    path = path or ENV_PATH
    if not path.exists():
        return []
    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if not key or not value or key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded
