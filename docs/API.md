# Local API and architecture

The browser talks to `server.py` on the same loopback origin. No engine logic
is duplicated in JavaScript. Two `PaymentEngine` instances own separate WALs.

```text
Browser UI → validated, serialized PaymentService
             ├─ customer PaymentEngine → customer.jsonl
             ├─ merchant PaymentEngine → merchant.jsonl
             └─ SQLite request identity / notes / network setting
```

The browser opens one server-sent-events stream (`/api/events`); the server pushes a
version bump only when something changed, and the browser then fetches `/api/state`.
There is no steady polling. The server sweeps timeouts every second. The default timeout is 20 seconds. The UI shows the exported
current state of each replica. There are no fake populated transactions.

| Method | Route | Behavior |
|---|---|---|
| GET | `/api/session` | Returns the process-local CSRF token. No production auth is implied. |
| GET | `/api/state` | Both device exports, retry decisions, per-device journal and metrics. |
| POST | `/api/payments` | `{txn_id, amount_minor, scenario, note}`; initiates and simulates the selected journey. |
| POST | `/api/payments/{id}/retry` | Calls the engine's `on_retry`; never creates a new debit. |
| POST | `/api/payments/{id}/sync` | Exchanges exported CRDTs through `apply_remote` on both engines. |
| POST | `/api/payments/{id}/credit` | Syncs, calls merchant `mark_credited`/`settle`, syncs back. |
| POST | `/api/payments/{id}/refund` | Calls customer `mark_refunded` only for refund-pending state; syncs if connected. |
| POST | `/api/network` | `{online: boolean}`; reconnecting exchanges all known states. |
| POST | `/api/reconcile` | Runs a genuine current-time timeout sweep; no artificial time travel. |
| GET | `/api/events` | Server-sent events. `event: state` with a monotonically increasing version on every change; `: ping` keepalive every 15 s. |
| GET | `/api/agent` | Reliability-agent findings: explanation plus at most one allowlisted proposed action per payment. Read-only. |
| POST | `/api/agent/approve` | `{proposal_id}`; executes that proposal only if it is still exactly what the rules propose now (409 if stale or unknown). Audited in `agent-audit.jsonl`. |
| GET | `/api/export` | CSV containing amount in integer paise, currency, status and note. |

All POSTs require `Content-Type: application/json`, a JSON object body and
`X-CSRF-Token`. Send `{}` for actions with no parameters. The route validates
origin and Host. Static files are explicitly allowlisted. Request IDs are
8–80 ASCII letters/digits/underscore/hyphen. Amounts must be actual integers
from 1 through 10,000,000 paise. Notes permit at most 120 characters.

### Journeys

- `instant`: customer initiate/debit → merchant apply/credit/settle → converge.
- `delayed`: customer initiate/debit → sync; waits for manual merchant credit
  before timeout or moves to refund pending.
- `offline`: disconnects replica exchange, then records the customer debit.
  Reconnect using the switch to exchange the saved state and confirm receipt
  before the timeout. This setting is visible in the payment room.
- `declined`: customer initiate/fail before any debit; sync when connected.

The disconnected switch controls replica exchange, not the browser's HTTP
connection to the server. Offline replica simulation is distinct from losing
the local API. A lost API response preserves its original browser request ID.

All history is grouped by device and ordered within that device's WAL. The
original WAL records have no global wall-clock event timestamps, so the UI
does not invent an inter-device chronological order.

An adapter mutex spans validation, engine mutations and sync. A data directory
owner lock prevents concurrent servers accessing the same files. This is a
single-process local architecture, not a distributed lock or a production
payment database transaction.
