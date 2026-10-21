"""Payment state CRDT (join-semilattice).

State is a pair (state, state_clock) joined by taking the maximum under a
fixed TOTAL order; the observed `clock` is the pointwise vector-clock join;
amount is max; reversal_initiated is OR; conflict is OR; created_at is the
earliest non-zero value. Every component is a semilattice, so merge is
commutative, associative and idempotent for ALL inputs (strong eventual
consistency).

Order on (state, state_clock):
  1. Terminal states dominate non-terminal ones.
  2. Between terminals: REFUNDED > SETTLED (money already returned wins);
     a differing pair of terminals raises the sticky `conflict` flag.
     (Replicas also keep their own terminal state; see PaymentEngine.apply_remote.)
  3. Between non-terminals: larger sum(state_clock) wins. This is a linear
     extension of causality: a causally newer state has a strictly larger sum.
  4. Remaining ties: rank, state name, then the clock items (deterministic).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from .clocks import VectorClock
from .fsm import State, TERMINAL

_RANK = {
    State.INITIATED: 0, State.FAILED: 1, State.DEBITED: 2,
    State.REFUND_PENDING: 3, State.CREDITED: 4,
    State.SETTLED: 5, State.REFUNDED: 6,
}


def _key(state: State, state_clock: VectorClock):
    items = tuple(sorted(state_clock.entries.items()))
    if state in TERMINAL:
        return (1, 0, _RANK[state], state.value, items)
    return (0, sum(state_clock.entries.values()), _RANK[state], state.value, items)


@dataclass
class PaymentCRDT:
    txn_id: str
    state: State = State.INITIATED
    clock: VectorClock = field(default_factory=VectorClock)
    amount_minor: int = 0
    reversal_initiated: bool = False
    created_at: float = 0.0
    state_clock: Optional[VectorClock] = None
    conflict: bool = False

    def __post_init__(self) -> None:
        if self.state_clock is None:
            self.state_clock = self.clock

    def local_update(self, replica: str, new_state: State) -> "PaymentCRDT":
        if new_state == self.state:
            return self
        clock = self.clock.incremented(replica)
        return PaymentCRDT(self.txn_id, new_state, clock, self.amount_minor,
                           self.reversal_initiated, self.created_at,
                           clock, self.conflict)

    def merge(self, other: "PaymentCRDT") -> "PaymentCRDT":
        if other.txn_id != self.txn_id:
            raise ValueError("cannot merge CRDTs of different transactions")
        if _key(other.state, other.state_clock) > _key(self.state, self.state_clock):
            state, sclock = other.state, other.state_clock
        else:
            state, sclock = self.state, self.state_clock
        conflict = (self.conflict or other.conflict or
                    (self.state in TERMINAL and other.state in TERMINAL
                     and self.state != other.state))
        times = [t for t in (self.created_at, other.created_at) if t]
        return PaymentCRDT(self.txn_id, state,
                           self.clock.merged(other.clock),
                           max(self.amount_minor, other.amount_minor),
                           self.reversal_initiated or other.reversal_initiated,
                           min(times) if times else 0.0,
                           sclock, conflict)

    def to_dict(self) -> dict:
        return {"txn_id": self.txn_id, "state": self.state.value,
                "clock": self.clock.to_dict(), "amount_minor": self.amount_minor,
                "reversal_initiated": self.reversal_initiated,
                "created_at": self.created_at,
                "state_clock": self.state_clock.to_dict(),
                "conflict": self.conflict}

    @staticmethod
    def from_dict(d: dict) -> "PaymentCRDT":
        clock = VectorClock.from_dict(d["clock"])
        sc = d.get("state_clock")
        return PaymentCRDT(d["txn_id"], State(d["state"]), clock,
                           int(d["amount_minor"]), bool(d["reversal_initiated"]),
                           float(d.get("created_at", 0.0)),
                           VectorClock.from_dict(sc) if sc is not None else clock,
                           bool(d.get("conflict", False)))
