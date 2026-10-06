"""Run the eval suite against the real Gemini-driven agent.

    python -m evals.run                       # all scenarios
    python -m evals.run --only s01_happy,s04_flaky_after_save
    python -m evals.run --repeat 3            # reliability over repeated runs (LLMs are stochastic)

Writes evals/RESULTS.md. Numbers there come from real runs; nothing is hand-edited.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from opsagent.cli import DEFAULT_URL, ensure_sandbox
from opsagent.llm.gemini import GeminiLLM
from opsagent.runtime.agent import Runtime, ScriptedHuman
from opsagent.runtime.gate import ScriptedApprover

from .grader import grade_trace
from .scenarios import SCENARIOS, Scenario

ROOT = Path(__file__).resolve().parents[1]


async def run_one(sc: Scenario, base_url: str, model: str | None, runs_dir: Path) -> dict:
    httpx.post(base_url + "/__admin__/reset", json={"faults": sc.faults}, timeout=5)
    approver, human = ScriptedApprover(sc.approve, "" if sc.approve else "eval: rejected"), ScriptedHuman(list(sc.human_answers))
    rt = Runtime(GeminiLLM(model=model), base_url=base_url, runs_dir=runs_dir, approver=approver, human=human, learn=False)
    t0 = time.time()
    res = await rt.run(sc.task)
    bills = httpx.get(base_url + "/__admin__/state").json()["bills"]
    problems = []
    if (err := sc.outcome(bills)):
        problems.append(f"final state: {err}")
    if res.stop_reason.value not in sc.expect_stop:
        problems.append(f"stop reason {res.stop_reason.value!r} not in {sc.expect_stop}")
    if sc.expect_human_question is not None and bool(human.asked) != sc.expect_human_question:
        problems.append(f"human question asked={bool(human.asked)}, expected {sc.expect_human_question}")
    if sc.expect_gate and not any(a["verdict"] == sc.expect_gate for a in res.approvals):
        problems.append(f"expected a gate verdict of {sc.expect_gate!r}, got {[a['verdict'] for a in res.approvals]}")
    violations = grade_trace(res.run_dir)
    for v in violations:
        problems.append(f"trace: {v.invariant} at step {v.step}: {v.detail}")
    return {"id": sc.id, "title": sc.title, "passed": not problems, "problems": problems, "stop": res.stop_reason.value,
            "steps": res.meter.get("steps"), "cost": res.meter.get("cost_usd"), "secs": round(time.time() - t0, 1),
            "run_dir": str(res.run_dir.relative_to(ROOT)) if res.run_dir.is_relative_to(ROOT) else str(res.run_dir)}


async def main() -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=DEFAULT_URL)
    args = ap.parse_args()
    only = set(filter(None, args.only.split(",")))
    scs = [s for s in SCENARIOS if not only or s.id in only]
    proc = ensure_sandbox(args.base_url)
    runs_dir = ROOT / "runs"
    rows = []
    try:
        for sc in scs:
            for k in range(args.repeat):
                print(f"-> {sc.id} ({k + 1}/{args.repeat}) {sc.title}", flush=True)
                r = await run_one(sc, args.base_url, args.model, runs_dir)
                print(f"   {'PASS' if r['passed'] else 'FAIL'}  {r['steps']} steps  ${r['cost']}  {r['secs']}s", flush=True)
                for p in r["problems"]:
                    print("     -", p)
                rows.append(r)
    finally:
        if proc:
            proc.terminate()
    passed = sum(r["passed"] for r in rows)
    lines = [f"# Eval results", "", f"Model: `{args.model or os.environ.get('OPSAGENT_MODEL', 'gemini-flash-latest')}`  |  "
             f"{time.strftime('%Y-%m-%d %H:%M')}  |  **{passed}/{len(rows)} runs passed**", "",
             "| scenario | result | stop | steps | cost (USD) | time | first problem |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {'PASS' if r['passed'] else 'FAIL'} | {r['stop']} | {r['steps']} | {r['cost']} | {r['secs']}s | "
                     f"{(r['problems'][0] if r['problems'] else '')[:90]} |")
    (ROOT / "evals" / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{passed}/{len(rows)} passed -> evals/RESULTS.md")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
