"""Fast unit tests: registry rules, breaker, knowledge retrieval, vault, verifier helpers, context."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from opsagent.core import vault
from opsagent.core.breaker import CircuitBreaker, BreakerPolicy
from opsagent.core.budget import Budget, Meter, StopReason
from opsagent.core.contracts import ErrorCode, Risk, ToolError, ToolFailure, ToolSpec
from opsagent.core.knowledge import Knowledge
from opsagent.core.redact import redact_text
from opsagent.core.registry import ToolRegistry
from opsagent.runtime.context import Context
from opsagent.runtime.verifier import appears

ROOT = Path(__file__).resolve().parents[1]


class In(BaseModel):
    x: int = 0


def make(handler, **kw) -> ToolRegistry:
    r = ToolRegistry()
    r.register(ToolSpec("t", "test tool", In, handler, **kw))
    return r


async def ok(inp):
    return {"x": inp.x}


# ---------------------------------------------------------------- registry
async def test_unknown_and_ungranted_tools_are_refused():
    r = make(ok)
    assert (await r.call("nope", {})).error.code is ErrorCode.NOT_FOUND
    assert (await r.call("t", {}, allowed=[])).error.code is ErrorCode.NOT_PERMITTED


async def test_input_contract_is_enforced_before_handler():
    called = []

    async def h(inp):
        called.append(1)

    r = make(h)
    res = await r.call("t", {"x": "not-an-int"})
    assert res.error.code is ErrorCode.INVALID_INPUT and not called


async def test_high_risk_needs_approval_and_digest_binds_args():
    async def preview(inp):
        return {"x": inp.x}

    ran = []

    async def h(inp):
        ran.append(inp.x)
        return {}

    r = make(h, risk=Risk.WRITE_HIGH, preview=preview)
    first = await r.call("t", {"x": 1})
    assert first.error.code is ErrorCode.NEEDS_APPROVAL and not ran
    approval = r.approval_for(first.error.data, by="p", human=True)
    # same approval replayed onto different arguments is void
    replay = await r.call("t", {"x": 2}, approval=approval)
    assert replay.error.code is ErrorCode.STALE_APPROVAL and not ran
    assert (await r.call("t", {"x": 1}, approval=approval)).ok and ran == [1]


async def test_retry_only_when_repeating_cannot_repeat_the_effect():
    calls = {"n": 0}

    async def flaky(inp):
        calls["n"] += 1
        raise ToolFailure(ErrorCode.UPSTREAM, "boom", retryable=True)

    # a non-idempotent write must not be retried
    r = make(flaky, risk=Risk.WRITE_LOW, max_retries=3)
    await r.call("t", {})
    assert calls["n"] == 1
    # a read may be
    calls["n"] = 0
    r2 = make(flaky, risk=Risk.READ, max_retries=2)
    await r2.call("t", {})
    assert calls["n"] == 3


async def test_fault_injection_and_audit_redaction(tmp_path):
    r = ToolRegistry(audit_path=tmp_path / "a.jsonl")
    r.register(ToolSpec("t", "d", In, ok))
    r.inject_fault("t", ToolError(code=ErrorCode.UPSTREAM, message="down"))
    assert not (await r.call("t", {"x": 1})).ok
    assert (await r.call("t", {"x": 1})).ok
    assert len((tmp_path / "a.jsonl").read_text().splitlines()) == 2


def test_breaker_opens_on_upstream_failures_not_on_bad_requests():
    b = CircuitBreaker(BreakerPolicy(threshold=3, window_s=60, cooldown_s=30))
    for _ in range(5):
        b.record_outcome("t", ErrorCode.CONFLICT)
    assert b.allows("t")
    for _ in range(3):
        b.record_outcome("t", ErrorCode.UPSTREAM)
    assert not b.allows("t")


# ---------------------------------------------------------------- budget
def test_stall_and_budget_stop_reasons():
    m = Meter(Budget(max_steps=3, stall_repeats=3))
    for _ in range(3):
        m.record_action("browser_click", {"ref": 1})
    assert m.exhausted() is StopReason.STALLED
    m2 = Meter(Budget(max_steps=2))
    m2.steps = 2
    assert m2.exhausted() is StopReason.MAX_STEPS


# ---------------------------------------------------------------- knowledge
def test_aliases_bridge_vocabulary():
    k = Knowledge.load(ROOT / "company" / "policies")
    top = k.search("how do I book a supplier bill", k=3)
    assert any(c.id == "AP-01" for c, _ in top)
    assert k.search("who can sign off a big payment", k=2)[0][0].id == "AP-02"


# ---------------------------------------------------------------- vault / redaction
def test_vault_resolves_known_and_rejects_unknown(monkeypatch):
    monkeypatch.setenv("VAULT_ERP_PASSWORD", "s3cret")
    assert vault.resolve("{{vault:erp.password}}") == "s3cret"
    with pytest.raises(vault.VaultError):
        vault.resolve("{{vault:prod.root}}")


def test_redaction_masks_secrets_and_account_numbers():
    assert "hunter2" not in redact_text("password=hunter2")
    assert "123456789012" not in redact_text("acct 123456789012")


# ---------------------------------------------------------------- verifier helpers
def test_value_provenance_handles_formats():
    pdf = "Due Date: 28 Oct 2026  Total Payable: INR 48,250.00"
    assert appears(48250.0, pdf) and appears("2026-10-28", pdf)
    assert not appears(46250.0, pdf) and not appears("2026-10-29", pdf)


# ---------------------------------------------------------------- context
def test_context_keeps_facts_after_compaction_and_only_latest_snapshot_in_full():
    c = Context("goal", budget_tokens=300)
    c.observe(1, "read_pdf", {"path": "x.pdf"}, "t", {"path": "x.pdf", "text": "Total Payable INR 48,250.00"}, None)
    for i in range(2, 12):
        c.observe(i, "browser_click", {"ref": i}, "t", {"snapshot": f"PAGE {i}\n" + "filler " * 80, "url": "http://x/", "screenshot": ""}, None)
    view = c.render()
    assert "48,250.00" in view                    # pinned fact survived compaction
    one_snapshot = ("filler " * 80).count("filler filler")
    assert view.count("filler filler") == one_snapshot   # exactly ONE snapshot (the newest) is verbatim
    assert "compacted" in view
    assert "48,250.00" in c.source_text()
