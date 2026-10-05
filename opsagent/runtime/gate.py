"""The approval gate: the model proposes, deterministic code decides, a person approves.

When a call is high-risk the registry hands back the exact effect (a preview of
the form about to be submitted). The gate then:

  1. runs explicit CHECKS against that effect (the gate's logic IS the checklist,
     so the explanation shown to the human can never drift from the decision),
  2. returns one of  AUTO (policy may approve), HUMAN (ask a person), BLOCK (refuse),
  3. a person (or a scripted approver in evals) is shown every check and decides.

The checks read `company/policy.yaml`; changing the policy changes behaviour
without touching code. The duplicate check queries the ERP through its API: a
second channel, independent of what the model believed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional, Protocol

import yaml

from ..tools.system import ErpClient


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    blocking: bool = False      # a failed blocking check refuses the action outright


@dataclass
class GateDecision:
    verdict: str                # "auto" | "human" | "block"
    checks: list[Check] = field(default_factory=list)
    reason: str = ""

    def table(self) -> str:
        return "\n".join(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}" for c in self.checks)


class Approver(Protocol):
    async def decide(self, request: dict, decision: GateDecision) -> tuple[bool, str]:
        """Return (approved, note)."""


def _num(s: str) -> Optional[float]:
    m = re.search(r"-?\d[\d,]*\.?\d*", s or "")
    return float(m.group(0).replace(",", "")) if m else None


class PolicyGate:
    def __init__(self, policy_path: Path, erp: ErpClient):
        cfg = yaml.safe_load(policy_path.read_text(encoding="utf-8"))["approvals"]
        self.cfg = cfg
        self.erp = erp

    def _field(self, fields: dict, key: str) -> str:
        pats = self.cfg["field_patterns"].get(key, [])
        for label, value in fields.items():
            if any(p in label.lower() for p in pats):
                return (value or "").strip()
        return ""

    async def evaluate(self, request: dict) -> GateDecision:
        prev = request.get("preview") or {}
        fields = prev.get("fields") or {}
        if not fields:
            return GateDecision("human", [Check("effect_known", False, "no form values to evaluate")],
                                "effect could not be inspected; a person must decide")
        c = self.cfg
        vendor = self._field(fields, "vendor")
        inv = self._field(fields, "invoice_number")
        amount = _num(self._field(fields, "amount"))
        currency = self._field(fields, "currency")
        inv_date, due = self._field(fields, "invoice_date"), self._field(fields, "due_date")
        checks: list[Check] = []

        checks.append(Check("required_fields", all([vendor, inv, amount, inv_date, due]),
                            f"vendor={vendor!r} invoice={inv!r} amount={amount} dates={inv_date!r}/{due!r}"))
        # independent duplicate check through the ERP API
        dup = False
        try:
            found = await self.erp.find_bills(vendor=vendor, invoice_number=inv) if (vendor and inv) else {"bills": []}
            dup = any(b["vendor"] == vendor and b["invoice_number"].lower() == inv.lower() for b in found["bills"])
            checks.append(Check("not_duplicate", not dup,
                                "no existing bill with this vendor+invoice number" if not dup
                                else f"ERP already holds {inv} for {vendor}", blocking=dup))
        except Exception as exc:  # ERP lookup down: do not auto-approve blind
            checks.append(Check("not_duplicate", False, f"could not verify against ERP ({type(exc).__name__})"))
        checks.append(Check("trusted_vendor", vendor in c["trusted_vendors"], f"{vendor!r} on trusted list"))
        checks.append(Check("currency", currency == c["currency"], f"currency {currency!r} (policy: {c['currency']})"))
        checks.append(Check("within_auto_limit", amount is not None and amount <= c["auto_approve_max_amount"],
                            f"amount {amount} vs auto limit {c['auto_approve_max_amount']}"))
        try:
            ok_dates = date.fromisoformat(due) >= date.fromisoformat(inv_date)
        except ValueError:
            ok_dates = False
        checks.append(Check("dates_sane", ok_dates, f"due {due} not before invoice date {inv_date}"))
        big = amount is not None and amount > c["always_human_above"]
        checks.append(Check("below_hard_ceiling", not big, f"amount {amount} vs human-only ceiling {c['always_human_above']}"))

        blockers = [x for x in checks if x.blocking and not x.passed]
        if blockers:
            return GateDecision("block", checks, "; ".join(x.detail for x in blockers))
        if all(x.passed for x in checks):
            return GateDecision("auto", checks, "all policy checks passed")
        failed = ", ".join(x.name for x in checks if not x.passed)
        return GateDecision("human", checks, f"needs a person: {failed}")


class CliApprover:
    """Asks the person at the terminal. Shows the exact effect and every check."""

    async def decide(self, request: dict, decision: GateDecision) -> tuple[bool, str]:
        prev = request.get("preview", {})
        print("\n" + "=" * 70)
        print("APPROVAL REQUIRED:", prev.get("button"), "->", prev.get("effect") or prev.get("submits_to"))
        for k, v in (prev.get("fields") or {}).items():
            print(f"    {k}: {v}")
        print("  Policy checks:\n" + decision.table())
        print(f"  Gate says: {decision.reason}   (approval digest {request['digest']})")
        ans = input("Approve this action? [y/N] ").strip().lower()
        note = input("Note (optional): ").strip() if ans != "y" else ""
        return ans == "y", note


class ScriptedApprover:
    """For evals and tests: approves or rejects deterministically and records what it was shown."""

    def __init__(self, approve: bool = True, note: str = ""):
        self.approve, self.note = approve, note
        self.seen: list[dict] = []

    async def decide(self, request: dict, decision: GateDecision) -> tuple[bool, str]:
        self.seen.append({"request": request, "verdict": decision.verdict})
        return self.approve, self.note
