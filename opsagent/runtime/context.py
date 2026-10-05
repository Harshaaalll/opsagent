"""The run context, managed as a budget (ported from sanwaad's ContextWindow).

A loop's context grows on its own: every observation is appended, pass after
pass. Long before the hard limit, quality drops (context rot). So each pass:

1. SHAPING   page snapshots are large and go stale at once; only the NEWEST
             snapshot is shown in full, older ones collapse to a one-line outcome.
2. FACTS     durable information (PDF text, ERP lookups, things the agent chose
             to `remember`) is written to a scratchpad and always shown, so it
             survives compaction even when the step that produced it does not.
3. COMPACTION old steps are folded into one-line summaries under a token budget;
             the last KEEP_RECENT steps stay verbatim.

`all_seen` keeps every raw observation, uncompacted, for the verifier's
provenance check, so compaction can never make a true fact look invented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ..core.budget import estimate_tokens
from ..core.redact import redact_text

KEEP_RECENT = 2
MAX_FACTS_SHOWN = 12


@dataclass
class Step:
    n: int
    tool: str
    args: dict
    thought: str
    ok: bool
    outcome: str            # short, one line
    detail: str = ""        # full observation text for the newest steps


@dataclass
class Fact:
    key: str
    text: str
    step: int


# control actions whose content the agent (or the harness) wrote: never usable as provenance
AGENT_AUTHORED = {"update_plan", "remember", "finish"}

PIN_TOOLS = {"read_pdf": "pdf", "erp_find_bills": "erp", "search_company_knowledge": "policy"}


class Context:
    def __init__(self, goal: str, budget_tokens: int):
        self.goal = goal
        self.budget_tokens = budget_tokens
        self.steps: list[Step] = []
        self.facts: dict[str, Fact] = {}
        self.plan: list[str] = []
        self.notes: list[str] = []          # kernel / verifier / gate messages shown on the next pass
        self.retrieved: str = ""
        self.all_seen: list[dict] = []      # raw {tool,url,text}, never compacted

    # -- writers ------------------------------------------------------------------
    def remember(self, key: str, text: str, step: int) -> None:
        self.facts[key] = Fact(key, text[:1500], step)

    def note(self, who: str, text: str) -> None:
        self.notes.append(f"[{who}] {text}")

    def observe(self, step: int, tool: str, args: dict, thought: str, output: Optional[dict],
                error: Optional[str]) -> None:
        if error:
            outcome, detail = f"FAILED: {error}", f"FAILED: {error}"
            # failures are never evidence: the verifier's own feedback must not become a "source"

        else:
            raw = json.dumps(output, ensure_ascii=False, default=str)
            if tool not in AGENT_AUTHORED:
                self.all_seen.append({"tool": tool, "url": (output or {}).get("url", ""), "text": raw})
            if output and "snapshot" in output:
                detail = output["snapshot"]
                extras = {k: v for k, v in output.items() if k not in ("snapshot", "screenshot", "url")}
                if extras:
                    detail += "\nRESULT: " + json.dumps(extras, ensure_ascii=False, default=str)
                first = detail.splitlines()[0]
                outcome = f"{first[:110]}" + (f" | {json.dumps(extras, default=str)[:80]}" if extras else "")
            else:
                detail = raw[:2500]
                outcome = raw[:140]
            if tool in PIN_TOOLS and output:
                self.remember(f"{PIN_TOOLS[tool]}:{step}", self._pin_text(tool, args, output), step)
        self.steps.append(Step(step, tool, args, thought, error is None, redact_text(outcome), redact_text(detail)))

    @staticmethod
    def _pin_text(tool: str, args: dict, out: dict) -> str:
        if tool == "read_pdf":
            return f"PDF {out.get('path')}: " + " ".join((out.get("text") or "").split())[:900]
        if tool == "erp_find_bills":
            bills = out.get("bills", [])
            return f"ERP lookup {args}: {out.get('count', 0)} bill(s) " + "; ".join(
                f"{b['invoice_number']} {b['vendor']} {b['currency']} {b['amount']} due {b['due_date']} [{b['status']}]"
                for b in bills[:4])
        if tool == "search_company_knowledge":
            return "POLICY: " + " | ".join(f"[{r['id']}] {r['text'][:300]}" for r in out.get("results", [])[:2])
        return json.dumps(out, default=str)[:400]

    # -- renderer -----------------------------------------------------------------
    def render(self) -> str:
        parts = [f"GOAL: {self.goal}"]
        if self.retrieved:
            parts.append("COMPANY KNOWLEDGE (retrieved for this goal):\n" + self.retrieved)
        if self.plan:
            parts.append("YOUR PLAN:\n" + "\n".join(f"  {i + 1}. {p}" for i, p in enumerate(self.plan)))
        if self.facts:
            shown = sorted(self.facts.values(), key=lambda f: f.step)[-MAX_FACTS_SHOWN:]
            parts.append("FACTS YOU HAVE ESTABLISHED (durable):\n" + "\n".join(f"  - {f.key}: {f.text}" for f in shown))

        recent = self.steps[-KEEP_RECENT:]
        older = self.steps[:-KEEP_RECENT]
        history = []
        for s in older:
            history.append(f"  #{s.n} {s.tool}({self._argstr(s.args)}) -> {'ok' if s.ok else 'ERR'} {s.outcome}")
        # compaction: the summary is itself bounded
        budget = self.budget_tokens
        dropped = 0
        while history and estimate_tokens("\n".join(history)) > budget // 3:
            history.pop(0)
            dropped += 1
        if dropped:
            history.insert(0, f"  ... {dropped} earlier steps compacted away (durable facts are kept above) ...")
        if history:
            parts.append("EARLIER STEPS (compacted):\n" + "\n".join(history))
        for i, s in enumerate(recent):
            newest = i == len(recent) - 1
            body = s.detail if newest else s.outcome
            parts.append(f"STEP #{s.n}: {s.tool}({self._argstr(s.args)})   because: {s.thought}\n"
                         f"  RESULT: {body}")
        if self.notes:
            parts.append("SYSTEM NOTICES (read these):\n" + "\n".join(f"  {n}" for n in self.notes))
            self.notes = []
        parts.append("Choose your next action (call exactly one function).")
        return "\n\n".join(parts)

    @staticmethod
    def _argstr(args: dict) -> str:
        return ", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items())

    def source_text(self, exclude_url_substrings: tuple[str, ...] = ()) -> str:
        """Everything observed from SOURCE systems: used for provenance checks. Observations from
        the system of record the agent writes into (e.g. /erp/) are excluded, otherwise a value the
        agent merely typed would 'prove' itself."""
        keep = []
        for o in self.all_seen:
            if o["tool"] in ("erp_find_bills", "browser_type", "browser_select"):
                continue
            if any(x in (o["url"] or "") for x in exclude_url_substrings):
                continue
            keep.append(o["text"])
        return "\n".join(keep)
