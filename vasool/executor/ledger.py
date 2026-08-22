"""Append-only, hash-chained audit ledger.

Two properties matter. It is *write-ahead*: the record of what we intend to do
exists before the doing, so a crash between the two leaves evidence rather than
a mystery. And it is *chained*: each record commits to the previous digest, so
a run is replayable and any edit to history is detectable.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

GENESIS = "0" * 64


@dataclass(frozen=True)
class LedgerRecord:
    seq: int
    at: str
    kind: str
    case_id: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "seq": self.seq, "at": self.at, "kind": self.kind,
                "case_id": self.case_id, "payload": self.payload,
                "prev_hash": self.prev_hash, "hash": self.hash,
            },
            sort_keys=True, separators=(",", ":"),
        )


def _digest(seq: int, at: str, kind: str, case_id: str,
            payload: dict[str, Any], prev_hash: str) -> str:
    body = json.dumps(
        {"seq": seq, "at": at, "kind": kind, "case_id": case_id,
         "payload": payload, "prev_hash": prev_hash},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(body.encode()).hexdigest()


class Ledger:
    """JSONL on disk, chained in memory. One file per run."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self._records: list[LedgerRecord] = []
        self._tip = GENESIS
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", encoding="utf-8")
        else:
            self._fh = None

    def append(self, kind: str, case_id: str, payload: dict[str, Any],
               at: Optional[datetime] = None) -> str:
        """Write one record and return its digest — the receipt I8 demands."""
        seq = len(self._records)
        stamp = (at or datetime.utcnow()).isoformat()
        safe_payload = json.loads(json.dumps(payload, default=str, sort_keys=True))
        digest = _digest(seq, stamp, kind, case_id, safe_payload, self._tip)
        record = LedgerRecord(
            seq=seq, at=stamp, kind=kind, case_id=case_id,
            payload=safe_payload, prev_hash=self._tip, hash=digest,
        )
        self._records.append(record)
        self._tip = digest
        if self._fh is not None:
            self._fh.write(record.to_json() + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        return digest

    @property
    def tip(self) -> str:
        return self._tip

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[LedgerRecord]:
        return iter(self._records)

    def for_case(self, case_id: str) -> list[LedgerRecord]:
        return [r for r in self._records if r.case_id == case_id]

    def verify(self) -> tuple[bool, Optional[int]]:
        """Recompute the whole chain. Returns (ok, first_bad_seq)."""
        prev = GENESIS
        for record in self._records:
            expected = _digest(
                record.seq, record.at, record.kind, record.case_id,
                record.payload, prev,
            )
            if expected != record.hash or record.prev_hash != prev:
                return False, record.seq
            prev = record.hash
        return True, None

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    @staticmethod
    def load(path: Path) -> "Ledger":
        ledger = Ledger(path=None)
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            ledger._records.append(LedgerRecord(**d))
        if ledger._records:
            ledger._tip = ledger._records[-1].hash
        return ledger
