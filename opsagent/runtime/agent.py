"""The agent loop: Goal -> Plan -> Act -> Observe -> Adapt -> Verify -> Report.

The model decides WHAT to do next; this module owns everything around it:

  * which actions exist (the registry's granted tools + 4 control actions),
  * execution, through the single registry door (contracts, risk, approvals, retries, audit),
  * the approval gate for high-risk effects,
  * termination: budgets/stall checks run BEFORE each pass; "done" is only
    granted after the verifier independently confirms the outcome,
  * the record: every step (with the model's stated reason), approval, verdict
    and screenshot is written to runs/<run_id>/.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from ..core.budget import Budget, Meter, StopReason, CLEAN_STOPS
from ..core.contracts import ErrorCode, ToolResult
from ..core.knowledge import Knowledge, append_lesson
from ..core.redact import redact
from ..core.registry import ToolRegistry
from ..llm.base import LLM, LLMError
from ..tools.browser import BrowserSession, build_browser_tools
from ..tools.system import ErpClient, build_system_tools
from .context import Context
from .gate import Approver, CliApprover, PolicyGate
from .prompt import CONTROL_NAMES, CONTROL_TOOLS, SYSTEM
from .verifier import Verifier

ROOT = Path(__file__).resolve().parents[2]
ALWAYS_RETRIEVE = ("SEC-01", "SYS-02")


class Human(Protocol):
    async def answer(self, question: str) -> Optional[str]: ...


class CliHuman:
    async def answer(self, question: str) -> Optional[str]:
        print("\n" + "=" * 70 + f"\nAGENT ASKS: {question}")
        ans = input("Your answer (blank = cannot answer): ").strip()
        return ans or None


class ScriptedHuman:
    def __init__(self, answers: Optional[list[str]] = None):
        self.answers = list(answers or [])
        self.asked: list[str] = []

    async def answer(self, question: str) -> Optional[str]:
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else None


@dataclass
class RunResult:
    run_id: str
    goal: str
    stop_reason: StopReason
    final_status: str = ""
    summary: str = ""
    verdicts: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    approvals: list[dict] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    meter: dict = field(default_factory=dict)
    run_dir: Optional[Path] = None
    detail: str = ""

    @property
    def clean(self) -> bool:
        return self.stop_reason in CLEAN_STOPS

    @property
    def done(self) -> bool:
        return self.stop_reason is StopReason.DONE


class Runtime:
    """One configured agent environment (browser, tools, gate, verifier)."""

    def __init__(self, llm: LLM, *, base_url: str = "http://localhost:8765", runs_dir: Optional[Path] = None,
                 approver: Optional[Approver] = None, human: Optional[Human] = None,
                 budget: Optional[Budget] = None, headless: bool = True, slow_mo: int = 0,
                 company_dir: Optional[Path] = None, learn: bool = True):
        self.llm = llm
        self.base_url = base_url
        self.runs_dir = runs_dir or ROOT / "runs"
        self.company = company_dir or ROOT / "company"
        self.approver = approver or CliApprover()
        self.human = human or CliHuman()
        self.budget = budget or Budget()
        self.headless, self.slow_mo = headless, slow_mo
        self.learn = learn
        self.lessons_path = self.company / "lessons.jsonl"

    async def run(self, goal: str) -> RunResult:
        run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        knowledge = Knowledge.load(self.company / "policies", self.lessons_path)
        erp = ErpClient(self.base_url)
        registry = ToolRegistry(audit_path=run_dir / "audit.jsonl")
        session = BrowserSession(run_dir, headless=self.headless, slow_mo=self.slow_mo)
        await session.start()
        try:
            for spec in build_browser_tools(session) + build_system_tools(run_dir, erp, knowledge):
                registry.register(spec)
            return await _Run(self, run_id, run_dir, goal, registry, knowledge, erp).execute()
        finally:
            await session.close()


class _Run:
    def __init__(self, rt: Runtime, run_id: str, run_dir: Path, goal: str, registry: ToolRegistry,
                 knowledge: Knowledge, erp: ErpClient):
        self.rt, self.run_id, self.run_dir, self.goal = rt, run_id, run_dir, goal
        self.registry, self.knowledge = registry, knowledge
        self.gate = PolicyGate(rt.company / "policy.yaml", erp)
        self.verifier = Verifier(registry)
        self.ctx = Context(goal, rt.budget.context_tokens)
        self.meter = Meter(rt.budget, model=getattr(rt.llm, "model", "gemini-2.5-flash"))
        self.allowed = [s.name for s in registry.specs()]
        self.tool_decls = [s.as_function_schema() for s in registry.specs()] + CONTROL_TOOLS
        self.result = RunResult(run_id, goal, StopReason.ERROR, run_dir=run_dir)
        self.rejected: set[str] = set()
        self._steps_f = (run_dir / "steps.jsonl").open("a", encoding="utf-8")

    # -------------------------------------------------------------------- main
    async def execute(self) -> RunResult:
        hits = {c.id: c for c, _ in self.knowledge.search(self.goal, k=4)}
        for cid in ALWAYS_RETRIEVE:
            if (c := self.knowledge.get(cid)):
                hits.setdefault(cid, c)
        self.ctx.retrieved = "\n".join(f"  [{c.id}] {c.heading}: {c.text}" for c in hits.values())
        self._log({"event": "start", "goal": self.goal, "retrieved": list(hits)})
        try:
            await self._loop()
        except LLMError as exc:
            self._stop(StopReason.ERROR, detail=str(exc))
        except Exception as exc:  # the harness records a crash; it does not hide one
            self._stop(StopReason.ERROR, detail=f"{type(exc).__name__}: {exc}"[:300])
        self.result.meter = self.meter.snapshot()
        self._write_report()
        self._steps_f.close()
        return self.result

    def _stop(self, reason: StopReason, summary: str = "", status: str = "", detail: str = "") -> None:
        self.result.stop_reason = reason
        self.result.summary = summary or self.result.summary
        self.result.final_status = status or self.result.final_status
        self.result.detail = detail
        self._log({"event": "stop", "reason": reason.value, "detail": detail, "status": status})

    async def _loop(self) -> None:
        while True:
            stop = self.meter.exhausted()
            if stop:
                self._stop(stop, detail=f"stopped before step {self.meter.steps + 1}")
                return
            view = self.ctx.render()
            d = await self.rt.llm.decide(SYSTEM, view, self.tool_decls)
            self.meter.steps += 1
            self.meter.charge(d.prompt_tokens, d.output_tokens)
            self.meter.record_action(d.name, d.args)
            n = self.meter.steps
            self._log({"event": "decision", "step": n, "action": d.name, "args": redact(d.args),
                       "thought": d.thought, "model": d.model, "degraded": d.degraded})

            if d.name == "__invalid__":
                self.ctx.note("kernel", "You must respond by calling exactly one function. Do so now.")
                continue
            if d.name in CONTROL_NAMES:
                if await self._control(n, d):
                    return
                continue
            await self._act(n, d)

    # ------------------------------------------------------------ control actions
    async def _control(self, n: int, d) -> bool:
        a = d.args
        if d.name == "update_plan":
            self.ctx.plan = [str(s) for s in a.get("steps", [])][:12]
            self.ctx.observe(n, "update_plan", a, d.thought, {"plan": self.ctx.plan}, None)
        elif d.name == "remember":
            self.ctx.remember(str(a.get("key", "note")), str(a.get("value", "")), n)
            self.ctx.observe(n, "remember", a, d.thought, {"stored": a.get("key")}, None)
        elif d.name == "ask_human":
            q = str(a.get("question", ""))
            ans = await self.rt.human.answer(q)
            self.result.questions.append({"step": n, "question": q, "answer": ans})
            self._log({"event": "ask_human", "step": n, "question": q, "answered": ans is not None})
            if ans is None:
                self._stop(StopReason.NEEDS_HUMAN, summary=f"Paused: waiting for a person to answer: {q}",
                           status="needs_human")
                return True
            self.ctx.observe(n, "ask_human", a, d.thought, {"human_answer": ans}, None)
            self.ctx.remember(f"human_answer:{n}", f"Q: {q} A: {ans}", n)
        elif d.name == "finish":
            return await self._finish(n, d)
        return False

    async def _finish(self, n: int, d) -> bool:
        a = d.args
        status, summary = str(a.get("status", "")), str(a.get("summary", ""))
        if status == "cannot_complete":
            self._stop(StopReason.NEEDS_HUMAN, summary, status)
            return True
        claims = a.get("claims") or []
        verdicts = await self.verifier.verify(claims, self.ctx, self.allowed, self.run_id)
        self.result.verdicts = [v.__dict__ for v in verdicts]
        self._log({"event": "verify", "step": n, "verdicts": self.result.verdicts})
        if all(v.passed for v in verdicts):
            self._stop(StopReason.DONE, summary, status)
            if self.rt.learn and a.get("lesson"):
                append_lesson(self.rt.lessons_path, "Lesson from a verified run", str(a["lesson"]), tags=self.goal[:80])
            return True
        self.meter.verify_retries += 1
        feedback = "; ".join(f"{v.claim}: {v.detail}" for v in verdicts if not v.passed)
        self.ctx.observe(n, "finish", {"status": status}, d.thought, None, f"verification FAILED: {feedback}")
        if self.meter.verify_retries > self.rt.budget.max_verify_retries:
            self._stop(StopReason.VERIFICATION_FAILED, summary, status, detail=feedback[:300])
            return True
        self.ctx.note("verifier", "Your finish was NOT accepted. " + feedback +
                      " Fix the underlying problem (or finish with cannot_complete if it is not fixable).")
        return False

    # -------------------------------------------------------------- tool calls
    async def _act(self, n: int, d) -> None:
        res = await self.registry.call(d.name, d.args, allowed=self.allowed, run_id=self.run_id)
        if not res.ok and res.error.code in (ErrorCode.NEEDS_APPROVAL, ErrorCode.STALE_APPROVAL) and res.error.data:
            res = await self._gate(n, d, res)
        if res.ok:
            self.ctx.observe(n, d.name, d.args, d.thought, res.output, None)
        else:
            self.ctx.observe(n, d.name, d.args, d.thought, None, f"{res.error.code.value}: {res.error.message}")
        self._log({"event": "result", "step": n, "tool": d.name, "ok": res.ok, "risk": res.risk,
                   "error": res.error.model_dump(exclude={"data"}) if res.error else None, "ms": res.ms,
                   "screenshot": (res.output or {}).get("screenshot")})

    async def _gate(self, n: int, d, res: ToolResult) -> ToolResult:
        need = res.error.data
        decision = await self.gate.evaluate(need)
        rec = {"step": n, "tool": d.name, "digest": need["digest"], "verdict": decision.verdict, "reason": decision.reason,
               "checks": [c.__dict__ for c in decision.checks], "effect": need.get("preview")}
        if need["digest"] in self.rejected:
            rec["outcome"] = "refused: a person already rejected this exact action"
            self.result.approvals.append(rec)
            return self._refusal(d.name, ErrorCode.NOT_PERMITTED, rec["outcome"])
        if decision.verdict == "block":
            rec["outcome"] = "blocked by policy"
            self.result.approvals.append(rec)
            self._log({"event": "gate", **rec})
            return self._refusal(d.name, ErrorCode.CONFLICT, f"blocked by policy gate: {decision.reason}")
        if decision.verdict == "auto":
            approval = self.registry.approval_for(need, by="policy-gate", human=False, note=decision.reason)
            rec["approved_by"] = "policy-gate"
        else:
            ok, note = await self.rt.approver.decide(need, decision)
            if not ok:
                self.rejected.add(need["digest"])
                rec["outcome"] = f"rejected by human: {note or 'no reason given'}"
                self.result.approvals.append(rec)
                self._log({"event": "gate", **rec})
                self.ctx.note("gate", f"A person REJECTED this action ({note or 'no reason'}). Do not try to work around it; "
                                      "report this and finish with cannot_complete unless they gave different instructions.")
                return self._refusal(d.name, ErrorCode.NOT_PERMITTED, f"rejected by human approver: {note or 'no reason given'}")
            approval = self.registry.approval_for(need, by="human", human=True, note=note)
            rec["approved_by"] = "human"
        # executor re-validates: the registry recomputes the digest from the live page before acting
        res2 = await self.registry.call(d.name, d.args, allowed=self.allowed, approval=approval, run_id=self.run_id)
        rec["outcome"] = "executed" if res2.ok else f"not executed: {res2.error.code.value}"
        self.result.approvals.append(rec)
        self._log({"event": "gate", **rec})
        return res2

    @staticmethod
    def _refusal(tool: str, code: ErrorCode, msg: str) -> ToolResult:
        from ..core.contracts import ToolError
        return ToolResult(tool=tool, ok=False, error=ToolError(code=code, message=msg), risk="write_high")

    # ---------------------------------------------------------------- records
    def _log(self, rec: dict) -> None:
        rec = {"t": round(time.perf_counter() - self.meter.started, 2), **rec}
        self._steps_f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._steps_f.flush()
        if rec["event"] in ("decision", "result", "gate", "stop", "verify", "ask_human"):
            self.result.steps.append(rec)
            if os.environ.get("OPSAGENT_VERBOSE"):
                print(self._pretty(rec))

    @staticmethod
    def _pretty(rec: dict) -> str:
        e = rec["event"]
        if e == "decision":
            args = json.dumps(rec["args"], ensure_ascii=False)[:150]
            return f"  [{rec['step']:>2}] {rec['action']} {args}\n       why: {rec['thought']}"
        if e == "result":
            return f"       -> {'ok' if rec['ok'] else 'ERR ' + str(rec['error'] and rec['error']['code'])}"
        return f"  == {e}: " + json.dumps({k: v for k, v in rec.items() if k not in ('event', 't')}, default=str)[:200]

    def _write_report(self) -> None:
        r = self.result
        lines = [f"# Run {r.run_id}", "", f"**Goal:** {r.goal}", "",
                 f"**Outcome:** `{r.stop_reason.value}` (agent said: `{r.final_status or '-'}`)", "",
                 "## Summary", r.summary or "(none)", ""]
        if r.detail:
            lines += [f"**Detail:** {r.detail}", ""]
        lines += ["## Independent verification"] + (
            [f"- {'PASS' if v['passed'] else 'FAIL'} - {v['claim']}: {v['detail']}" for v in r.verdicts] or ["- (not reached)"])
        lines += ["", "## Approvals"] + ([f"- step {a['step']}: gate={a['verdict']} ({a['reason']}) -> {a.get('approved_by', '')} {a['outcome']}"
                                         for a in r.approvals] or ["- none required"])
        if r.questions:
            lines += ["", "## Questions asked"] + [f"- Q: {q['question']} / A: {q['answer']}" for q in r.questions]
        lines += ["", "## Steps (action - reason)"]
        for s in r.steps:
            if s["event"] == "decision":
                lines.append(f"{s['step']}. `{s['action']}` {json.dumps(s['args'], ensure_ascii=False)[:160]} - _{s['thought']}_")
        lines += ["", f"## Cost\n`{json.dumps(r.meter)}`", "", "Evidence: `screenshots/`, `audit.jsonl`, `steps.jsonl` in this folder."]
        (self.run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
