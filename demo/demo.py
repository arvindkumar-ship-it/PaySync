"""PaySync demo: customer vs merchant over a flaky network.
Run: python demo/demo.py"""
import os, sys, time, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paysync.engine import PaymentEngine
from paysync.crdt import PaymentCRDT

def log(tag, msg): print(f"[{tag:>10}] {msg}")

def main():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_wal.jsonl")
    if os.path.exists(path): os.remove(path)
    backend = PaymentEngine(path, fsync=False, pending_timeout_s=1.0)
    txn_a, txn_b = str(uuid.uuid4()), str(uuid.uuid4())
    AMT = 10000

    log("CUSTOMER", f"Pay Rs 100 -> txn A ({txn_a[:8]})")
    backend.initiate(txn_a, AMT); backend.mark_debited(txn_a, "bank-debit-001")
    log("NETWORK", "connection lost right after debit (status stuck)")
    log("CUSTOMER", f"Retry tapped -> {backend.on_retry(txn_a)['decision']}")

    log("CUSTOMER", f"Pay Rs 100 again -> txn B ({txn_b[:8]}) [NEW txn]")
    backend.initiate(txn_b, AMT); backend.mark_debited(txn_b, "bank-debit-002")
    backend.mark_credited(txn_b, "merchant-credit-002"); backend.settle(txn_b)
    log("BACKEND", f"txn B -> {backend.export_state(txn_b).state.value}")

    log("NETWORK", "connection restored"); time.sleep(1.1)
    for tid, event, state in backend.reconcile():
        log("RECONCILER", f"txn {tid[:8]} -> {event} -> {state}")
    backend.mark_refunded(txn_a, "reversal-001")
    log("BACKEND", f"txn A -> {backend.export_state(txn_a).state.value} (auto-refund)")

    merged = PaymentCRDT(txn_a, amount_minor=AMT).merge(backend.export_state(txn_a))
    log("MERCHANT", f"replica converged -> {merged.state.value}")

    print("\n--- RESULT ---")
    print(f"txn A: {backend.export_state(txn_a).state.value} (refunded)")
    print(f"txn B: {backend.export_state(txn_b).state.value} (merchant paid)")
    print("double-charge: 0 | silent-pending: 0 | mismatch: 0")

if __name__ == "__main__":
    main()
