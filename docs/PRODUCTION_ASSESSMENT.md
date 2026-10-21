# Production assessment — 21 October 2026

**Status: LOCAL SANDBOX. Real-money production release is still blocked, for reasons outside the core algebra (below).**

## Core findings: resolved

The six defects reproduced in the originally supplied engine (CORE-01..06) are fixed and
regression-tested. See [CORE_CHANGES.md](CORE_CHANGES.md) for each defect, fix and test.

| ID | Original behavior | Status |
|---|---|---|
| CORE-01 | Merge not associative | Fixed: join-semilattice; 4000 random triples + all-order convergence tests |
| CORE-02 | Concurrent duplicate got an opaque placeholder | Fixed: duplicates wait for and receive the real result |
| CORE-03 | Empty proof strings accepted | Fixed: rejected with `ValueError` |
| CORE-04 | Stale cached result | Fixed: mutators return current exported state |
| CORE-05 | Torn final WAL record blocked startup | Fixed: archived to `.torn` and truncated; mid-file corruption still fails loudly |
| CORE-06 | Local `REFUNDED` replaceable by remote `SETTLED` | Fixed: terminal absorbing locally; conflict flagged for review |

`python audit_engine.py` now reports 0 findings; against the original core it reports 6
(`python audit_engine.py --core <original>`), so the probes are not vacuous.

## What the product adds

- One process owns each data directory; adapter mutations are serialized.
- SQLite preserves request identities across restart; repeated creation with different parameters is rejected.
- Idempotency keys are rebuilt from the engine WAL after restart.
- Live updates use server-sent events (no steady polling); a lost response replays the same request ID and never double-pays (browser-tested).
- A reliability agent explains stuck/refund-pending/conflicting payments and proposes one allowlisted action; execution requires explicit approval and is audited.
- Simulated proof strings are generated only by the server (`SIM-` prefix). They are not verified financial proofs.
- A runtime reconciliation/storage error pauses further writes.

## Required before handling money

1. Integrate a real provider with verified debit/credit/refund outcomes, signed webhook validation, immutable amount/payee binding and a durable financial ledger. Provider idempotency must cover external side effects and crash windows.
2. Add authentication, per-user/workspace authorization, session lifecycle, secret management, TLS and deployment isolation. This workspace has no login and must not be publicly exposed.
3. Replace the development HTTP server with a reviewed deployment architecture; design multi-process ownership, job processing, bounded history queries, rate limits, retention and back-pressure. The snapshot reads complete logs and is intended for small local datasets.
4. Exercise actual power-loss recovery, failed fsync/disk-full, delayed/duplicate/conflicting provider events, backup/restore and sustained load. Add observability and recovery procedures.
5. Complete an independent security and financial-correctness review of the revised core and the deployment.

Not implemented: real UPI/QR acceptance, Reed–Solomon QR recovery, real bank refunds, multi-user authentication, cloud hosting, a provider connector.

## Evidence and limits

`qa/test-results.txt` has the unit/integration run; `qa/browser-checks.md` and `qa/full-run-summary.txt`
the real-browser run; `qa/engine-audit*.json` the probes against the original and current core;
`qa/core-integrity.json` the manifest check. Success is scoped to those checks; it is not a guarantee
about untested production environments or real financial transactions.
