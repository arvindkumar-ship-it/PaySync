# PaySync

**A payment-reliability engine that makes sure a retry never charges you twice, and a crash never loses a payment.**

Built with Python (standard library only), SQLite and vanilla JavaScript. No pip packages, no build step.

> **Sandbox.** Money is simulated and the server runs on localhost only. There is no real bank, UPI or login. See [Limitations](#limitations).

---

## The problem

On a weak network, a customer taps *Pay* and the screen hangs. Did the money leave? Should they tap again? If they do, will they pay twice? If the app crashes mid-payment, what is the true state afterwards?

PaySync answers these questions with a small engine that guarantees:

| Guarantee | How |
|---|---|
| A retry never double-charges | Idempotency keys. Duplicates, even concurrent ones, run once and all get the real result |
| No payment is stuck "pending" forever | A timeout sweep forces every payment to a definite outcome (failed, or refund pending) |
| A crash loses nothing | Write-ahead log, `fsync`ed before a state change counts |
| Customer and merchant always agree | Two independent replicas converge using a CRDT with vector clocks |
| A finished payment can't be silently overwritten | Terminal states are absorbing; conflicting outcomes are flagged for review |

## Before and after

Six defects were found in revision 1 of the engine using regression probes (`audit_engine.py`). All six are fixed in revision 2.

| # | Defect in revision 1 | Real-world effect | Revision 2 |
|---|---|---|---|
| 1 | CRDT merge not associative | Replicas could end in different states depending on sync order | Merge is a true join-semilattice. Verified on 4,000 random triples and all 24 orderings of 4 states |
| 2 | Concurrent duplicate got a placeholder instead of the result | A retry during an in-flight payment got a meaningless answer | Duplicates wait and receive the real result. 64 concurrent duplicates run once |
| 3 | Empty debit/credit proof accepted | A payment could be "confirmed" with no evidence | Empty or blank proofs are rejected |
| 4 | Stale cached result returned | API could say `DEBITED` when the payment was already `SETTLED` | Every call returns the current state |
| 5 | Torn last WAL record crashed startup | A power cut mid-write made the server unable to start | Torn tail is archived and trimmed. Tested at every byte offset |
| 6 | Local `REFUNDED` overwritten by remote `SETTLED` | Money returned to the customer, yet shown as paid | Local terminal state is kept and the conflict is flagged |

Probe results: **6 findings on revision 1, 0 on revision 2** (`qa/engine-audit-original-core.json` vs `qa/engine-audit.json`).

## Measured performance

Run `python bench.py` to reproduce. Measured on a Windows laptop, single thread. Your numbers will differ.

| Metric | Result |
|---|---|
| Full payment lifecycle (initiate, debit, credit, settle) with `fsync` on | ~1,080 payments/s (~0.9 ms each) |
| Same, `fsync` off (shows the durability cost) | ~17,500 payments/s |
| Restart recovery | 20,000 WAL records replayed in ~150 ms |
| 64 concurrent identical requests | 1 execution, 64 correct results |

## Verification

```text
70 tests, all passing
  5 original engine · 22 core regression and property tests
  13 agent guardrail tests · 30 adapter, HTTP and live-sync tests
33 browser checks (desktop and mobile Chrome, real servers, real restarts)
```

## Run it

Requires Python 3.10 or newer.

```sh
python server.py              # open http://127.0.0.1:8765
python run_tests.py           # 70 tests
python audit_engine.py        # defect probes, expect 0 findings
python bench.py               # performance numbers above
```

Browser suite (optional): `pip install playwright`, then `python test_full_product.py`.

## What's in the box

- **Engine** (`paysync/`): state machine, vector clocks, CRDT, write-ahead log, idempotency store.
- **Server** (`server.py`): HTTP API, live updates over server-sent events (no polling), SQLite for request identity, timeout sweeper. Includes CSRF and origin checks, CSP, input limits, HTML escaping and CSV formula neutralisation.
- **Reliability agent** (`agent.py`): reads the payment log, explains what is stuck, and proposes one action (`refund`, `credit`, `sync` or `reconnect`). It **only acts after you approve**. Stale proposals are refused, conflicts are never auto-actioned, and every approval is audit-logged. The planner is rule-based. An optional LLM hook can reword explanations or drop a proposal, but cannot add or change an action.
- **UI** (`web/`): payment room showing the customer and merchant devices side by side, with transactions, refunds and a system page.

## Try these

1. **Instant:** New payment, ₹100, *Instant*. Both sides show `SETTLED`, and retrying says "already paid".
2. **Timeout:** *Delayed credit*, wait for the timeout. The payment reverses to refund pending. Start a new payment, then refund the original.
3. **Offline:** Turn devices off, create an *Interrupted* payment, reconnect, confirm. Both sides converge.
4. **Decline:** *Declined* fails before any debit, so a new payment is safe.

## Limitations

- Both replicas run inside one process. This simulates distribution but is not a real network.
- No authentication, no real payment provider, no signed webhooks. Do not expose publicly.
- Proof strings are generated by the server and are not real bank proofs.
- State snapshots read the full log, which suits small local datasets.
- Benchmarks are single-machine micro-benchmarks, not load tests.