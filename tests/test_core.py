"""Regression + property tests for the core revision (CORE-01..06 fixes)."""
import itertools, json, os, random, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paysync.clocks import VectorClock
from paysync.crdt import PaymentCRDT
from paysync.engine import PaymentEngine
from paysync.fsm import State, TransitionError
from paysync.idempotency import IdempotencyStore
from paysync.wal import WALCorruptionError, WriteAheadLog

ALL = list(State)


def rand_crdt(rng, tid="t"):
    clock = {r: rng.randint(0, 3) for r in ("customer", "merchant", "backend") if rng.random() < .8}
    sc = {k: rng.randint(0, v) for k, v in clock.items()}
    sc = {k: max(v, 0) for k, v in sc.items()}
    full = {k: max(clock.get(k, 0), sc.get(k, 0)) for k in set(clock) | set(sc)}
    return PaymentCRDT(tid, rng.choice(ALL), VectorClock(full), rng.choice([100, 200]),
                       rng.random() < .3, rng.choice([0.0, 5.0, 9.0]), VectorClock(sc),
                       rng.random() < .2)


def rd(path, mode="r"):
    with open(path, mode) as fh:
        return fh.read()


def wr(path, data, mode="w"):
    with open(path, mode) as fh:
        fh.write(data)


def snap(c):
    return json.dumps(c.to_dict(), sort_keys=True)


class CrdtLaws(unittest.TestCase):
    def test_semilattice_laws_randomized(self):
        rng = random.Random(1234)
        for _ in range(4000):
            a, b, c = (rand_crdt(rng) for _ in range(3))
            self.assertEqual(snap(a.merge(b)), snap(b.merge(a)))
            self.assertEqual(snap(a.merge(b).merge(c)), snap(a.merge(b.merge(c))))
            self.assertEqual(snap(a.merge(a)), snap(a))

    def test_all_orders_converge(self):
        rng = random.Random(7)
        for _ in range(300):
            xs = [rand_crdt(rng) for _ in range(4)]
            results = set()
            for perm in itertools.permutations(xs):
                acc = perm[0]
                for x in perm[1:]:
                    acc = acc.merge(x)
                results.add(snap(acc))
            self.assertEqual(len(results), 1)

    def test_core01_audit_counterexample(self):
        a = PaymentCRDT("x", amount_minor=1).local_update("customer", State.DEBITED)
        b = PaymentCRDT("x", amount_minor=1).local_update("merchant", State.FAILED)
        c = a.local_update("customer", State.FAILED)
        self.assertEqual(snap(a.merge(b).merge(c)), snap(a.merge(b.merge(c))))
        self.assertIs(a.merge(b).merge(c).state, State.FAILED)

    def test_causally_newer_wins(self):
        a = PaymentCRDT("x", amount_minor=1).local_update("customer", State.DEBITED)
        b = a.local_update("customer", State.FAILED)
        self.assertIs(a.merge(b).state, State.FAILED)
        self.assertIs(b.merge(a).state, State.FAILED)

    def test_terminal_conflict_is_flagged_and_deterministic(self):
        s = PaymentCRDT("x", amount_minor=1).local_update("m", State.SETTLED)
        r = PaymentCRDT("x", amount_minor=1).local_update("c", State.REFUNDED)
        self.assertIs(s.merge(r).state, State.REFUNDED)
        self.assertIs(r.merge(s).state, State.REFUNDED)
        self.assertTrue(s.merge(r).conflict and r.merge(s).conflict)

    def test_legacy_record_without_new_fields_loads(self):
        d = {"txn_id": "x", "state": "DEBITED", "clock": {"customer": 2},
             "amount_minor": 5, "reversal_initiated": False, "created_at": 1.0}
        c = PaymentCRDT.from_dict(d)
        self.assertEqual(c.state_clock.entries, {"customer": 2})
        self.assertFalse(c.conflict)


class Idempotency(unittest.TestCase):
    def test_core02_concurrent_duplicate_gets_real_result(self):
        store, entered, release = IdempotencyStore(), threading.Event(), threading.Event()
        ran, got = [], []
        def slow():
            ran.append(1); entered.set(); release.wait(5); return "done"
        t1 = threading.Thread(target=lambda: got.append(store.execute("k", slow)))
        t1.start(); entered.wait(5)
        t2 = threading.Thread(target=lambda: got.append(store.execute("k", lambda: ran.append(2) or "dup")))
        t2.start(); time.sleep(0.1); release.set(); t1.join(); t2.join()
        self.assertEqual(ran, [1])
        self.assertEqual(sorted(g[0] for g in got), ["done", "done"])
        self.assertEqual(sorted(g[1] for g in got), [False, True])

    def test_waiter_takes_over_when_first_fails(self):
        store, entered, release = IdempotencyStore(), threading.Event(), threading.Event()
        def bad():
            entered.set(); release.wait(5); raise RuntimeError("boom")
        errs, got = [], []
        def first():
            try: store.execute("k", bad)
            except RuntimeError as e: errs.append(e)
        t1 = threading.Thread(target=first); t1.start(); entered.wait(5)
        t2 = threading.Thread(target=lambda: got.append(store.execute("k", lambda: "second")))
        t2.start(); time.sleep(0.1); release.set(); t1.join(); t2.join()
        self.assertEqual(len(errs), 1)
        self.assertEqual(got, [("second", True)])

    def test_many_threads_single_execution(self):
        store, count = IdempotencyStore(), []
        def op():
            time.sleep(0.05); count.append(1); return 42
        out = []
        ts = [threading.Thread(target=lambda: out.append(store.execute("k", op)[0])) for _ in range(20)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(count), 1)
        self.assertEqual(out, [42] * 20)


_OPEN = []


def engine(tmp, name="wal.jsonl", **kw):
    e = PaymentEngine(os.path.join(tmp, name), fsync=False, **kw)
    _OPEN.append(e)
    return e


class EngineFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
    def tearDown(self):
        while _OPEN:
            try: _OPEN.pop().close()
            except Exception: pass
        self.tmp.cleanup()

    def test_core03_empty_or_blank_proofs_rejected(self):
        e = engine(self.tmp.name); e.initiate("t", 100)
        for bad in ("", "   ", None, 5):
            with self.assertRaises(ValueError): e.mark_debited("t", bad)
        self.assertIs(e.export_state("t").state, State.INITIATED)
        e.mark_debited("t", "d")
        with self.assertRaises(ValueError): e.mark_credited("t", "")
        e.start_reversal("t")
        with self.assertRaises(ValueError): e.mark_refunded("t", "")

    def test_core04_returned_state_is_current(self):
        e = engine(self.tmp.name); e.initiate("t", 100)
        e.mark_debited("t", "d"); e.mark_credited("t", "c"); e.settle("t")
        self.assertIs(e.mark_debited("t", "d").state, State.SETTLED)
        self.assertIs(e.initiate("t", 100).state, State.SETTLED)

    def test_core05_torn_tail_recovered_and_archived(self):
        e = engine(self.tmp.name); e.initiate("t", 100); e.mark_debited("t", "d"); e.close()
        path = os.path.join(self.tmp.name, "wal.jsonl")
        wr(path, '{"event":"CRED', "a")
        e2 = engine(self.tmp.name)
        self.assertIs(e2.export_state("t").state, State.DEBITED)
        self.assertGreater(e2.recovered_torn_bytes, 0)
        self.assertTrue(os.path.exists(path + ".torn"))
        e2.mark_credited("t", "c"); e2.close()
        e3 = engine(self.tmp.name)
        self.assertIs(e3.export_state("t").state, State.CREDITED)
        self.assertEqual(e3.recovered_torn_bytes, 0)

    def test_core05_every_truncation_point_recovers(self):
        e = engine(self.tmp.name, "full.jsonl"); e.initiate("t", 100); e.mark_debited("t", "d")
        e.mark_credited("t", "c"); e.settle("t"); e.close()
        raw = rd(os.path.join(self.tmp.name, "full.jsonl"), "rb")
        ends = [i + 1 for i, b in enumerate(raw) if b == 10]
        order = [State.INITIATED, State.DEBITED, State.CREDITED, State.SETTLED]
        for cut in range(len(raw) + 1):
            name = f"cut{cut}.jsonl"
            wr(os.path.join(self.tmp.name, name), raw[:cut], "wb")
            e2 = engine(self.tmp.name, name)
            complete = sum(1 for x in ends if x <= cut)
            tail_is_whole = cut == len(raw) or (cut > (ends[complete - 1] if complete else 0)
                                               and self._whole(raw[(ends[complete - 1] if complete else 0):cut]))
            expected = complete + (1 if tail_is_whole and cut not in ends and cut > 0 else 0)
            st = e2.export_state("t")
            if expected == 0:
                self.assertIsNone(st)
            else:
                self.assertIs(st.state, order[min(expected, 4) - 1], f"cut={cut}")
            e2.close()

    @staticmethod
    def _whole(b):
        try: json.loads(b.decode()); return True
        except ValueError: return False

    def test_complete_record_missing_newline_is_kept_and_next_append_safe(self):
        e = engine(self.tmp.name); e.initiate("t", 100); e.close()
        path = os.path.join(self.tmp.name, "wal.jsonl")
        wr(path, rd(path, "rb").rstrip(b"\n"), "wb")
        e2 = engine(self.tmp.name); e2.mark_debited("t", "d"); e2.close()
        self.assertIs(engine(self.tmp.name).export_state("t").state, State.DEBITED)

    def test_mid_file_corruption_still_fails_loudly(self):
        e = engine(self.tmp.name); e.initiate("t", 100); e.mark_debited("t", "d"); e.close()
        path = os.path.join(self.tmp.name, "wal.jsonl")
        lines = rd(path).splitlines()
        wr(path, "garbage\n" + "\n".join(lines[1:]) + "\n")
        with self.assertRaises(WALCorruptionError): engine(self.tmp.name)

    def test_core06_local_terminal_is_absorbing(self):
        e = engine(self.tmp.name); e.initiate("t", 100); e.mark_debited("t", "d")
        e.start_reversal("t"); e.mark_refunded("t", "r")
        remote = PaymentCRDT("t", amount_minor=100).local_update("merchant", State.SETTLED)
        got = e.apply_remote(remote)
        self.assertIs(got.state, State.REFUNDED)
        self.assertIs(e.export_state("t").state, State.REFUNDED)
        self.assertTrue(e.export_state("t").conflict)
        self.assertTrue(e.on_retry("t")["conflict"])

    def test_core06_settled_not_replaced_by_remote_refunded_and_survives_restart(self):
        e = engine(self.tmp.name); e.initiate("t", 100); e.mark_debited("t", "d")
        e.mark_credited("t", "c"); e.settle("t")
        e.apply_remote(PaymentCRDT("t", amount_minor=100).local_update("m", State.REFUNDED))
        self.assertIs(e.export_state("t").state, State.SETTLED)
        e.close()
        e2 = engine(self.tmp.name)
        self.assertIs(e2.export_state("t").state, State.SETTLED)
        self.assertTrue(e2.export_state("t").conflict)

    def test_idempotency_survives_restart(self):
        e = engine(self.tmp.name); e.initiate("t", 100); e.mark_debited("t", "d")
        e.mark_credited("t", "c"); e.close()
        e2 = engine(self.tmp.name)
        self.assertIs(e2.mark_debited("t", "d").state, State.CREDITED)   # no TransitionError
        self.assertIs(e2.mark_credited("t", "c").state, State.CREDITED)
        before = rd(os.path.join(self.tmp.name, "wal.jsonl"))
        e2.mark_debited("t", "d"); e2.mark_credited("t", "c"); e2.initiate("t", 100)
        self.assertEqual(rd(os.path.join(self.tmp.name, "wal.jsonl")), before)

    def test_reconcile_after_restart_does_not_crash_on_failed(self):
        e = engine(self.tmp.name, pending_timeout_s=0.01)
        e.initiate("a", 100); e.fail("a", "x")
        e.initiate("b", 100); e.mark_debited("b", "d")
        e.close(); time.sleep(0.05)
        e2 = engine(self.tmp.name, pending_timeout_s=0.01)
        acts = e2.reconcile()
        self.assertEqual(acts, [("b", "REVERSAL_STARTED", "REFUND_PENDING")])
        self.assertEqual(e2.reconcile(), [])

    def test_reconcile_on_remote_failed_state_does_not_raise(self):
        e = engine(self.tmp.name, pending_timeout_s=0.0)
        e.apply_remote(PaymentCRDT("r", amount_minor=5, created_at=1.0).local_update("m", State.FAILED))
        self.assertEqual(e.reconcile(), [])

    def test_reconcile_and_concurrent_transition_do_not_deadlock(self):
        e = engine(self.tmp.name, pending_timeout_s=0.0)
        for i in range(30):
            e.initiate(f"t{i}", 100); e.mark_debited(f"t{i}", "d")
        errs = []
        def go(fn):
            try: fn()
            except TransitionError: pass
            except Exception as ex: errs.append(ex)
        ts = [threading.Thread(target=go, args=(e.reconcile,)) for _ in range(4)]
        ts += [threading.Thread(target=go, args=(lambda i=i: e.mark_credited(f"t{i}", "c"),)) for i in range(30)]
        [t.start() for t in ts]; [t.join(15) for t in ts]
        self.assertFalse(any(t.is_alive() for t in ts))
        self.assertEqual(errs, [])

    def test_wal_roundtrip_preserves_new_fields(self):
        e = engine(self.tmp.name); e.initiate("t", 100); e.mark_debited("t", "d"); e.close()
        c = engine(self.tmp.name).export_state("t")
        self.assertEqual(c.state_clock.entries, c.clock.entries)


if __name__ == "__main__":
    unittest.main()
