"""On-disk cache of model responses, keyed by evidence.

This is what makes the benchmark reproducible by someone who does not have an
API key: the cache file is committed, and a replay of the same batch reads
every decision back byte-for-byte instead of re-querying the model.

The key includes the prompt version and model id, so changing either produces a
clean miss rather than a silently stale result.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

CACHE_PATH = Path(__file__).resolve().parents[2] / "cache" / "llm_responses.json"


def key_for(evidence_digest: str, prompt_version: str, model: str,
            effort: str, repair_round: int) -> str:
    blob = f"{evidence_digest}|{prompt_version}|{model}|{effort}|{repair_round}"
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class ResponseCache:
    def __init__(self, path: Optional[Path] = None, enabled: bool = True,
                 autoflush_every: int = 20) -> None:
        self.path = path or CACHE_PATH
        self.enabled = enabled
        self.autoflush_every = autoflush_every
        self._data: dict[str, Any] = {}
        self.hits = 0
        self.misses = 0
        self._dirty = False
        self._since_flush = 0
        self._lock = threading.Lock()
        if self.enabled and self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self._data = {}

    def get(self, key: str) -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None
        value = self._data.get(key)
        if value is None:
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, key: str, value: dict[str, Any]) -> None:
        """Store a response, flushing periodically.

        Flushing only at the end was a real bug: a warm run killed at 45
        minutes lost every one of its results, because nothing had touched the
        disk yet. An expensive cache that only persists on a clean exit is not
        a cache, it is a gamble on the process surviving.
        """
        if not self.enabled:
            return
        with self._lock:
            self._data[key] = value
            self._dirty = True
            self._since_flush += 1
            due = self._since_flush >= self.autoflush_every
        if due:
            self.flush()

    def flush(self) -> None:
        """Atomic write via rename, so a kill mid-flush cannot corrupt the file."""
        if not (self.enabled and self._dirty):
            return
        with self._lock:
            snapshot = dict(self._data)
            self._dirty = False
            self._since_flush = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, sort_keys=True, indent=0)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def __len__(self) -> int:
        return len(self._data)
