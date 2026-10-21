import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from server import ApiError, PaymentService, make_server


def close_service(service):
    service.close()


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = PaymentService(self.tmp.name, timeout=20)

    def tearDown(self):
        close_service(self.service)
        self.tmp.cleanup()

    def create(self, tid="payment_A_100", amount=10000, scenario="instant", note="Coffee"):
        return self.service.create(dict(txn_id=tid, amount_minor=amount, scenario=scenario, note=note))

    def payment(self, tid="payment_A_100"):
        return next(p for p in self.service.snapshot()["payments"] if p["id"] == tid)

    def expire(self, tid="payment_A_100"):
        c = self.service.customer.export_state(tid)
        self.service.customer.reconcile(now=c.created_at + 21)
        if self.service.online:
            self.service.sync(tid)

    def test_instant_both_devices_settle(self):
        self.create()
        p = self.payment()
        self.assertEqual(p["customer"]["state"], "SETTLED")
        self.assertEqual(p["customer"], p["merchant"])
        self.assertEqual(p["retry"]["decision"], "ALREADY_PAID")
        self.assertEqual(self.service.snapshot()["stats"]["settled_minor"], 10000)

    def test_duplicate_request_has_one_debit(self):
        self.create()
        result = self.create()
        self.assertTrue(result["replayed"])
        self.assertEqual(sum(h["event"] == "DEBITED" for h in self.payment()["history"]), 1)

    def test_concurrent_requests_are_serialized(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.create(), range(24)))
        self.assertEqual(sum(not r["replayed"] for r in results), 1)
        self.assertEqual(self.service.snapshot()["stats"]["total"], 1)

    def test_same_id_different_amount_is_conflict(self):
        self.create()
        with self.assertRaises(ApiError) as error:
            self.create(amount=20000)
        self.assertEqual(error.exception.status,409)

    def test_validation_minor_units_and_bounds(self):
        for value in (0, -1, 1.5, True, "10000", 10000001, None):
            with self.subTest(value=value), self.assertRaises(ApiError):
                self.create(amount=value)
        self.create(amount=1)
        self.assertEqual(self.payment()["amount"],1)

    def test_bad_id_scenario_and_note(self):
        for body in ({"txn_id":"../bad"},{"scenario":"unknown"},{"scenario":[]},{"scenario":{}},{"note":"x"*121},{"note":"\t=evil"}):
            payload=dict(txn_id="payment_A_100",amount_minor=10000,scenario="instant",note="Coffee")
            payload.update(body)
            with self.assertRaises(ApiError):
                self.service.create(payload)

    def test_pending_then_new_100_then_original_refund(self):
        self.create(scenario="delayed")
        self.assertEqual(self.service.action("payment_A_100","retry")["decision"],"RESOLVING")
        self.expire()
        self.assertEqual(self.service.action("payment_A_100","retry")["decision"],"SAFE_TO_RETRY_NEW_PAYMENT")
        self.create(tid="payment_B_100")
        self.service.action("payment_A_100","refund")
        a,b=self.payment(),self.payment("payment_B_100")
        self.assertEqual(a["customer"]["state"],"REFUNDED")
        self.assertEqual(a["customer"],a["merchant"])
        self.assertEqual(b["customer"]["state"],"SETTLED")
        self.assertEqual(self.service.snapshot()["stats"]["settled_minor"],10000)
        self.assertEqual(self.service.snapshot()["stats"]["refunded_minor"],10000)

    def test_confirm_before_timeout(self):
        self.create(scenario="delayed")
        self.service.action("payment_A_100","credit")
        self.assertEqual(self.payment()["customer"]["state"],"SETTLED")

    def test_late_credit_is_blocked_after_reversal(self):
        self.create(scenario="delayed")
        self.expire()
        with self.assertRaises(ApiError):
            self.service.action("payment_A_100","credit")
        self.assertEqual(self.payment()["customer"]["state"],"REFUND_PENDING")

    def test_offline_reconnect_converges_then_credit(self):
        self.service.set_network(False)
        self.create(scenario="offline")
        self.assertIsNone(self.payment()["merchant"])
        with self.assertRaises(ApiError):
            self.service.action("payment_A_100","credit")
        self.service.set_network(True)
        self.assertEqual(self.payment()["customer"],self.payment()["merchant"])
        self.service.action("payment_A_100","credit")
        self.assertEqual(self.payment()["merchant"]["state"],"SETTLED")

    def test_interrupted_journey_starts_disconnected(self):
        self.create(scenario="offline")
        self.assertFalse(self.service.online)
        self.service.reconcile()
        self.assertIsNone(self.payment()["merchant"])

    def test_offline_refund_reaches_merchant_after_reconnect(self):
        self.service.set_network(False)
        self.create(scenario="offline")
        self.expire()
        self.service.action("payment_A_100","refund")
        self.assertIsNone(self.payment()["merchant"])
        self.service.set_network(True)
        self.assertEqual(self.payment()["customer"],self.payment()["merchant"])
        self.assertEqual(self.payment()["merchant"]["state"],"REFUNDED")

    def test_decline_has_no_debit_and_safe_retry(self):
        self.create(scenario="declined")
        p=self.payment()
        self.assertEqual(p["retry"]["decision"],"SAFE_TO_RETRY_NEW_PAYMENT")
        self.assertFalse(any(h["event"] == "DEBITED" for h in p["history"]))

    def test_sweep_reports_only_changed_outcomes(self):
        self.create(scenario="declined")
        self.service.customer.pending_timeout_s=0
        self.assertEqual(self.service.reconcile(),[])

    def test_duplicate_refund_is_noop(self):
        self.create(scenario="delayed")
        self.expire()
        self.service.action("payment_A_100","refund")
        self.service.action("payment_A_100","refund")
        self.assertEqual(sum(h["event"] == "REFUNDED" for h in self.payment()["history"]),1)

    def test_illegal_refund_is_rejected(self):
        self.create()
        with self.assertRaises(ApiError):
            self.service.action("payment_A_100","refund")

    def test_request_identity_and_state_survive_restart(self):
        self.create()
        before=self.payment()
        self.service.set_network(False)
        close_service(self.service)
        self.service=PaymentService(self.tmp.name,timeout=20)
        self.assertTrue(self.create()["replayed"])
        after=self.payment()
        self.assertEqual(before["customer"],after["customer"])
        self.assertEqual(len(before["history"]),len(after["history"]))
        self.assertFalse(self.service.online)

    def test_restart_recovers_credit_before_settlement(self):
        self.create(scenario="delayed")
        self.service.merchant.mark_credited("payment_A_100","SIM-credit-before-stop")
        close_service(self.service)
        self.service=PaymentService(self.tmp.name,timeout=20)
        actions=self.service.reconcile()
        self.assertEqual(actions,[("payment_A_100","SETTLEMENT_RECOVERED","SETTLED")])
        self.assertEqual(self.payment()["customer"]["state"],"SETTLED")
        self.assertEqual(self.payment()["customer"],self.payment()["merchant"])

    def test_second_process_owner_is_rejected(self):
        with self.assertRaises(RuntimeError):
            PaymentService(self.tmp.name)

    def test_unknown_action_payment_and_network_validation(self):
        with self.assertRaises(ApiError): self.service.action("unknown_payment","retry")
        with self.assertRaises(ApiError): self.service.set_network("false")

    def test_csv_contains_integer_money_and_neutralizes_formula(self):
        self.create(note="=SUM(1,2)")
        rows=list(csv.reader(io.StringIO(self.service.export_csv().decode("utf-8-sig"))))
        self.assertEqual(rows[1][1],"10000")
        self.assertEqual(rows[1][5],"'=SUM(1,2)")


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.server=make_server(cls.tmp.name,port=0)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base=f"http://127.0.0.1:{cls.server.server_port}"
        with urlopen(cls.base+"/api/session") as response:
            cls.token=json.load(response)["csrf_token"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        close_service(cls.server.service)
        cls.tmp.cleanup()

    def request(self,path,data=None,headers=None):
        hdr={"Content-Type":"application/json","X-CSRF-Token":self.token}
        hdr.update(headers or {})
        request=Request(self.base+path,data=data,headers=hdr,method="POST" if data is not None else "GET")
        try:
            with urlopen(request) as response:return response.status,response.read(),response.headers
        except HTTPError as error:return error.code,error.read(),error.headers

    def test_assets_and_content_security_headers(self):
        for path in ("/","/app.js","/styles.css","/favicon.svg"):
            status,body,headers=self.request(path)
            self.assertEqual(status,200)
            self.assertTrue(body)
            self.assertIn("frame-ancestors 'none'",headers["Content-Security-Policy"])

    def test_block_cross_origin_and_missing_csrf(self):
        self.assertEqual(self.request("/api/network",b'{"online":false}',{"Origin":"https://attacker.example"})[0],403)
        self.assertEqual(self.request("/api/network",b'{"online":false}',{"X-CSRF-Token":""})[0],403)

    def test_host_header_and_private_paths(self):
        self.assertEqual(self.request("/api/state",headers={"Host":"attacker.example"})[0],403)
        self.assertEqual(self.request("/data/customer.jsonl")[0],404)
        self.assertEqual(self.request("/../server.py")[0],404)

    def test_malformed_json_and_content_type(self):
        self.assertEqual(self.request("/api/payments",b'{bad')[0],400)
        self.assertEqual(self.request("/api/payments",b'[]')[0],400)
        self.assertEqual(self.request("/api/payments",b'{}',{"Content-Type":"text/plain"})[0],415)

    def test_create_retry_and_export_over_http(self):
        body=json.dumps(dict(txn_id="http_payment_100",amount_minor=10000,scenario="instant",note="HTTP test")).encode()
        self.assertEqual(self.request("/api/payments",body)[0],200)
        status,result,_=self.request("/api/payments/http_payment_100/retry",b'{}')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(result)["decision"],"ALREADY_PAID")
        self.assertIn(b"http_payment_100",self.request("/api/export")[1])



class EventStreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = make_server(cls.tmp.name, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        with urlopen(cls.base + "/api/session") as response:
            cls.token = json.load(response)["csrf_token"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        close_service(cls.server.service)
        cls.tmp.cleanup()

    def post(self, path, body):
        request = Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                          headers={"Content-Type": "application/json", "X-CSRF-Token": self.token})
        with urlopen(request) as response:
            return response.status

    def read_event(self, stream):
        name = data = None
        while True:
            line = stream.readline().decode().rstrip("\n")
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
            elif line == "" and name:
                return name, data

    def test_stream_headers_and_push_on_change_only(self):
        stream = urlopen(self.base + "/api/events", timeout=10)
        self.assertIn("text/event-stream", stream.headers["Content-Type"])
        name, first = self.read_event(stream)
        self.assertEqual(name, "state")
        self.post("/api/payments", dict(txn_id="sse_payment_001", amount_minor=500, scenario="delayed", note="sse"))
        name, second = self.read_event(stream)
        self.assertEqual(name, "state")
        self.assertNotEqual(first, second)
        stream.close()

    def test_two_listeners_both_notified(self):
        a, b = urlopen(self.base + "/api/events", timeout=10), urlopen(self.base + "/api/events", timeout=10)
        _, a0 = self.read_event(a); _, b0 = self.read_event(b)
        self.post("/api/network", {"online": True})
        _, a1 = self.read_event(a); _, b1 = self.read_event(b)
        self.assertNotEqual(a0, a1); self.assertNotEqual(b0, b1)
        a.close(); b.close()

    def test_invalid_host_cannot_open_stream(self):
        request = Request(self.base + "/api/events", headers={"Host": "attacker.example"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)

    def test_app_js_uses_events_not_steady_polling(self):
        source = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("new EventSource('/api/events')", source)
        self.assertNotIn("},1500)", source)
