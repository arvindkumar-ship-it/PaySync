"""PaySync local product adapter around the revised paysync/ core (see docs/CORE_CHANGES.md).

This is a single-process, loopback-only sandbox, never a payment provider.
Run: python server.py   |   Test: python run_tests.py
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from agent import ReliabilityAgent
from paysync.engine import PaymentEngine
from paysync.fsm import State, TransitionError

ROOT = Path(__file__).resolve().parent
SCENARIOS = {"instant", "delayed", "offline", "declined"}
ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
MAX_MINOR = 10_000_000


class ApiError(Exception):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)


class DataLock:
    """Reject concurrent owners of a WAL directory, including on Windows."""
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = open(directory / "owner.lock", "a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            self.file.close()
            raise RuntimeError("Data directory is already in use. Stop the other PaySync process.")

    def close(self):
        self.file.close()


class PaymentService:
    def __init__(self, directory, timeout=20.0):
        self.directory = Path(directory).resolve()
        self.owner = DataLock(self.directory)
        self.lock = threading.RLock()
        self.timeout = timeout
        self.fault = None
        self.version = 0
        self.closed = False
        self.changed = threading.Condition()
        self.db = sqlite3.connect(self.directory / "workspace.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS payments (id TEXT PRIMARY KEY, amount INTEGER NOT NULL, scenario TEXT NOT NULL, note TEXT NOT NULL, created REAL NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()
        self.customer = PaymentEngine(str(self.directory / "customer.jsonl"), pending_timeout_s=timeout)
        self.merchant = PaymentEngine(str(self.directory / "merchant.jsonl"), pending_timeout_s=timeout)
        # Recover the journal-first boundary if a process died after metadata commit.
        for row in self.db.execute("SELECT * FROM payments"):
            if self.customer.export_state(row["id"]) is None:
                self.customer.initiate(row["id"], row["amount"])
        self.online = self.setting("online", "true") == "true"
        self.agent = ReliabilityAgent(self)

    def bump(self):
        with self.changed:
            self.version += 1
            self.changed.notify_all()

    def wake(self):
        with self.changed:
            self.closed = True
            self.changed.notify_all()

    def wait_change(self, since, timeout):
        with self.changed:
            self.changed.wait_for(lambda: self.version != since or self.closed, timeout)
            return self.version

    def close(self):
        self.wake()
        self.customer.close()
        self.merchant.close()
        self.db.close()
        self.owner.close()

    def setting(self, key, default):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def require(self, tid):
        row = self.db.execute("SELECT * FROM payments WHERE id=?", (tid,)).fetchone()
        if row is None:
            raise ApiError("Payment not found.", 404)
        return row

    def sync(self, tid):
        if not self.online:
            raise ApiError("Devices are disconnected. Reconnect before syncing.", 409)
        customer = self.customer.export_state(tid)
        merchant = self.merchant.export_state(tid)
        if customer:
            self.merchant.apply_remote(customer)
        if merchant:
            self.customer.apply_remote(merchant)
        # Exchange the join once more so both exported clocks match.
        self.customer.apply_remote(self.merchant.export_state(tid))
        self.merchant.apply_remote(self.customer.export_state(tid))

    def create(self, body):
        tid, amount = body.get("txn_id"), body.get("amount_minor")
        scenario, note = body.get("scenario", "instant"), body.get("note", "")
        if not isinstance(tid, str) or not ID.fullmatch(tid):
            raise ApiError("A valid payment request ID is required.")
        if type(amount) is not int or not 1 <= amount <= MAX_MINOR:
            raise ApiError("Enter an amount from ₹0.01 to ₹1,00,000.00 using at most two decimal places.")
        if not isinstance(scenario, str) or scenario not in SCENARIOS:
            raise ApiError("Choose a supported payment scenario.")
        if not isinstance(note, str) or len(note) > 120 or any(ord(c) < 32 for c in note):
            raise ApiError("Payment note must be 120 characters or fewer with no control characters.")
        with self.lock:
            row = self.db.execute("SELECT * FROM payments WHERE id=?", (tid,)).fetchone()
            if row:
                if (row["amount"], row["scenario"], row["note"]) != (amount, scenario, note):
                    raise ApiError("This request ID belongs to a different payment.", 409)
                return {"txn_id": tid, "replayed": True}
            self.db.execute("INSERT INTO payments VALUES (?, ?, ?, ?, ?)", (tid, amount, scenario, note, time.time()))
            self.db.commit()
            if scenario == "offline":
                self.set_network(False)
            self.customer.initiate(tid, amount)
            if scenario == "declined":
                self.customer.fail(tid, "simulated_provider_decline")
                if self.online:
                    self.sync(tid)
            else:
                self.customer.mark_debited(tid, "SIM-DEBIT-" + tid)
                if self.online and scenario != "offline":
                    self.sync(tid)
                if self.online and scenario == "instant":
                    self.credit(tid)
            return {"txn_id": tid, "replayed": False}

    def credit(self, tid):
        if not self.online:
            raise ApiError("Reconnect the devices before confirming receipt.", 409)
        # Consult current exported state, not the engine's cached return values.
        current = self.customer.export_state(tid)
        if current.state is State.SETTLED:
            return
        if current.state is State.CREDITED:
            self.sync(tid)
            if self.merchant.export_state(tid).state is State.CREDITED:
                self.merchant.settle(tid)
                self.sync(tid)
            return
        if current.state is not State.DEBITED:
            raise ApiError("This payment can no longer be credited. Check its current status.", 409)
        self.sync(tid)
        if self.merchant.export_state(tid).state is not State.DEBITED:
            raise ApiError("The merchant state cannot accept this credit.", 409)
        self.merchant.mark_credited(tid, "SIM-CREDIT-" + tid)
        self.sync(tid)
        self.merchant.settle(tid)
        self.sync(tid)

    def action(self, tid, action):
        with self.lock:
            self.require(tid)
            if action == "retry":
                return self.customer.on_retry(tid)
            if action == "sync":
                self.sync(tid)
            elif action == "credit":
                self.credit(tid)
            elif action == "refund":
                state = self.customer.export_state(tid).state
                if state is not State.REFUNDED:
                    if state is not State.REFUND_PENDING:
                        raise ApiError("Only a payment awaiting a refund can be refunded.", 409)
                    self.customer.mark_refunded(tid, "SIM-REFUND-" + tid)
                if self.online:
                    self.sync(tid)
            else:
                raise ApiError("Unknown payment action.", 404)
            return {"txn_id": tid}

    def reconcile(self):
        with self.lock:
            # Customer is timeout authority; merchant never times out independently.
            # Synchronize existing credit evidence before deciding about a reversal.
            actions = []
            if self.online:
                for row in self.db.execute("SELECT id FROM payments"):
                    self.sync(row[0])
                    if self.customer.export_state(row[0]).state is State.CREDITED:
                        self.credit(row[0])
                        actions.append((row[0], "SETTLEMENT_RECOVERED", "SETTLED"))
            before = {row[0]: self.customer.export_state(row[0]).state.value for row in self.db.execute("SELECT id FROM payments")}
            actions.extend(action for action in self.customer.reconcile() if before.get(action[0]) != action[2])
            if self.online:
                for tid, _, _ in actions:
                    self.sync(tid)
            return actions

    def set_network(self, online):
        if type(online) is not bool:
            raise ApiError("online must be true or false.")
        with self.lock:
            self.online = online
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('online', ?)", (json.dumps(online),))
            self.db.commit()
            if online:
                for row in self.db.execute("SELECT id FROM payments"):
                    self.sync(row[0])
            return {"online": online}

    def snapshot(self):
        with self.lock:
            payments = []
            histories = {}
            for side in ("customer", "merchant"):
                with open(self.directory / (side + ".jsonl"), encoding="utf-8") as fh:
                    for line in fh:
                        record = json.loads(line)
                        tid = record["crdt"]["txn_id"]
                        histories.setdefault(tid, []).append({"side": side, "event": record["event"], "state": record["crdt"]["state"], "proof": record["proof"]})
            for row in self.db.execute("SELECT * FROM payments ORDER BY created DESC"):
                customer = self.customer.export_state(row["id"])
                merchant = self.merchant.export_state(row["id"])
                payments.append({**dict(row), "customer": customer.to_dict() if customer else None,
                                 "merchant": merchant.to_dict() if merchant else None,
                                 "retry": self.customer.on_retry(row["id"]),
                                 "history": histories.get(row["id"], [])})
            states = [p["customer"]["state"] for p in payments]
            settled = [p for p in payments if p["customer"]["state"] == "SETTLED"]
            return {"payments": payments, "online": self.online, "timeout_s": self.timeout,
                    "mode": "sandbox", "fault": self.fault,
                    "stats": {"total": len(payments), "settled": len(settled),
                              "settled_minor": sum(p["amount"] for p in settled),
                              "pending": sum(s in ("INITIATED", "DEBITED", "CREDITED") for s in states),
                              "refunds": states.count("REFUND_PENDING"),
                              "conflicts": sum(bool((p["customer"] or {}).get("conflict") or (p["merchant"] or {}).get("conflict")) for p in payments),
                              "refunded_minor": sum(p["amount"] for p in payments if p["customer"]["state"] == "REFUNDED")}}

    def export_csv(self):
        text = io.StringIO(newline="")
        writer = csv.writer(text)
        writer.writerow(["transaction_id", "amount_paise", "currency", "state", "scenario", "note", "created_unix"])
        for p in self.snapshot()["payments"]:
            # Prevent spreadsheet formula injection, including tab/CR prefixes.
            note = p["note"]
            if note.lstrip().startswith(("=", "+", "-", "@")):
                note = "'" + note
            writer.writerow([p["id"], p["amount"], "INR", p["customer"]["state"], p["scenario"], note, p["created"]])
        return text.getvalue().encode("utf-8-sig")


class Handler(BaseHTTPRequestHandler):
    server_version = "PaySync/1.1"

    def log_message(self, fmt, *args):
        logging.info("%s %s", self.command, self.path.split("?")[0])

    def respond(self, status, data, mime="application/json; charset=utf-8", extra=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get("Host") in self.server.allowed_hosts

    def do_GET(self):
        if not self.valid_host():
            return self.respond(403, {"error": "Invalid host."})
        path = urlsplit(self.path).path
        try:
            if path == "/api/state":
                return self.respond(200, self.server.service.snapshot())
            if path == "/api/agent":
                return self.respond(200, self.server.service.agent.report())
            if path == "/api/events":
                return self.stream_events()
            if path == "/api/session":
                return self.respond(200, {"csrf_token": self.server.csrf})
            if path == "/api/export":
                return self.respond(200, self.server.service.export_csv(), "text/csv; charset=utf-8", {"Content-Disposition": 'attachment; filename="paysync-transactions.csv"'})
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            if path in assets:
                file, mime = assets[path]
                return self.respond(200, (ROOT / "web" / file).read_bytes(), mime)
            return self.respond(404, {"error": "Not found."})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.exception("Read failed")
            self.respond(503, {"error": "Workspace unavailable. Check the server log."})

    def stream_events(self):
        service = self.server.service
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        seen = -1
        try:
            while not service.closed:
                current = service.wait_change(seen, 15)
                if service.closed:
                    break
                if current != seen:
                    seen = current
                    self.wfile.write(f"event: state\ndata: {current}\n\n".encode())
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def do_POST(self):
        if not self.valid_host():
            return self.respond(403, {"error": "Invalid host."})
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in self.server.allowed_hosts}:
            return self.respond(403, {"error": "Cross-origin request rejected."})
        if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf):
            return self.respond(403, {"error": "Refresh the page to restore your local session."})
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ApiError("JSON body required.", 415)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ApiError("Request size is invalid.", 413)
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ApiError("A JSON object is required.")
            if self.server.service.fault:
                raise ApiError("Writes paused after a storage error. Inspect the server log and restart.", 503)
            path = urlsplit(self.path).path
            if path == "/api/payments":
                result = self.server.service.create(body)
            elif path == "/api/network":
                result = self.server.service.set_network(body.get("online"))
            elif path == "/api/agent/approve":
                try:
                    result = self.server.service.agent.approve(body.get("proposal_id"), "operator")
                except LookupError as e:
                    raise ApiError(str(e), 409)
                except ValueError as e:
                    raise ApiError(str(e))
            elif path == "/api/reconcile":
                result = {"actions": self.server.service.reconcile()}
            else:
                match = re.fullmatch(r"/api/payments/([A-Za-z0-9_-]{8,80})/(retry|sync|credit|refund)", path)
                if not match:
                    raise ApiError("Action not found.", 404)
                result = self.server.service.action(*match.groups())
            self.respond(200, result)
        except ApiError as e:
            self.respond(e.status, {"error": str(e)})
        except (ValueError, UnicodeDecodeError):
            self.respond(400, {"error": "Invalid JSON request."})
        except TransitionError:
            self.respond(409, {"error": "Payment status changed. Refresh and try again."})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.server.service.fault = "storage_error"
            logging.exception("Write failed; mutations paused")
            self.respond(503, {"error": "Payment could not be confirmed. Writes are paused; inspect the server log."})
        finally:
            self.server.service.bump()


class Server(ThreadingHTTPServer):
    def shutdown(self):
        self.service.wake()  # release open event streams
        super().shutdown()


def make_server(directory, port=8765, timeout=20):
    service = PaymentService(directory, timeout)
    server = Server(("127.0.0.1", port), Handler)
    server.service = service
    server.csrf = secrets.token_urlsafe(32)
    server.allowed_hosts = {f"127.0.0.1:{server.server_port}", f"localhost:{server.server_port}"}
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="PaySync local sandbox — no real money is moved")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be at least 1 second")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = make_server(args.data_dir, args.port, args.timeout)
    stopped = threading.Event()
    def sweep():
        while not stopped.wait(1):
            try:
                if not server.service.fault and server.service.reconcile():
                    server.service.bump()
            except Exception:
                server.service.fault = "reconciliation_error"
                logging.exception("Reconciler stopped; writes paused")
    threading.Thread(target=sweep, daemon=True).start()
    print(f"PaySync sandbox: http://127.0.0.1:{server.server_port}", flush=True)
    print("Simulated payments only. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        server.service.wake()
        server.server_close()
        server.service.close()


if __name__ == "__main__":
    main()
