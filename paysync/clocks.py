"""Logical time: Lamport clock for event ordering, vector clocks for
causality tracking across replicas (customer / merchant / backend)."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict


class Causal(Enum):
    EQUAL = 0
    BEFORE = 1
    AFTER = 2
    CONCURRENT = 3


@dataclass
class LamportClock:
    counter: int = 0

    def tick(self) -> int:
        self.counter += 1
        return self.counter

    def observe(self, other: int) -> int:
        self.counter = max(self.counter, other) + 1
        return self.counter


@dataclass(frozen=True)
class VectorClock:
    """Immutable vector clock. a <= b iff a[r] <= b[r] for every replica r."""
    entries: Dict[str, int] = field(default_factory=dict)

    def incremented(self, replica: str) -> "VectorClock":
        nxt = dict(self.entries)
        nxt[replica] = nxt.get(replica, 0) + 1
        return VectorClock(nxt)

    def merged(self, other: "VectorClock") -> "VectorClock":
        merged = dict(self.entries)
        for r, c in other.entries.items():
            merged[r] = max(merged.get(r, 0), c)
        return VectorClock(merged)

    def compare(self, other: "VectorClock") -> Causal:
        a, b = self.entries, other.entries
        keys = set(a) | set(b)
        a_le = all(a.get(k, 0) <= b.get(k, 0) for k in keys)
        b_le = all(b.get(k, 0) <= a.get(k, 0) for k in keys)
        if a_le and b_le:
            return Causal.EQUAL
        if a_le:
            return Causal.BEFORE
        if b_le:
            return Causal.AFTER
        return Causal.CONCURRENT

    def to_dict(self) -> Dict[str, int]:
        return dict(self.entries)

    @staticmethod
    def from_dict(d: Dict[str, int]) -> "VectorClock":
        return VectorClock({k: int(v) for k, v in d.items()})
