"""Verification: "done" is a request, granted only on independent evidence.

When the agent calls `finish(status="completed")` it must attach CLAIMS: each is
a read-only tool call plus the values it expects back. The verifier (plain code,
no LLM) then:

  1. RE-READS the world itself: runs each claim's read tool and compares the
     result with the expectation (post-state check, through a channel the agent
     did not write through),
  2. checks PROVENANCE: every expected value must appear in what the agent read
     from a *source* system (PDF, mail), so a hallucinated value cannot
     verify itself even if it was typed into the ERP consistently,
  3. returns per-claim verdicts. Any failure sends the agent back to work with
     the specific feedback, within a bounded retry budget.

This is what keeps "I entered it" from being the evidence for "I entered it".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..core.contracts import Risk
from ..core.registry import ToolRegistry
from .context import Context

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_FULL = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
         "October", "November", "December"]


@dataclass
class Verdict:
    claim: str
    passed: bool
    detail: str


def _get(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(path)
    return cur


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return abs(float(str(actual).replace(",", "")) - float(expected)) < 0.005
        except ValueError:
            return False
    return str(actual).strip().lower() == str(expected).strip().lower()


def _variants(value: Any) -> list[str]:
    """Textual forms a value may take in a source document."""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        v = float(value)
        return [f"{v:,.2f}", f"{v:.2f}", f"{v:,.0f}" if v == int(v) else f"{v:,.2f}", str(int(v)) if v == int(v) else str(v)]
    s = str(value).strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        d = date(int(m[1]), int(m[2]), int(m[3]))
        return [s, f"{d.day:02d} {_MONTHS[d.month - 1]} {d.year}", f"{d.day} {_MONTHS[d.month - 1]} {d.year}",
                f"{d.day} {_FULL[d.month - 1]} {d.year}", f"{_MONTHS[d.month - 1]} {d.day}, {d.year}", f"{d.day:02d}/{d.month:02d}/{d.year}"]
    return [s]


def appears(value: Any, haystack: str) -> bool:
    low = haystack.lower()
    return any(v and v.lower() in low for v in _variants(value))


# fields that are bookkeeping, not facts that must come from a source document
_NO_PROVENANCE = {"count", "status", "id", "notes", "currency"}


class Verifier:
    def __init__(self, registry: ToolRegistry, provenance_exclude_urls: tuple[str, ...] = ("/erp/",)):
        self.registry = registry
        self.exclude = provenance_exclude_urls

    async def verify(self, claims: list[dict], ctx: Context, allowed: list[str], run_id: str) -> list[Verdict]:
        if not claims:
            return [Verdict("(no claims)", False, "a completed task needs at least one verifiable claim "
                            "(a read tool call plus the values you expect it to return)")]
        source = ctx.source_text(self.exclude)
        out: list[Verdict] = []
        for c in claims:
            label = str(c.get("description") or c.get("tool"))
            tool = str(c.get("tool", ""))
            spec = self.registry.get(tool)
            if spec is None or spec.risk is not Risk.READ or spec.risk_fn is not None:
                out.append(Verdict(label, False, f"{tool!r} is not a read-only tool; claims must be checked with a read tool"))
                continue
            try:
                args = json.loads(c.get("args_json") or "{}")
                expect = json.loads(c.get("expect_json") or "{}")
            except json.JSONDecodeError as exc:
                out.append(Verdict(label, False, f"claim JSON invalid: {exc}"))
                continue
            res = await self.registry.call(tool, args, allowed=allowed, run_id=run_id)
            if not res.ok:
                out.append(Verdict(label, False, f"could not re-read the world: {res.error.message}"))
                continue
            problems = []
            if not expect:
                problems.append("no expected values given")
            for path, want in expect.items():
                try:
                    got = _get(res.output, path)
                except (KeyError, IndexError, ValueError):
                    problems.append(f"{path}: not present in {tool} result")
                    continue
                if not _same(got, want):
                    problems.append(f"{path}: expected {want!r}, the system shows {got!r}")
                elif path.split(".")[-1] not in _NO_PROVENANCE and not appears(want, source):
                    problems.append(f"{path}={want!r}: matches the system but was never read from a source "
                                    "document (PDF/mail), so its origin is unverified")
            out.append(Verdict(label, not problems, "verified against the system" if not problems else "; ".join(problems)))
        return out
