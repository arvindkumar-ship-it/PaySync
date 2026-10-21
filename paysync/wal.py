"""Append-only write-ahead log. A transition is durable only after its record
is appended, flushed and fsynced. Replay rebuilds state deterministically.

Crash safety: a torn FINAL record (power loss mid-append) is detected on open,
archived byte-for-byte to `<path>.torn`, and truncated so the log ends on a
record boundary. Invalid data anywhere BEFORE the final record is real
corruption and raises WALCorruptionError; it is never skipped."""
from __future__ import annotations
import json, os
from typing import Callable


class WALCorruptionError(Exception):
    pass


def _valid(line: bytes) -> bool:
    try:
        rec = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(rec, dict) and isinstance(rec.get("crdt"), dict)


class WriteAheadLog:
    def __init__(self, path: str, fsync: bool = True):
        self.path = path
        self.fsync_enabled = fsync
        self.recovered_torn_bytes = 0
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        self._recover()
        self._fh = open(self.path, "a", encoding="utf-8")

    def _recover(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as fh:
            data = fh.read()
        if not data:
            return
        lines = data.split(b"\n")
        tail = lines.pop()                      # bytes after the last "\n"
        good_end = 0
        for i, line in enumerate(lines):
            if line.strip() and not _valid(line):
                raise WALCorruptionError(f"{self.path}: corrupt record at line {i + 1}")
            good_end += len(line) + 1
        if not tail.strip():
            if tail:                            # trailing whitespace only
                self._truncate(good_end)
            return
        if _valid(tail):                        # complete record, missing newline
            with open(self.path, "ab") as fh:
                fh.write(b"\n")
                fh.flush()
                os.fsync(fh.fileno())
            return
        with open(self.path + ".torn", "ab") as fh:   # archive, then drop torn tail
            fh.write(tail + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.recovered_torn_bytes = len(tail)
        self._truncate(good_end)

    def _truncate(self, size: int) -> None:
        with open(self.path, "r+b") as fh:
            fh.truncate(size)
            fh.flush()
            os.fsync(fh.fileno())

    def append(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":"), sort_keys=True)
        self._fh.write(line + "\n")
        self._fh.flush()  # always flush so replay sees every record
        if self.fsync_enabled:
            os.fsync(self._fh.fileno())

    def replay(self, apply: Callable[[dict], None]) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    apply(json.loads(line))

    def close(self) -> None:
        self._fh.close()
