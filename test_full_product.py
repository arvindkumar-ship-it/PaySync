"""PaySync: fresh-data API, process-restart and desktop/mobile browser tests.

Copy this file beside server.py. Install Playwright, then run:
    python test_full_product.py --headed

Never connects to an already running server or uses the project's data/ folder.
All writes go to a unique test-results/<run-id>/ directory. Core is not edited.
Set PAYSYNC_CHROMIUM=/path/to/chrome to use an existing Chrome/Chromium.
Exit: 0=covered checks passed; 1=check failed; 2=a historical core defect reproduces;
      3=incomplete run (for example --api-only).
This tests the local sandbox. It cannot verify real bank/provider transactions.
"""
from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def unique_id():
    return uuid.uuid4().hex


class Results:
    def __init__(self, folder):
        self.folder = folder
        self.rows = []
        self.blockers = []
        self.browser_executed = False

    def case(self, name, operation):
        started = time.monotonic()
        print(f"RUN   {name}", flush=True)
        try:
            operation()
            row = {"name": name, "status": "PASS"}
        except Exception as error:
            row = {"name": name, "status": "FAIL", "error": str(error),
                   "traceback": traceback.format_exc()}
        row["seconds"] = round(time.monotonic() - started, 3)
        self.rows.append(row)
        print(f"{row['status']:5} {name}" + (f": {row['error']}" if row['status'] == "FAIL" else ""), flush=True)
        self.save()
        return row["status"] == "PASS"

    def save(self):
        failed = [row for row in self.rows if row["status"] == "FAIL"]
        result = {"checks": self.rows, "failed_checks": len(failed),
                  "passed_checks": len(self.rows) - len(failed),
                  "browser_executed": self.browser_executed,
                  "core_audit_findings": self.blockers,
                  "scope": "Local sandbox; simulated provider proofs",
                  "production_certified": False}
        (self.folder / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        lines = [f"{r['status']:5} {r['name']}" + (f" - {r['error']}" if r['status'] == "FAIL" else "") for r in self.rows]
        lines += ["", f"Browser executed: {self.browser_executed}",
                  f"Core audit findings: {len(self.blockers)}",
                  "A successful local run is not real-money production certification."]
        (self.folder / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


class Server:
    """Only owns the subprocess it starts; never kills another application."""
    def __init__(self, project, folder, timeout=3):
        self.project, self.folder, self.timeout = project, folder, timeout
        folder.mkdir(parents=True, exist_ok=True)
        self.data = folder / "isolated-data"
        self.log_path = folder / "server.log"
        self.process = None
        self.log = None
        self.port = 0
        self.base = ""
        self.token = ""

    def start(self):
        offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        self.log = self.log_path.open("ab")
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "server.py", "--port", str(self.port),
             "--data-dir", str(self.data), "--timeout", str(self.timeout)],
            cwd=self.project, env=env, stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Test server exited. Inspect {self.log_path}")
            content = self.log_path.read_bytes()[offset:].decode("utf-8", "replace")
            match = re.search(r"http://127\.0\.0\.1:(\d+)", content)
            if match:
                self.port = int(match.group(1))
                self.base = f"http://127.0.0.1:{self.port}"
                try:
                    self.token = self.get("/api/session")["csrf_token"]
                    return
                except (OSError, URLError):
                    pass
            time.sleep(0.1)
        raise TimeoutError(f"Test server did not start. Inspect {self.log_path}")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.log:
            self.log.close()
            self.log = None

    def restart(self):
        self.stop()
        self.start()

    def raw(self, path, body=None, headers=None, raw_body=None):
        hdr = {}
        data = None
        if body is not None or raw_body is not None:
            hdr = {"Content-Type": "application/json", "X-CSRF-Token": self.token}
            data = raw_body if raw_body is not None else json.dumps(body).encode("utf-8")
        hdr.update(headers or {})
        request = Request(self.base + path, data=data, headers=hdr,
                          method="POST" if data is not None else "GET")
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.read(), dict(response.headers)
        except HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def call(self, path, body=None, expected=200, **kwargs):
        status, data, _ = self.raw(path, body, **kwargs)
        require(status == expected, f"{path}: expected HTTP {expected}, got {status}: {data[:300]!r}")
        return json.loads(data)

    def get(self, path):
        return self.call(path)

    def create(self, scenario="instant", amount=10000, note="Test payment", tid=None):
        payload = {"txn_id": tid or unique_id(), "amount_minor": amount,
                   "scenario": scenario, "note": note}
        self.call("/api/payments", payload)
        return payload

    def payment(self, tid):
        return next(p for p in self.get("/api/state")["payments"] if p["id"] == tid)

    def action(self, tid, action, expected=200):
        return self.call(f"/api/payments/{tid}/{action}", {}, expected=expected)

    def wait_state(self, tid, target, seconds=None):
        deadline = time.monotonic() + (seconds or self.timeout + 5)
        last = None
        while time.monotonic() < deadline:
            snapshot = self.get("/api/state")
            require(not snapshot["fault"], f"Workspace fault: {snapshot['fault']}; inspect {self.log_path}")
            last = next(p for p in snapshot["payments"] if p["id"] == tid)
            if last["customer"]["state"] == target:
                return last
            time.sleep(0.15)
        raise AssertionError(f"{tid}: expected {target}, last state was {last}")

    def settled(self, tid):
        p = self.payment(tid)
        require(p["customer"]["state"] == "SETTLED", "Customer did not settle")
        require(p["merchant"] == p["customer"], "Device exports do not match")


def child_tool(project, folder, filename, json_output=False, allow_fail=False):
    result = subprocess.run([sys.executable, filename], cwd=project,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=180,
                            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    (folder / (filename + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
    require(result.returncode == 0 or allow_fail, f"{filename} failed; inspect its saved log")
    return json.loads(result.stdout) if json_output else result.stdout


def api_tests(server, result):
    def check(name, fn):
        def isolated():
            server.call("/api/network", {"online": True})
            fn()
        result.case("API: " + name, isolated)

    def fresh():
        snapshot = server.get("/api/state")
        require(snapshot["payments"] == [], "Fresh workspace was not empty")
        require(snapshot["stats"]["total"] == 0, "Fresh counters were not zero")
    check("empty data and zero counters", fresh)

    def assets():
        for path, mime in [("/", "text/html"), ("/app.js", "text/javascript"),
                           ("/styles.css", "text/css"), ("/favicon.svg", "image/svg+xml")]:
            code, body, hdr = server.raw(path)
            require(code == 200 and body, f"Missing asset {path}")
            require(mime in hdr.get("Content-Type", ""), f"Wrong MIME type: {path}")
            require("frame-ancestors 'none'" in hdr.get("Content-Security-Policy", ""), "Missing CSP")
            require(hdr.get("X-Content-Type-Options") == "nosniff", "Missing nosniff")
    check("actual frontend assets and response headers", assets)

    def instant():
        payload = server.create()
        tid = payload["txn_id"]
        server.settled(tid)
        before = len(server.get("/api/state")["payments"])
        require(server.action(tid, "retry")["decision"] == "ALREADY_PAID", "Wrong paid retry decision")
        require(len(server.get("/api/state")["payments"]) == before, "Retry created a payment")
    check("instant settlement and already-paid retry", instant)

    def duplicate():
        payload = server.create()
        again = server.call("/api/payments", payload)
        require(again["replayed"] is True, "Duplicate was not replayed")
        history = server.payment(payload["txn_id"])["history"]
        require(sum(h["event"] == "DEBITED" for h in history) == 1, "Duplicate debit journal entry")
        server.call("/api/payments", dict(payload, amount_minor=20000), expected=409)
    check("duplicate collapse and amount binding", duplicate)

    def concurrent():
        payload = dict(txn_id=unique_id(), amount_minor=10000, scenario="instant", note="Concurrent retry")
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: server.call("/api/payments", payload), range(24)))
        require(sum(not r["replayed"] for r in responses) == 1, "More than one initial request executed")
        history = server.payment(payload["txn_id"])["history"]
        require(sum(h["event"] == "DEBITED" for h in history) == 1, "Concurrent request duplicated debit")
        server.settled(payload["txn_id"])
    check("24 concurrent duplicate HTTP requests", concurrent)

    def delayed_credit():
        tid = server.create("delayed")["txn_id"]
        require(server.action(tid, "retry")["decision"] == "RESOLVING", "Pending retry should resolve first")
        server.action(tid, "credit")
        server.settled(tid)
    check("merchant confirmation before timeout", delayed_credit)

    def refund_journey():
        a = server.create("delayed", note="Original 100")["txn_id"]
        server.wait_state(a, "REFUND_PENDING")
        require(server.action(a, "retry")["decision"] == "SAFE_TO_RETRY_NEW_PAYMENT", "Retry was not safe")
        server.action(a, "credit", expected=409)
        b = server.create(note="Second 100")["txn_id"]
        require(a != b, "New payment reused original ID")
        server.action(a, "refund")
        server.action(a, "refund")
        old = server.payment(a)
        require(old["customer"]["state"] == "REFUNDED", "Original was not refunded")
        require(old["customer"] == old["merchant"], "Refund did not converge")
        require(sum(h["event"] == "REFUNDED" for h in old["history"]) == 1, "Repeated refund wrote another event")
        server.settled(b)
    check("100 pending -> new 100 -> original refund; late credit blocked", refund_journey)

    def disconnected():
        server.call("/api/network", {"online": False})
        tid = server.create("offline")["txn_id"]
        require(server.payment(tid)["merchant"] is None, "Merchant received a disconnected payment")
        server.action(tid, "credit", expected=409)
        server.action(tid, "sync", expected=409)
        server.call("/api/network", {"online": True})
        server.action(tid, "sync")
        server.action(tid, "credit")
        server.settled(tid)
    check("disconnected replicas -> reconnect -> confirm -> convergence", disconnected)

    def offline_refund():
        server.call("/api/network", {"online": False})
        tid = server.create("offline")["txn_id"]
        server.wait_state(tid, "REFUND_PENDING")
        server.action(tid, "refund")
        require(server.payment(tid)["merchant"] is None, "Offline merchant should be stale")
        server.call("/api/network", {"online": True})
        p = server.payment(tid)
        require(p["merchant"] == p["customer"], "Offline refund did not converge on reconnect")
    check("refund while disconnected -> reconnect", offline_refund)

    def declined():
        tid = server.create("declined")["txn_id"]
        p = server.payment(tid)
        require(p["customer"]["state"] == "FAILED", "Decline state is wrong")
        require(not any(h["event"] == "DEBITED" for h in p["history"]), "Decline created a debit")
        require(server.action(tid, "retry")["decision"] == "SAFE_TO_RETRY_NEW_PAYMENT", "Decline retry decision wrong")
    check("decline before debit and safe retry", declined)

    def money_validation():
        before = server.get("/api/state")["stats"]["total"]
        for amount in (0, -1, 1.5, True, "10000", None, 10000001):
            server.call("/api/payments", dict(txn_id=unique_id(), amount_minor=amount, scenario="instant", note="Invalid money"), expected=400)
        require(server.get("/api/state")["stats"]["total"] == before, "Invalid money created a payment")
        for amount in (1, 10000000):
            payload = server.create(amount=amount)
            require(server.payment(payload["txn_id"])["amount"] == amount, "Integer money boundary changed")
    check("integer paise, lower/upper limits, invalid inputs", money_validation)

    def protocol_validation():
        server.call("/api/payments", expected=400, raw_body=b'{bad')
        server.call("/api/payments", expected=400, raw_body=b'[]')
        server.call("/api/payments", {}, expected=415, headers={"Content-Type": "text/plain"})
        server.call("/api/payments", expected=413, raw_body=b' ' * 8193)
        server.call("/api/network", {"online": "false"}, expected=400)
        server.action("unknown_payment_123", "retry", expected=404)
        server.call("/api/not-a-route", {}, expected=404)
    check("malformed JSON, size, content type and unknown requests", protocol_validation)

    def payload_validation():
        for extra in ({"txn_id": "../bad"}, {"scenario": []}, {"scenario": {}},
                      {"scenario": "unknown"}, {"note": "x" * 121}, {"note": "\tbad"}):
            payload = dict(txn_id=unique_id(), amount_minor=10000, scenario="instant", note="Valid")
            payload.update(extra)
            server.call("/api/payments", payload, expected=400)
        require(server.get("/api/state")["fault"] is None, "Malformed payload paused the workspace")
    check("invalid IDs, journeys and notes cannot damage workspace", payload_validation)

    def protections():
        server.call("/api/network", {"online": False}, expected=403, headers={"X-CSRF-Token": ""})
        server.call("/api/network", {"online": False}, expected=403, headers={"Origin": "https://invalid.example"})
        require(server.raw("/api/state", headers={"Host": "invalid.example"})[0] == 403, "Invalid Host accepted")
        for path in ("/server.py", "/data/customer.jsonl", "/../server.py"):
            require(server.raw(path)[0] == 404, f"Private path exposed: {path}")
    check("CSRF, cross-origin, Host and private-file protections", protections)

    def terminal_safety():
        tid = server.create()["txn_id"]
        server.action(tid, "refund", expected=409)
        before = server.payment(tid)["customer"]
        server.action(tid, "credit")
        server.action(tid, "sync")
        require(server.payment(tid)["customer"] == before, "Late duplicate changed settled state")
    check("illegal refund and duplicate settled credit", terminal_safety)

    def csv_export():
        p = server.create(note="=SUM(1,2)")
        code, data, headers = server.raw("/api/export")
        require(code == 200 and "text/csv" in headers.get("Content-Type", ""), "CSV unavailable")
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        row = next(r for r in rows if r["transaction_id"] == p["txn_id"])
        require(row["amount_paise"] == "10000" and row["currency"] == "INR", "CSV money mismatch")
        require(row["note"] == "'=SUM(1,2)", "CSV formula not neutralized")
        (result.folder / "api-transactions.csv").write_bytes(data)
    check("CSV content, paise precision and formula neutralization", csv_export)

    def restart_recovery():
        payload = server.create(note="Terminate process and replay")
        tid = payload["txn_id"]
        before = server.payment(tid)
        server.restart()
        after = server.payment(tid)
        require(before["customer"] == after["customer"], "Restart lost customer state")
        require(before["merchant"] == after["merchant"], "Restart lost merchant state")
        require(len(before["history"]) == len(after["history"]), "Restart changed journal unexpectedly")
        require(server.call("/api/payments", payload)["replayed"], "Restart lost durable request identity")
    check("real subprocess termination and WAL/request recovery", restart_recovery)

    def timeout_after_restart():
        server.call("/api/network", {"online": False})
        tid = server.create("offline")["txn_id"]
        server.restart()
        require(server.get("/api/state")["online"] is False, "Network setting was not restored")
        server.wait_state(tid, "REFUND_PENDING")
        server.action(tid, "refund")
        server.call("/api/network", {"online": True})
        p = server.payment(tid)
        require(p["customer"] == p["merchant"], "Restarted refund did not converge")
    check("pending debit survives restart and still times out/refunds", timeout_after_restart)

    def metrics():
        snapshot = server.get("/api/state")
        payments, stats = snapshot["payments"], snapshot["stats"]
        require(stats["total"] == len(payments), "Payment count wrong")
        require(stats["settled_minor"] == sum(p["amount"] for p in payments if p["customer"]["state"] == "SETTLED"), "Settled total wrong")
        require(stats["refunded_minor"] == sum(p["amount"] for p in payments if p["customer"]["state"] == "REFUNDED"), "Refund total wrong")
        require(stats["refunds"] == sum(p["customer"]["state"] == "REFUND_PENDING" for p in payments), "Refund queue count wrong")
        require(snapshot["fault"] is None, "Workspace entered a fault state")
    check("dashboard totals match exported engine states", metrics)


def browser_tests(project, folder, result, headed):
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as playwright:
        executable = os.environ.get("PAYSYNC_CHROMIUM")  # optional: use an existing Chrome/Chromium
        browser = playwright.chromium.launch(headless=not headed, **({"executable_path": executable} if executable else {}))
        result.browser_executed = True
        try:
            for width, height in [(1440, 1000), (390, 844)]:
                artifact = folder / f"browser-{width}"
                server = Server(project, artifact, timeout=12)
                context = browser.new_context(viewport={"width": width, "height": height},
                                              accept_downloads=True,
                                              permissions=["clipboard-read", "clipboard-write"])
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
                page = context.new_page()
                page.set_default_timeout(10000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def snapshot(name):
                    page.screenshot(path=str(artifact / f"{name}.png"), full_page=True)

                def navigate(name):
                    page.get_by_role("link", name=re.compile(rf"^{name}(\s*\d+)?$")).click()  # nav badge may append a count

                def status(label):
                    expect(page.locator(".phone:not(.merchant) .status")).to_have_text(label)
                    expect(page.locator(".phone.merchant .status")).to_have_text(label)

                def create(scenario, note, amount="100.00"):
                    page.get_by_role("button", name="New payment", exact=True).click()
                    page.get_by_label("Payment amount", exact=True).fill(amount)
                    page.get_by_label("Try a payment journey", exact=True).select_option(scenario)
                    page.get_by_label("Note (optional)", exact=True).fill(note)
                    with page.expect_response(lambda r: urlsplit(r.url).path == "/api/payments" and r.request.method == "POST") as response:
                        page.get_by_role("button", name="Create payment", exact=True).click()
                    require(response.value.status == 200, "Browser payment request failed")
                    tid = response.value.json()["txn_id"]
                    expect(page.locator("#payment-dialog")).not_to_be_visible()
                    expect(page.locator("#payment-select")).to_have_value(tid)
                    return tid

                def journey():
                    server.start()
                    page.goto(server.base)
                    expect(page.get_by_role("heading", name="A good day to be in sync.", exact=True)).to_be_visible()
                    require(server.get("/api/state")["payments"] == [], "Browser did not start on fresh data")
                    require(page.locator('.side-note [data-icon="spark"]').count() == 0, "Sidebar sparkle is still present")
                    require(page.locator('.system-link [data-icon="shield"]').count() == 0, "Sidebar shield is still present")
                    snapshot("01-empty-overview")

                    page.get_by_role("button", name="New payment", exact=True).click()
                    page.get_by_label("Payment amount", exact=True).fill("100.999")
                    page.get_by_role("button", name="Create payment", exact=True).click()
                    expect(page.locator("#form-error")).to_be_visible()
                    require(server.get("/api/state")["stats"]["total"] == 0, "Invalid form created a transaction")
                    page.get_by_role("button", name="Close new payment", exact=True).click()

                    first = create("instant", "UI instant coffee")
                    status("Settled")
                    page.get_by_role("button", name="Check payment & retry", exact=True).click()
                    expect(page.locator(".decision-box strong")).to_have_text("Already paid. You’re all set.")
                    require(server.get("/api/state")["stats"]["total"] == 1, "Retry duplicated payment")
                    snapshot("02-settled-phones")

                    page.get_by_role("button", name="View payment details", exact=True).click()
                    expect(page.locator("#detail-content")).to_contain_text(first)
                    expect(page.locator("#detail-content")).to_contain_text("ALREADY_PAID")
                    page.get_by_role("button", name="Copy ID", exact=True).click()
                    expect(page.locator("#toast")).to_contain_text("Transaction ID copied")
                    require(page.evaluate("navigator.clipboard.readText()") == first, "Copied ID is wrong")
                    page.get_by_role("button", name="Close payment details", exact=True).click()

                    original = create("delayed", "UI original pending 100")
                    status("Awaiting credit")
                    page.get_by_role("button", name="Check payment & retry", exact=True).click()
                    expect(page.locator(".decision-box strong")).to_have_text("Let’s resolve this one first.")
                    expect(page.locator(".phone:not(.merchant) .status")).to_have_text("Refund pending", timeout=18000)
                    page.get_by_role("button", name="Check payment & retry", exact=True).click()
                    page.get_by_role("button", name="Start a new payment", exact=True).click()
                    page.get_by_label("Note (optional)", exact=True).fill("UI replacement 100")
                    with page.expect_response(lambda r: urlsplit(r.url).path == "/api/payments" and r.request.method == "POST") as response:
                        page.get_by_role("button", name="Create payment", exact=True).click()
                    replacement = response.value.json()["txn_id"]
                    require(replacement != original, "UI retry reused the old ID")
                    expect(page.locator("#payment-dialog")).not_to_be_visible()
                    status("Settled")
                    navigate("Refunds")
                    page.get_by_role("button", name="Simulate refund", exact=True).click()
                    expect(page.get_by_text("Nothing left hanging.", exact=True)).to_be_visible()
                    require(server.payment(original)["customer"]["state"] == "REFUNDED", "Original UI refund failed")
                    server.settled(replacement)
                    snapshot("03-original-refunded")

                    navigate("Payment room")
                    interrupted = create("offline", "UI interrupted 250", "250.00")
                    expect(page.get_by_role("switch", name="Devices connected", exact=True)).not_to_be_checked()
                    expect(page.locator(".phone.merchant .status")).to_have_text("Not received yet")
                    require(not page.get_by_role("button", name="Confirm simulated receipt", exact=True).is_enabled(), "Disconnected credit button is enabled")
                    page.get_by_role("switch", name="Devices connected", exact=True).click()
                    page.get_by_role("button", name="Confirm simulated receipt", exact=True).click()
                    status("Settled")
                    server.settled(interrupted)
                    snapshot("04-reconnected-phones")

                    create("declined", "UI declined one paisa", "0.01")
                    status("Failed")
                    expect(page.locator(".decision-box strong")).to_have_text("A fresh payment is safe to start.")
                    navigate("Transactions")
                    page.get_by_label("Search payment ID or note", exact=True).fill("UI original pending 100")
                    expect(page.locator("tbody tr")).to_have_count(1)
                    expect(page.locator("tbody")).to_contain_text("Refunded")
                    page.get_by_label("Search payment ID or note", exact=True).fill("")
                    page.get_by_role("button", name="Settled", exact=True).click()
                    require(all("Settled" in row for row in page.locator("tbody tr").all_text_contents()), "Settled filter includes other states")
                    page.get_by_role("button", name="Refunds", exact=True).click()
                    expect(page.locator("tbody tr")).to_have_count(1)
                    page.get_by_role("button", name="All payments", exact=True).click()
                    snapshot("05-transactions")

                    with page.expect_download(timeout=15000) as download:
                        page.get_by_role("link", name="Export CSV", exact=True).click()
                    export = artifact / "transactions.csv"
                    download.value.save_as(str(export))
                    require(download.value.failure() is None, "Browser CSV download failed")
                    records = list(csv.DictReader(io.StringIO(export.read_text(encoding="utf-8-sig"))))
                    require(any(row["transaction_id"] == original for row in records), "Browser CSV omitted payment")

                    injected = '<img src=x onerror="window.__paysync_xss=1">'
                    server.create(note=injected)
                    page.reload()
                    page.get_by_label("Search payment ID or note", exact=True).fill("onerror")
                    expect(page.locator(".cell-note")).to_have_text(injected)
                    require(page.locator("tbody img").count() == 0, "Note rendered as active HTML")
                    require(page.evaluate("window.__paysync_xss === undefined"), "Note executed JavaScript")

                    page.get_by_role("button", name="Open workspace guide", exact=True).click()
                    page.get_by_role("button", name="System & guide", exact=True).click()
                    expect(page.get_by_role("heading", name="Workspace health", exact=True)).to_be_visible()
                    page.get_by_role("button", name="Run timeout check", exact=True).click()
                    expect(page.locator("#toast")).to_contain_text("Timeout check")

                    navigate("Overview")
                    for responsive_width in (320, 390, 600, 768, 1024, 1440):
                        page.set_viewport_size({"width": responsive_width, "height": height})
                        page.wait_for_function("document.documentElement.scrollWidth <= innerWidth")
                        snapshot(f"responsive-{responsive_width}")
                    page.set_viewport_size({"width": width, "height": height})

                    before = server.get("/api/state")
                    server.stop()
                    expect(page.locator("#connection-banner")).to_be_visible(timeout=15000)
                    snapshot("06-server-stopped")
                    server.start()
                    page.goto(server.base)
                    expect(page.get_by_role("heading", name="A good day to be in sync.", exact=True)).to_be_visible()
                    expect(page.locator("#connection-banner")).not_to_be_visible()
                    after = server.get("/api/state")
                    require(before["stats"] == after["stats"], "Browser restart lost saved totals")
                    require(not errors, "Browser JavaScript errors: " + repr(errors))
                    snapshot("07-restored-overview")

                def lost_response():
                    """A completed request with a lost response must not become another debit."""
                    retry_server = Server(project, artifact / "lost-response", timeout=20)
                    try:
                        retry_server.start()
                        page.goto(retry_server.base)
                        expect(page.get_by_role("button", name="New payment", exact=True)).to_be_visible()
                        before = retry_server.get("/api/state")["stats"]["total"]
                        sent = []
                        def intercept(route):
                            sent.append(route.request.post_data_json)
                            completed = route.fetch()  # Complete server operation first.
                            require(completed.status == 200, "Injected request was not accepted by server")
                            route.abort("failed")  # Then drop only the browser response.
                        page.route("**/api/payments", intercept)
                        page.get_by_role("button", name="New payment", exact=True).click()
                        page.get_by_label("Note (optional)", exact=True).fill("Lost response retry must be one payment")
                        try:
                            page.get_by_role("button", name="Create payment", exact=True).click()
                            expect(page.locator("#form-error")).to_be_visible()
                        finally:
                            page.unroute("**/api/payments", intercept)
                        require(len(sent) == 1, "Could not inject one lost-response request")
                        expected_count = str(before + 1).zfill(2)
                        # Normal background poll must acknowledge the saved request.
                        expect(page.locator(".stat-value").nth(1)).to_have_text(expected_count, timeout=10000)
                        with page.expect_response(lambda r: urlsplit(r.url).path == "/api/payments" and r.request.method == "POST") as response:
                            page.get_by_role("button", name="Create payment", exact=True).click()
                        require(response.value.status == 200, "Request retry failed")
                        count = retry_server.get("/api/state")["stats"]["total"]
                        require(count == before + 1, f"LOST-RESPONSE BUG: retry created a second payment (before={before}, after={count})")
                    finally:
                        try:
                            snapshot("08-lost-response-result")
                        finally:
                            retry_server.stop()

                def live_events():
                    """Server-sent events: no steady polling, external writes appear without reload."""
                    live = Server(project, artifact / "live-events", timeout=20)
                    counts = {"state": 0, "events": 0}
                    def count(request):
                        path = urlsplit(request.url).path
                        if path == "/api/state": counts["state"] += 1
                        if path == "/api/events": counts["events"] += 1
                    try:
                        live.start()
                        page.on("request", count)
                        page.goto(live.base)
                        expect(page.get_by_role("heading", name="A good day to be in sync.", exact=True)).to_be_visible()
                        page.wait_for_timeout(1500)
                        require(counts["events"] >= 1, "EventSource stream was never opened")
                        baseline = counts["state"]
                        page.wait_for_timeout(5000)
                        idle = counts["state"] - baseline
                        require(idle <= 1, f"Idle page still polls /api/state ({idle} requests in 5s)")
                        live.call("/api/payments", {"txn_id": unique_id(), "amount_minor": 10000, "scenario": "instant", "note": "pushed by another client"})
                        expect(page.locator(".stat-value").nth(1)).to_have_text("01", timeout=5000)
                        live.stop()
                        expect(page.locator("#connection-banner")).to_be_visible(timeout=15000)
                        live.start()
                        page.goto(live.base)
                        expect(page.locator("#connection-banner")).not_to_be_visible(timeout=10000)
                    finally:
                        try:
                            page.remove_listener("request", count)
                        finally:
                            live.stop()

                def agent_panel():
                    """The agent proposes; nothing executes until the Approve button is pressed."""
                    box = Server(project, artifact / "agent", timeout=60)
                    try:
                        box.start()
                        tid = unique_id()
                        box.call("/api/payments", {"txn_id": tid, "amount_minor": 10000, "scenario": "delayed", "note": "agent browser test"})
                        page.goto(box.base + "/#system")
                        expect(page.get_by_role("heading", name="Reliability agent", exact=True)).to_be_visible()
                        expect(page.get_by_text("awaiting merchant confirmation")).to_be_visible()
                        require(box.payment(tid)["customer"]["state"] == "DEBITED", "Agent acted before approval")
                        page.get_by_role("button", name="Approve: credit", exact=True).click()
                        expect(page.get_by_text("All payments look healthy")).to_be_visible(timeout=10000)
                        require(box.payment(tid)["customer"]["state"] == "SETTLED", "Approved action did not execute")
                        require((box.data / "agent-audit.jsonl").read_text().count("EXECUTED") == 1, "Approval was not audited")
                    finally:
                        box.stop()

                try:
                    result.case(f"Browser {width}px: complete payment/refund/offline/receipt/export/restart journey", journey)
                    result.case(f"Browser {width}px: lost-response retry must not double-pay", lost_response)
                    result.case(f"Browser {width}px: live SSE updates without steady polling", live_events)
                    result.case(f"Browser {width}px: reliability agent executes only after approval", agent_panel)
                finally:
                    if page and not page.is_closed():
                        try:
                            snapshot("final-or-failure")
                        except Exception:
                            pass
                    context.tracing.stop(path=str(artifact / "trace.zip"))
                    context.close()
                    server.stop()
        finally:
            browser.close()


def core_hashes(project):
    return {p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (project / "paysync").rglob("*.py")}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--report-dir", type=Path, default=None)
    parser.add_argument("--headed", action="store_true", help="Show the test Chromium browser")
    parser.add_argument("--api-only", action="store_true", help="Partial run; browser checks NOT counted as passing")
    args = parser.parse_args()
    project = args.project.resolve()
    for name in ("server.py", "run_tests.py", "audit_engine.py", "verify_core.py", "web/index.html"):
        if not (project / name).is_file():
            parser.error(f"Missing {name}. Run from the folder containing server.py, or pass --project.")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    folder = (args.report_dir or (project / "test-results")).resolve() / run_id
    folder.mkdir(parents=True, exist_ok=False)
    results = Results(folder)
    original_hashes = core_hashes(project)
    require(original_hashes, "No core source files found")
    print("Reports: " + str(folder), flush=True)
    print("Own isolated servers/data only. Existing localhost:8765 and data/ are not used.", flush=True)

    def integrity():
        data = child_tool(project, folder, "verify_core.py", json_output=True)
        require(data["unchanged"], "Core integrity check failed")
    results.case("Core integrity against reviewed manifest", integrity)
    results.case("All bundled engine and product tests", lambda: child_tool(project, folder, "run_tests.py"))

    server = Server(project, folder / "api", timeout=3)
    try:
        if results.case("Start a real server subprocess with fresh data", server.start):
            api_tests(server, results)
    finally:
        server.stop()

    if args.api_only:
        print("NOT RUN: browser tests (--api-only). This is an incomplete run.", flush=True)
    else:
        results.case("Desktop and mobile browser suite", lambda: browser_tests(project, folder, results, args.headed))

    def audit():
        data = child_tool(project, folder, "audit_engine.py", json_output=True, allow_fail=True)
        require(isinstance(data.get("findings"), list), "Audit returned no findings list")
        results.blockers = data["findings"]
        (folder / "core-audit.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"CORE REGRESSION PROBES: {len(results.blockers)} historical defects still reproduce.", flush=True)
    results.case("Core regression probes: no historical defect reproduces", lambda: (audit(), require(not results.blockers, f"Defects reproduce: {[b['id'] for b in results.blockers]}")))
    results.case("Test execution did not edit core", lambda: require(core_hashes(project) == original_hashes, "Core source bytes changed during test run"))
    results.save()

    failures = sum(row["status"] == "FAIL" for row in results.rows)
    print(f"\nRecorded checks: {len(results.rows)}; failed: {failures}; core blockers: {len(results.blockers)}", flush=True)
    print("Full details: " + str(folder / "report.json"), flush=True)
    if failures:
        return 1
    if results.blockers:
        return 2
    if not results.browser_executed:
        return 3
    print("Covered sandbox checks passed. No real-bank or production certification is implied.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted. Test results already written are retained.")
        raise SystemExit(130)
