import os, tempfile, time, uuid, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paysync.engine import PaymentEngine
from paysync.fsm import State, TransitionError
from paysync.crdt import PaymentCRDT
from paysync.clocks import Causal, VectorClock


def new_engine(tmp):
    return PaymentEngine(os.path.join(tmp, "wal.jsonl"), fsync=False, pending_timeout_s=0.05)


def test_duplicate_retries_collapse():
    with tempfile.TemporaryDirectory() as tmp:
        e = new_engine(tmp)
        tid = str(uuid.uuid4())
        e.initiate(tid, 10000)
        a = e.mark_debited(tid, "d1")
        b = e.mark_debited(tid, "d1")
        assert a.state is State.DEBITED and b.state is State.DEBITED
        try:
            e.settle(tid); assert False
        except TransitionError:
            pass
        e.mark_credited(tid, "c1"); e.settle(tid)
        assert e.export_state(tid).state is State.SETTLED
        e.fail(tid, "late")
        assert e.export_state(tid).state is State.SETTLED


def test_stuck_debit_reverses_and_allows_retry():
    with tempfile.TemporaryDirectory() as tmp:
        e = new_engine(tmp)
        tid = str(uuid.uuid4())
        e.initiate(tid, 10000); e.mark_debited(tid, "d1")
        time.sleep(0.08)
        actions = e.reconcile()
        assert actions and actions[0][1] == "REVERSAL_STARTED"
        assert e.export_state(tid).state is State.REFUND_PENDING
        assert e.on_retry(tid)["decision"] == "SAFE_TO_RETRY_NEW_PAYMENT"
        e.mark_refunded(tid, "r1")
        assert e.export_state(tid).state is State.REFUNDED


def test_crash_recovery_from_wal():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "wal.jsonl")
        e1 = PaymentEngine(path, fsync=False)
        tid = str(uuid.uuid4())
        e1.initiate(tid, 25000); e1.mark_debited(tid, "d1"); e1.mark_credited(tid, "c1")
        e2 = PaymentEngine(path, fsync=False)
        assert e2.export_state(tid).state is State.CREDITED
        e2.settle(tid)
        assert e2.export_state(tid).state is State.SETTLED


def test_crdt_concurrent_merge_converges():
    tid = str(uuid.uuid4())
    a = PaymentCRDT(tid, amount_minor=10000).local_update("customer", State.DEBITED)
    b = PaymentCRDT(tid, amount_minor=10000).local_update("merchant", State.FAILED)
    m1 = a.merge(b).merge(a); m2 = b.merge(a).merge(b)
    assert m1.state is m2.state and m1.clock.entries == m2.clock.entries
    settled = PaymentCRDT(tid, amount_minor=10000).local_update("backend", State.SETTLED)
    assert a.merge(b).merge(settled).state is State.SETTLED


def test_vector_clock_causality():
    v0 = VectorClock(); v1 = v0.incremented("a"); v2 = v1.incremented("b")
    assert v0.compare(v2) is Causal.BEFORE
    assert v2.compare(v0) is Causal.AFTER
    assert v0.incremented("a").compare(v0.incremented("b")) is Causal.CONCURRENT
