"""Payment reliability agent (sandbox).

Reads the payment log, explains stuck / refund-pending / conflicting payments,
proposes ONE safe action per payment from a fixed allowlist, and executes it
ONLY after an explicit human approval. Everything it does is appended to
`agent-audit.jsonl` in the data directory.

Guardrails (all enforced in code, covered by tests/test_agent.py):
  * allowlist: refund, credit, sync, reconnect. Anything else is rejected.
  * a proposal is bound to (txn, action, customer state, state clock); if the
    payment changed since it was proposed, approval is refused (stale).
  * conflicting terminal outcomes are NEVER auto-actioned (manual review).
  * no approval -> no execution. The planner (rules or an LLM) can only choose
    among proposals the rules already consider legal.
Planners: RulePlanner (deterministic, default). LLMPlanner (optional) lets a
model rank/explain the legal proposals; its output is validated, never trusted.
"""
from __future__ import annotations
import hashlib
import json
import time
from dataclasses import dataclass, asdict
from typing import Callable, Optional

ALLOWED = ("refund", "credit", "sync", "reconnect")


@dataclass
class Finding:
    txn_id: str
    severity: str            # ok | info | warn | high
    summary: str
    explanation: str
    action: Optional[str] = None       # one of ALLOWED, or None
    proposal_id: Optional[str] = None

    def to_dict(self):
        return asdict(self)


def _proposal_id(txn_id, action, p):
    c = p["customer"] or {}
    basis = json.dumps([txn_id, action, c.get("state"), c.get("state_clock"), p["merchant"] and p["merchant"].get("state")], sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def diagnose(p: dict, online: bool, timeout_s: float, now: float) -> Finding:
    tid, c, m = p["id"], p["customer"], p["merchant"]
    cs, ms = c["state"], (m or {}).get("state")
    money = f"₹{p['amount'] / 100:,.2f}"
    short = tid[:8]
    if c.get("conflict") or (m or {}).get("conflict"):
        return Finding(tid, "high", f"{short}: conflicting outcomes",
                       f"The two devices recorded different terminal outcomes for {money} (customer {cs}, merchant {ms}). "
                       "The local outcome was kept. This needs a human to decide; the agent will not act on it.")
    if cs == "SETTLED" and ms == "SETTLED":
        return Finding(tid, "ok", f"{short}: settled", f"{money} settled on both devices.")
    if cs == "REFUNDED" and ms == "REFUNDED":
        return Finding(tid, "ok", f"{short}: refunded", f"{money} refunded on both devices.")
    if cs == "REFUND_PENDING":
        return Finding(tid, "warn", f"{short}: refund pending",
                       f"The debit of {money} timed out without a confirmed credit and was reversed. "
                       "The refund proof is still missing, so the customer's money is in limbo.",
                       "refund")
    if cs == "FAILED":
        return Finding(tid, "info", f"{short}: failed before debit",
                       f"The provider declined the payment before any debit. Nothing is owed. A new payment is safe.")
    if cs in ("DEBITED", "CREDITED", "INITIATED"):
        if not online:
            return Finding(tid, "warn", f"{short}: devices disconnected",
                           f"{money} is {cs} on the customer device, but the merchant device cannot be reached. "
                           "Reconnect so the merchant can confirm before the timeout.", "reconnect")
        if cs == "CREDITED" or (cs == "DEBITED" and ms == "CREDITED"):
            return Finding(tid, "warn", f"{short}: credit confirmed, not settled",
                           f"Credit evidence exists for {money} but the payment has not settled on both sides. Syncing recovers settlement.", "sync")
        if cs == "DEBITED":
            left = max(0, int(p["created"] + timeout_s - now))
            if ms != "DEBITED":
                return Finding(tid, "warn", f"{short}: replicas out of sync",
                               f"Customer shows DEBITED but the merchant shows {ms or 'nothing'}. Syncing exchanges the latest state.", "sync")
            return Finding(tid, "info", f"{short}: awaiting merchant confirmation",
                           f"{money} is debited and waiting for the merchant to confirm receipt. "
                           f"{left}s remain before it reverses to a refund.", "credit")
    return Finding(tid, "info", f"{short}: {cs.lower()}", f"{money} is {cs}.")


class RulePlanner:
    """Deterministic: keeps each finding's own proposed action."""
    def plan(self, findings, snapshot):
        return findings


class LLMPlanner:
    """Optional. `complete(prompt:str)->str` is any text-completion callable (e.g. an Anthropic client wrapper).
    The model may only (a) reword the explanation and (b) DROP a proposal it considers unwise.
    It can never add an action or change the one the rules chose; invalid output falls back to the rules."""
    def __init__(self, complete: Callable[[str], str]):
        self.complete = complete

    def plan(self, findings, snapshot):
        brief = [{"txn_id": f.txn_id, "severity": f.severity, "summary": f.summary,
                  "explanation": f.explanation, "proposed_action": f.action} for f in findings if f.severity != "ok"]
        if not brief:
            return findings
        prompt = ("You review payment-reliability findings. Reply with JSON only: a list of "
                  '{"txn_id": str, "explanation": str, "keep_action": bool}. Never invent txn_ids.\n' + json.dumps(brief))
        try:
            data = json.loads(self.complete(prompt))
            by_id = {d["txn_id"]: d for d in data if isinstance(d, dict) and isinstance(d.get("txn_id"), str)}
        except Exception:
            return findings
        out = []
        for f in findings:
            d = by_id.get(f.txn_id)
            if d and isinstance(d.get("explanation"), str) and 0 < len(d["explanation"]) <= 600:
                f.explanation = d["explanation"]
            if d and d.get("keep_action") is False and f.severity != "high":
                f.action = None
            out.append(f)
        return out


class ReliabilityAgent:
    def __init__(self, service, planner=None, audit_path=None, clock=time.time):
        self.service, self.planner, self.clock = service, planner or RulePlanner(), clock
        self.audit_path = audit_path or (service.directory / "agent-audit.jsonl")

    def _audit(self, record):
        record["at"] = self.clock()
        with open(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def report(self, snapshot=None):
        snap = snapshot or self.service.snapshot()
        now = self.clock()
        findings = [diagnose(p, snap["online"], snap["timeout_s"], now) for p in snap["payments"]]
        findings = self.planner.plan(findings, snap)
        by_tid = {p["id"]: p for p in snap["payments"]}
        for f in findings:
            if f.action not in ALLOWED:
                f.action = None
            if f.action:
                f.proposal_id = _proposal_id(f.txn_id, f.action, by_tid[f.txn_id])
        order = {"high": 0, "warn": 1, "info": 2, "ok": 3}
        findings.sort(key=lambda f: order[f.severity])
        return {"findings": [f.to_dict() for f in findings],
                "needs_attention": sum(f.severity in ("high", "warn") for f in findings),
                "mode": "propose-then-approve"}

    def approve(self, proposal_id, approved_by="operator"):
        """Execute a proposal only if it is still exactly what the rules propose now."""
        if not isinstance(proposal_id, str) or not proposal_id:
            raise ValueError("proposal_id is required")
        with self.service.lock:
            current = next((f for f in self.report()["findings"] if f["proposal_id"] == proposal_id), None)
            if current is None:
                self._audit({"event": "REJECTED_STALE", "proposal_id": proposal_id, "by": approved_by})
                raise LookupError("This proposal is no longer valid: the payment changed. Review the new findings.")
            action, tid = current["action"], current["txn_id"]
            self._audit({"event": "APPROVED", "proposal_id": proposal_id, "txn_id": tid, "action": action, "by": approved_by})
            if action == "reconnect":
                self.service.set_network(True)
            else:
                self.service.action(tid, action)
            self._audit({"event": "EXECUTED", "proposal_id": proposal_id, "txn_id": tid, "action": action})
            return {"txn_id": tid, "action": action, "executed": True}
