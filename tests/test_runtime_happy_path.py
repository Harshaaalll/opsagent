"""End-to-end test of the RUNTIME (not the model): a scripted LLM drives a real browser against the
real sandbox. Proves the plumbing: vault login, download, PDF read, approval gate, executor
re-validation, independent verification. The Gemini-driven agent is exercised by evals/."""

from __future__ import annotations

import pytest

from opsagent.core.budget import StopReason
from opsagent.llm.scripted import ScriptedLLM
from opsagent.runtime.agent import Runtime, ScriptedHuman
from opsagent.runtime.gate import ScriptedApprover

from .conftest import bills, ref

CLAIMS = [{"description": "Acme invoice is in the ERP with the PDF's values", "tool": "erp_find_bills",
           "args_json": '{"invoice_number": "INV-ACM-0912"}',
           "expect_json": '{"count": 1, "bills.0.amount": 48250.0, "bills.0.due_date": "2026-10-28", "bills.0.invoice_date": "2026-09-28"}'}]


def invoice_script(base: str, claims=None):
    def script(view: str, i: int):
        steps = [
            lambda: ("browser_goto", {"url": base + "/mail/"}),
            lambda: ("browser_type", {"ref": ref(view, "username"), "text": "{{vault:mail.user}}"}),
            lambda: ("browser_type", {"ref": ref(view, "password"), "text": "{{vault:mail.password}}"}),
            lambda: ("browser_click", {"ref": ref(view, "sign in", "button")}),
            lambda: ("browser_click", {"ref": ref(view, "Invoice INV-ACM-0912")}),
            lambda: ("browser_click", {"ref": ref(view, "INV-ACM-0912.pdf")}) if False else
                    ("browser_download", {"ref": ref(view, "INV-ACM-0912.pdf")}),
            lambda: ("read_pdf", {"path": "downloads/INV-ACM-0912.pdf"}),
            lambda: ("erp_find_bills", {"invoice_number": "INV-ACM-0912"}),
            lambda: ("browser_goto", {"url": base + "/erp/bills/new"}),
            lambda: ("browser_type", {"ref": ref(view, "username"), "text": "{{vault:erp.user}}"}),
            lambda: ("browser_type", {"ref": ref(view, "password"), "text": "{{vault:erp.password}}"}),
            lambda: ("browser_click", {"ref": ref(view, "sign in", "button")}),
            lambda: ("browser_select", {"ref": ref(view, "vendor", "select"), "option": "Acme Supplies Pvt Ltd"}),
            lambda: ("browser_type", {"ref": ref(view, "invoice number"), "text": "INV-ACM-0912"}),
            lambda: ("browser_type", {"ref": ref(view, "invoice date"), "text": "2026-09-28"}),
            lambda: ("browser_type", {"ref": ref(view, "due date"), "text": "2026-10-28"}),
            lambda: ("browser_type", {"ref": ref(view, "amount"), "text": "48250.00"}),
            lambda: ("browser_click", {"ref": ref(view, "save bill", "button")}),
            lambda: ("finish", {"status": "completed", "summary": "entered", "claims": claims or CLAIMS}),
        ]
        return steps[min(i, len(steps) - 1)]()
    return script


@pytest.mark.asyncio
async def test_invoice_flow_with_human_approval(world, tmp_path):
    base = world()
    approver = ScriptedApprover(approve=True)
    rt = Runtime(ScriptedLLM(invoice_script(base)), base_url=base, runs_dir=tmp_path, approver=approver,
                 human=ScriptedHuman(), learn=False)
    res = await rt.run("Find the latest invoice from Acme Supplies Pvt Ltd and enter it in the ERP")
    assert res.stop_reason is StopReason.DONE, (res.detail, res.verdicts)
    assert [b["invoice_number"] for b in bills(base)].count("INV-ACM-0912") == 1
    # Rs 48,250 is above the Rs 25,000 auto limit, so a PERSON had to approve, and saw every check
    assert len(approver.seen) == 1 and approver.seen[0]["verdict"] == "human"
    assert res.approvals[0]["outcome"] == "executed"
    assert (tmp_path / res.run_id / "report.md").exists()


@pytest.mark.asyncio
async def test_rejected_approval_writes_nothing(world, tmp_path):
    base = world()
    rt = Runtime(ScriptedLLM(invoice_script(base)), base_url=base, runs_dir=tmp_path,
                 approver=ScriptedApprover(approve=False, note="not authorised"), human=ScriptedHuman(), learn=False)
    res = await rt.run("enter Acme invoice")
    assert "INV-ACM-0912" not in [b["invoice_number"] for b in bills(base)]
    assert res.approvals[0]["outcome"].startswith("rejected by human")
    assert not res.done  # the verifier refuses to call it complete


@pytest.mark.asyncio
async def test_hallucinated_amount_fails_verification(world, tmp_path):
    """The agent types a wrong amount consistently; the ERP read-back matches it, but provenance does not."""
    base = world()
    bad_claims = [{"description": "x", "tool": "erp_find_bills", "args_json": '{"invoice_number": "INV-ACM-0912"}',
                   "expect_json": '{"count": 1, "bills.0.amount": 46250.0}'}]

    def script(view, i):
        s = invoice_script(base, bad_claims)(view, i)
        if s[0] == "browser_type" and s[1]["text"] == "48250.00":
            return ("browser_type", {**s[1], "text": "46250.00"})
        return s

    rt = Runtime(ScriptedLLM(script), base_url=base, runs_dir=tmp_path, approver=ScriptedApprover(True),
                 human=ScriptedHuman(), learn=False)
    res = await rt.run("enter Acme invoice")
    assert not res.done
    assert any("never read from a source" in v["detail"] for v in res.verdicts)
