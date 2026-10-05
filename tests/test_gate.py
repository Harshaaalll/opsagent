"""The deterministic approval gate: policy decisions are code, not model judgement."""

from __future__ import annotations

from pathlib import Path

import pytest

from opsagent.runtime.gate import PolicyGate
from opsagent.tools.system import ErpClient

ROOT = Path(__file__).resolve().parents[1]


def req(vendor="Acme Supply Co", inv="INV-ASC-2201", amount="12000.00", cur="INR",
        idate="2026-10-01", due="2026-10-31"):
    return {"tool": "browser_click", "digest": "d",
            "preview": {"button": "Save bill", "fields": {
                "Vendor": vendor, "Invoice number": inv, "Invoice date": idate, "Due date": due,
                "Currency": cur, "Amount (incl. tax)": amount, "Notes": ""}}}


@pytest.fixture
def gate(world):
    base = world()
    return PolicyGate(ROOT / "company" / "policy.yaml", ErpClient(base))


async def test_small_trusted_bill_is_auto_approved(gate):
    d = await gate.evaluate(req())
    assert d.verdict == "auto", d.table()


async def test_amount_above_auto_limit_needs_a_human(gate):
    d = await gate.evaluate(req(vendor="Acme Supplies Pvt Ltd", inv="INV-ACM-0912", amount="48,250.00"))
    assert d.verdict == "human" and "within_auto_limit" in d.reason


async def test_amount_above_hard_ceiling_always_human(gate):
    d = await gate.evaluate(req(vendor="Globex Logistics", inv="INV-GLX-5521", amount="185000"))
    assert d.verdict == "human" and "below_hard_ceiling" in d.reason


async def test_untrusted_vendor_needs_a_human(gate):
    d = await gate.evaluate(req(vendor="Unknown Traders", inv="X-1"))
    assert d.verdict == "human"


async def test_duplicate_is_blocked_outright(world):
    base = world(preload_duplicate=True)
    gate = PolicyGate(ROOT / "company" / "policy.yaml", ErpClient(base))
    d = await gate.evaluate(req(vendor="Acme Supplies Pvt Ltd", inv="INV-ACM-0912", amount="48250"))
    assert d.verdict == "block" and "already holds" in d.reason


async def test_bad_dates_and_currency_escalate(gate):
    assert (await gate.evaluate(req(due="2026-09-01"))).verdict == "human"
    assert (await gate.evaluate(req(cur="USD"))).verdict == "human"


async def test_unknown_effect_is_not_auto_approved(gate):
    d = await gate.evaluate({"tool": "x", "digest": "d", "preview": {}})
    assert d.verdict == "human"


async def test_erp_outage_does_not_auto_approve_blind(world):
    gate = PolicyGate(ROOT / "company" / "policy.yaml", ErpClient("http://localhost:9"))
    d = await gate.evaluate(req())
    assert d.verdict == "human"
