"""Reproducible micro-benchmarks for the PaySync engine.  Run: python bench.py"""
import os, statistics, tempfile, threading, time
from paysync.engine import PaymentEngine
from paysync.idempotency import IdempotencyStore


def payments_per_sec(fsync, n):
    with tempfile.TemporaryDirectory() as d:
        e = PaymentEngine(os.path.join(d, "w.jsonl"), fsync=fsync)
        t0 = time.perf_counter()
        for i in range(n):
            t = f"bench{i:06d}"
            e.initiate(t, 10000); e.mark_debited(t, "d"); e.mark_credited(t, "c"); e.settle(t)
        dt = time.perf_counter() - t0
        e.close()
    return n / dt, dt / n * 1000


def replay_time(n):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "w.jsonl")
        e = PaymentEngine(p, fsync=False)
        for i in range(n):
            t = f"bench{i:06d}"
            e.initiate(t, 10000); e.mark_debited(t, "d"); e.mark_credited(t, "c"); e.settle(t)
        e.close()
        recs = sum(1 for _ in open(p))
        t0 = time.perf_counter(); e2 = PaymentEngine(p, fsync=False); dt = time.perf_counter() - t0
        e2.close()
    return recs, dt * 1000


def duplicate_storm(threads=64):
    store, runs, lock, out = IdempotencyStore(), [0], threading.Lock(), []
    def op():
        with lock: runs[0] += 1
        time.sleep(0.05); return "ok"
    def w(): out.append(store.execute("k", op)[0])
    ts = [threading.Thread(target=w) for _ in range(threads)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return threads, runs[0], all(r == "ok" for r in out)


if __name__ == "__main__":
    for fs in (True, False):
        r, ms = payments_per_sec(fs, 300 if fs else 2000)
        print(f"fsync={fs}: {r:,.0f} full payments/s ({ms:.2f} ms each, 4 durable writes per payment)")
    recs, ms = replay_time(5000)
    print(f"restart replay: {recs:,} WAL records rebuilt in {ms:.0f} ms")
    n, runs, ok = duplicate_storm()
    print(f"duplicate storm: {n} concurrent identical requests -> {runs} execution(s), all got real result: {ok}")
