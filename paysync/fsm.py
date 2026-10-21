"""Deterministic transaction state machine.

Invariants:
  I1: at most one terminal state per transaction.
  I2: DEBITED requires debit proof; CREDITED requires credit proof.
  I3: transitions outside the adjacency map raise TransitionError.
  I4: terminal states are absorbing.
"""
from __future__ import annotations
from enum import Enum


class State(str, Enum):
    INITIATED = "INITIATED"
    DEBITED = "DEBITED"
    CREDITED = "CREDITED"
    SETTLED = "SETTLED"
    FAILED = "FAILED"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"


TERMINAL = frozenset({State.SETTLED, State.REFUNDED})

TRANSITIONS = {
    State.INITIATED: frozenset({State.DEBITED, State.FAILED}),
    State.DEBITED: frozenset({State.CREDITED, State.REFUND_PENDING, State.FAILED}),
    State.CREDITED: frozenset({State.SETTLED}),
    State.REFUND_PENDING: frozenset({State.REFUNDED}),
    State.SETTLED: frozenset(),
    State.REFUNDED: frozenset(),
    State.FAILED: frozenset({State.REFUND_PENDING}),
}


class TransitionError(Exception):
    pass


def can_transition(src: State, dst: State) -> bool:
    return dst in TRANSITIONS[src]


def assert_transition(src: State, dst: State) -> None:
    if not can_transition(src, dst):
        raise TransitionError(f"illegal transition {src.value} -> {dst.value}")


def is_terminal(state: State) -> bool:
    return state in TERMINAL
