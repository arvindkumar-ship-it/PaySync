"""PaySync core engine: FSM + idempotency + WAL + CRDT + reconciler.
Money is integer minor units (paise). Never floats."""
from __future__ import annotations
import threading, time
from typing import Dict, Optional
from .crdt import PaymentCRDT
from .fsm import State, TransitionError, assert_transition, is_terminal
from .idempotency import IdempotencyStore
from .wal import WriteAheadLog

REPLICA = "backend"

# WAL event -> idempotency key prefix, so idempotency survives restarts.
_KEYED_EVENTS = {"INITIATED": "initiate", "DEBITED": "DEBITED", "CREDITED": "CREDITED",
                 "SETTLED": "SETTLED", "FAILED": "FAILED",
                 "REVERSAL_STARTED": "REVERSAL_STARTED", "REFUNDED": "REFUNDED"}


def _require_ref(name: str, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


class PaymentEngine:
    def __init__(self, wal_path: str, fsync: bool = True,
                 pending_timeout_s: float = 8.0):
        self._lock = threading.RLock()
        self._txns: Dict[str, PaymentCRDT] = {}
        self._idem = IdempotencyStore()
        self._wal = WriteAheadLog(wal_path, fsync=fsync)
        self.pending_timeout_s = pending_timeout_s
        self._replay_wal()

    def close(self) -> None:
        self._wal.close()

    @property
    def recovered_torn_bytes(self) -> int:
        return self._wal.recovered_torn_bytes

    def _replay_wal(self) -> None:
        def apply(rec: dict) -> None:
            crdt = PaymentCRDT.from_dict(rec["crdt"])
            self._txns[crdt.txn_id] = crdt
            prefix = _KEYED_EVENTS.get(rec.get("event"))
            if prefix:
                self._idem.record(f"{prefix}:{crdt.txn_id}")
        self._wal.replay(apply)

    def _persist(self, crdt: PaymentCRDT, event: str, proof: dict) -> None:
        self._wal.append({"event": event, "proof": proof, "crdt": crdt.to_dict()})

    def initiate(self, txn_id: str, amount_minor: int) -> PaymentCRDT:
        if amount_minor <= 0:
            raise ValueError("amount_minor must be positive")
        def op() -> None:
            with self._lock:
                if txn_id in self._txns:
                    return
                crdt = PaymentCRDT(txn_id=txn_id, amount_minor=amount_minor,
                                   created_at=time.time())
                crdt = crdt.local_update(REPLICA, State.INITIATED)
                self._persist(crdt, "INITIATED", {"txn_id": txn_id})
                self._txns[txn_id] = crdt
        self._idem.execute(f"initiate:{txn_id}", op)
        return self.export_state(txn_id)

    def mark_debited(self, txn_id: str, debit_ref: str) -> PaymentCRDT:
        _require_ref("debit_ref", debit_ref)
        return self._transition(txn_id, State.DEBITED, "DEBITED", {"debit_ref": debit_ref})

    def mark_credited(self, txn_id: str, credit_ref: str) -> PaymentCRDT:
        _require_ref("credit_ref", credit_ref)
        return self._transition(txn_id, State.CREDITED, "CREDITED", {"credit_ref": credit_ref})

    def settle(self, txn_id: str) -> PaymentCRDT:
        return self._transition(txn_id, State.SETTLED, "SETTLED", {})

    def fail(self, txn_id: str, reason: str) -> PaymentCRDT:
        return self._transition(txn_id, State.FAILED, "FAILED", {"reason": reason})

    def start_reversal(self, txn_id: str) -> PaymentCRDT:
        return self._transition(txn_id, State.REFUND_PENDING, "REVERSAL_STARTED", {})

    def mark_refunded(self, txn_id: str, refund_ref: str) -> PaymentCRDT:
        _require_ref("refund_ref", refund_ref)
        return self._transition(txn_id, State.REFUNDED, "REFUNDED", {"refund_ref": refund_ref})

    def _transition(self, txn_id: str, dst: State, event: str, proof: dict) -> PaymentCRDT:
        """Idempotent. Always returns the CURRENT exported state, never a stale cache."""
        def op() -> None:
            with self._lock:
                crdt = self._txns.get(txn_id)
                if crdt is None:
                    raise KeyError(f"unknown txn {txn_id}")
                if is_terminal(crdt.state) or crdt.state is dst:
                    return
                assert_transition(crdt.state, dst)
                nxt = crdt.local_update(REPLICA, dst)
                if dst is State.REFUND_PENDING:
                    nxt.reversal_initiated = True
                self._persist(nxt, event, proof)
                self._txns[txn_id] = nxt
        self._idem.execute(f"{event}:{txn_id}", op)
        return self.export_state(txn_id)

    def on_retry(self, txn_id: str) -> dict:
        """Customer tapped Retry. Never creates a new debit for same txn_id;
        tells UI whether a NEW payment is safe."""
        with self._lock:
            crdt = self._txns.get(txn_id)
            if crdt is None:
                return {"decision": "UNKNOWN", "state": None}
            s = crdt.state
            if s in (State.SETTLED, State.CREDITED):
                decision = "ALREADY_PAID"
            elif s in (State.FAILED, State.REFUND_PENDING, State.REFUNDED):
                decision = "SAFE_TO_RETRY_NEW_PAYMENT"
            else:
                decision = "RESOLVING"
            return {"decision": decision, "state": s.value,
                    "reversal_initiated": crdt.reversal_initiated,
                    "conflict": crdt.conflict}

    def apply_remote(self, remote: PaymentCRDT) -> PaymentCRDT:
        """Merge a remote replica state. A locally terminal state is absorbing:
        a conflicting remote terminal never replaces it; the conflict is
        recorded (sticky flag, WAL event) for review instead."""
        with self._lock:
            local = self._txns.get(remote.txn_id)
            if local is None:
                self._persist(remote, "REMOTE_APPLY", {"source": "remote"})
                self._txns[remote.txn_id] = remote
                return remote
            merged = local.merge(remote)
            if is_terminal(local.state) and merged.state is not local.state:
                merged = PaymentCRDT(local.txn_id, local.state, merged.clock,
                                     merged.amount_minor, merged.reversal_initiated,
                                     merged.created_at, local.state_clock, True)
            if (merged.state != local.state or merged.clock.entries != local.clock.entries
                    or merged.conflict != local.conflict
                    or merged.state_clock.entries != local.state_clock.entries):
                event = "CONFLICT" if merged.conflict and not local.conflict else "MERGE"
                self._persist(merged, event, {"remote": remote.to_dict()})
                self._txns[remote.txn_id] = merged
            return merged

    def export_state(self, txn_id: str) -> Optional[PaymentCRDT]:
        with self._lock:
            return self._txns.get(txn_id)

    def reconcile(self, now: Optional[float] = None) -> list:
        """Stuck non-terminal transactions get a definitive outcome:
        DEBITED (no credit) -> REFUND_PENDING; INITIATED -> FAILED.
        FAILED and CREDITED are already definitive/progressing and are left alone.
        Transitions run outside the engine lock (they take their own)."""
        now = time.time() if now is None else now
        with self._lock:
            due = [(t, c.state) for t, c in self._txns.items()
                   if c.state in (State.DEBITED, State.INITIATED)
                   and now - c.created_at >= self.pending_timeout_s]
        actions = []
        for txn_id, seen in due:
            try:
                if seen is State.DEBITED:
                    event, nxt = "REVERSAL_STARTED", self.start_reversal(txn_id)
                else:
                    event, nxt = "FAILED", self.fail(txn_id, "timeout")
            except TransitionError:
                continue  # state moved on concurrently; nothing to reconcile
            if nxt.state in (State.REFUND_PENDING, State.FAILED):
                actions.append((txn_id, event, nxt.state.value))
        return actions
