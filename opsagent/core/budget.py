"""Stopping conditions and loop economics (ported from `sanwaad`, re-priced for Gemini).

Termination is designed, never left to the model's own sense of "done". Several
stopping conditions run at once and whichever fires first wins. Budgets are
checked BEFORE a pass, so a budget stops the next call rather than noticing
after it has been paid for.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class StopReason(str, Enum):
    DONE = "done"                          # finished AND independently verified
    NEEDS_HUMAN = "needs_human"            # a decision the agent must not make alone
    MAX_STEPS = "max_steps"
    BUDGET = "budget_exhausted"
    TIMEOUT = "timeout"
    STALLED = "stalled"                    # same action, same args, again and again
    VERIFICATION_FAILED = "verification_failed"
    REJECTED = "rejected"                  # the human declined an approval
    ERROR = "error"


# A run that stops for one of these did its job: it finished, or it correctly
# recognised a decision it should not make alone.
CLEAN_STOPS = frozenset({StopReason.DONE, StopReason.NEEDS_HUMAN, StopReason.REJECTED})

# USD per 1M tokens (input, output). Rough list prices; used for the cost meter only.
PRICING = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.0),
}


def estimate_tokens(text: str) -> int:
    """Conservative estimate: UTF-8 bytes / 4."""
    return max(1, len((text or "").encode("utf-8")) // 4)


def cost_usd(model: str, prompt_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICING.get(model, PRICING["gemini-2.5-flash"])
    return (prompt_tokens * pin + output_tokens * pout) / 1_000_000


def action_fingerprint(tool: str, args: dict) -> str:
    canon = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{tool}\x00{canon}".encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Budget:
    max_steps: int = 40
    max_tokens: int = 400_000
    max_cost_usd: float = 0.50
    max_seconds: float = 300.0
    stall_repeats: int = 3          # identical action this many times in a row
    max_verify_retries: int = 2     # times a failed verification may send the agent back to work
    context_tokens: int = 6_000     # rendered-history budget per pass; compaction above it


@dataclass
class Meter:
    budget: Budget
    model: str = "gemini-2.5-flash"
    steps: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    verify_retries: int = 0
    started: float = field(default_factory=time.perf_counter)
    recent: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    @property
    def seconds(self) -> float:
        return time.perf_counter() - self.started

    @property
    def cost(self) -> float:
        return cost_usd(self.model, self.prompt_tokens, self.output_tokens)

    def charge(self, prompt_tokens: int, output_tokens: int) -> None:
        self.llm_calls += 1
        self.prompt_tokens += prompt_tokens
        self.output_tokens += output_tokens

    def record_action(self, tool: str, args: dict) -> None:
        self.recent.append(action_fingerprint(tool, args))
        del self.recent[:-self.budget.stall_repeats]

    def stalled(self) -> bool:
        n = self.budget.stall_repeats
        return len(self.recent) >= n and len(set(self.recent[-n:])) == 1

    def exhausted(self) -> Optional[StopReason]:
        b = self.budget
        if self.seconds >= b.max_seconds:
            return StopReason.TIMEOUT
        if self.cost >= b.max_cost_usd or self.tokens >= b.max_tokens:
            return StopReason.BUDGET
        if self.steps >= b.max_steps:
            return StopReason.MAX_STEPS
        if self.stalled():
            return StopReason.STALLED
        return None

    def snapshot(self) -> dict:
        return {
            "steps": self.steps, "llm_calls": self.llm_calls, "tokens": self.tokens,
            "cost_usd": round(self.cost, 5), "seconds": round(self.seconds, 2),
            "verify_retries": self.verify_retries,
        }
