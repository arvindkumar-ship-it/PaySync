import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent import ALLOWED, LLMPlanner, ReliabilityAgent, RulePlanner, _proposal_id
from paysync.crdt import PaymentCRDT
from paysync.fsm import State
from server import PaymentService, make_server


def audit_events(service):
    path = service.directory / "agent-audit.jsonl"
    return [json.loads(line)["event"] for line in path.read_text().splitlines()] if path.exists() else []


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = PaymentService(self.tmp.name, timeout=20)
        self.agent = self.service.agent

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def make(self, tid, scenario):
        self.service.create(dict(txn_id=tid, amount_minor=10000, scenario=scenario, note="agent"))

    def expire(self, tid):
        c = self.service.customer.export_state(tid)
        self.service.customer.reconcile(now=c.created_at + 21)
        if self.service.online:
            self.service.sync(tid)

    def finding(self, tid):
        return next(f for f in self.agent.report()["findings"] if f["txn_id"] == tid)

    def test_healthy_payment_has_no_action(self):
        self.make("agent_ok_0001", "instant")
        f = self.finding("agent_ok_0001")
        self.assertEqual((f["severity"], f["action"], f["proposal_id"]), ("ok", None, None))

    def test_refund_pending_is_explained_and_proposed_but_not_executed(self):
        self.make("agent_refund_01", "delayed"); self.expire("agent_refund_01")
        f = self.finding("agent_refund_01")
        self.assertEqual((f["severity"], f["action"]), ("warn", "refund"))
        self.assertIn("refund", f["explanation"].lower())
        self.assertEqual(self.service.customer.export_state("agent_refund_01").state, State.REFUND_PENDING)  # still untouched
        self.assertEqual(audit_events(self.service), [])

    def test_approval_executes_and_is_audited(self):
        self.make("agent_refund_02", "delayed"); self.expire("agent_refund_02")
        pid = self.finding("agent_refund_02")["proposal_id"]
        out = self.agent.approve(pid, "tester")
        self.assertTrue(out["executed"])
        self.assertEqual(self.service.customer.export_state("agent_refund_02").state, State.REFUNDED)
        self.assertEqual(audit_events(self.service), ["APPROVED", "EXECUTED"])

    def test_proposal_cannot_be_replayed_or_forged(self):
        self.make("agent_refund_03", "delayed"); self.expire("agent_refund_03")
        pid = self.finding("agent_refund_03")["proposal_id"]
        self.agent.approve(pid)
        for bad in (pid, "0" * 16, "refund"):
            with self.assertRaises(LookupError):
                self.agent.approve(bad)
        with self.assertRaises(ValueError):
            self.agent.approve("")
        self.assertEqual(audit_events(self.service).count("EXECUTED"), 1)

    def test_proposal_goes_stale_when_payment_changes(self):
        self.make("agent_stale_001", "delayed")
        f = self.finding("agent_stale_001")
        self.assertEqual(f["action"], "credit")
        self.service.action("agent_stale_001", "credit")   # someone else settled it first
        with self.assertRaises(LookupError):
            self.agent.approve(f["proposal_id"])

    def test_conflicting_terminals_are_never_actioned(self):
        self.make("agent_conflict_1", "delayed"); self.expire("agent_conflict_1")
        self.service.customer.mark_refunded("agent_conflict_1", "SIM-R")
        self.service.merchant.apply_remote(self.service.customer.export_state("agent_conflict_1"))
        settled = PaymentCRDT("agent_conflict_1", amount_minor=10000).local_update("merchant", State.SETTLED)
        self.service.customer.apply_remote(settled)
        f = self.finding("agent_conflict_1")
        self.assertEqual((f["severity"], f["action"], f["proposal_id"]), ("high", None, None))
        self.assertEqual(self.service.customer.export_state("agent_conflict_1").state, State.REFUNDED)

    def test_disconnected_devices_propose_reconnect(self):
        self.service.set_network(False)
        self.make("agent_offline_01", "offline")
        f = self.finding("agent_offline_01")
        self.assertEqual(f["action"], "reconnect")
        self.agent.approve(f["proposal_id"])
        self.assertTrue(self.service.online)

    def test_awaiting_confirmation_proposes_credit_and_settles(self):
        self.make("agent_wait_0001", "delayed")
        f = self.finding("agent_wait_0001")
        self.assertIn("remain", f["explanation"])
        self.agent.approve(f["proposal_id"])
        self.assertEqual(self.service.customer.export_state("agent_wait_0001").state, State.SETTLED)

    def test_proposal_id_is_bound_to_payment_state(self):
        base = {"customer": {"state": "DEBITED", "state_clock": {"customer": 1}}, "merchant": {"state": "DEBITED"}}
        moved = {"customer": {"state": "DEBITED", "state_clock": {"customer": 2}}, "merchant": {"state": "DEBITED"}}
        other = {"customer": {"state": "DEBITED", "state_clock": {"customer": 1}}, "merchant": {"state": "CREDITED"}}
        ids = {_proposal_id("t", "sync", x) for x in (base, moved, other)}
        self.assertEqual(len(ids), 3)
        self.assertNotEqual(_proposal_id("t", "sync", base), _proposal_id("t", "credit", base))
        self.assertNotEqual(_proposal_id("t", "sync", base), _proposal_id("u", "sync", base))

    def test_actions_outside_allowlist_are_stripped(self):
        class Rogue(RulePlanner):
            def plan(self, findings, snapshot):
                for f in findings:
                    f.action = "drop_database"
                return findings
        self.make("agent_rogue_001", "delayed")
        rogue = ReliabilityAgent(self.service, planner=Rogue())
        f = next(x for x in rogue.report()["findings"] if x["txn_id"] == "agent_rogue_001")
        self.assertIsNone(f["action"]); self.assertIsNone(f["proposal_id"])
        self.assertEqual(ALLOWED, ("refund", "credit", "sync", "reconnect"))

    def test_llm_planner_can_reword_and_drop_but_not_add_or_invent(self):
        self.make("agent_llm_00001", "delayed"); self.expire("agent_llm_00001")
        reply = json.dumps([{"txn_id": "agent_llm_00001", "explanation": "Refund proof is missing.", "keep_action": False},
                            {"txn_id": "invented_txn", "explanation": "x", "keep_action": True}])
        agent = ReliabilityAgent(self.service, planner=LLMPlanner(lambda prompt: reply))
        f = next(x for x in agent.report()["findings"] if x["txn_id"] == "agent_llm_00001")
        self.assertEqual(f["explanation"], "Refund proof is missing.")
        self.assertIsNone(f["action"])
        self.assertEqual(len(agent.report()["findings"]), 1)

    def test_llm_planner_falls_back_on_garbage_and_cannot_touch_high_severity(self):
        self.make("agent_llm_00002", "delayed"); self.expire("agent_llm_00002")
        agent = ReliabilityAgent(self.service, planner=LLMPlanner(lambda prompt: "not json"))
        self.assertEqual(next(x for x in agent.report()["findings"])["action"], "refund")
        raising = ReliabilityAgent(self.service, planner=LLMPlanner(lambda prompt: (_ for _ in ()).throw(RuntimeError("down"))))
        self.assertEqual(next(x for x in raising.report()["findings"])["action"], "refund")


class AgentHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(self.tmp.name, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        with urlopen(self.base + "/api/session") as r:
            self.token = json.load(r)["csrf_token"]

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.server.service.close(); self.tmp.cleanup()

    def call(self, path, body=None, token=None):
        headers = {"Content-Type": "application/json", "X-CSRF-Token": self.token if token is None else token}
        req = Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers, method="GET" if body is None else "POST")
        try:
            with urlopen(req) as r:
                return r.status, json.load(r)
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def test_report_approve_and_csrf_over_http(self):
        self.call("/api/payments", dict(txn_id="http_agent_0001", amount_minor=10000, scenario="delayed", note="x"))
        status, report = self.call("/api/agent")
        self.assertEqual(status, 200)
        proposal = report["findings"][0]["proposal_id"]
        self.assertEqual(self.call("/api/agent/approve", {"proposal_id": proposal}, token="")[0], 403)
        self.assertEqual(self.call("/api/agent/approve", {"proposal_id": "nope"})[0], 409)
        self.assertEqual(self.call("/api/agent/approve", {})[0], 400)
        status, out = self.call("/api/agent/approve", {"proposal_id": proposal})
        self.assertEqual((status, out["executed"]), (200, True))
        self.assertEqual(self.call("/api/agent/approve", {"proposal_id": proposal})[0], 409)


if __name__ == "__main__":
    unittest.main()
