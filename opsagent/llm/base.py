"""LLM interface: one decision per call, always a function call.

The runtime never parses prose. Every action, including the control actions
(`update_plan`, `remember`, `ask_human`, `finish`), is a function the model
calls, so the output is structured by construction. Every function carries a
required `thought` argument: one sentence on why this action. That is the
audit trail for "why did the agent do that?".
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class LLMDecision:
    name: str
    args: dict
    thought: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    degraded: bool = False          # produced by a fallback model


class LLMError(Exception):
    pass


class LLM(Protocol):
    name: str

    async def decide(self, system: str, view: str, tools: list[dict]) -> LLMDecision: ...


THOUGHT = {"type": "string", "description": "One short sentence: why you chose this action."}


def with_thought(decl: dict) -> dict:
    """Add the required `thought` parameter to a function declaration."""
    d = copy.deepcopy(decl)
    params = d.setdefault("parameters", {"type": "object", "properties": {}})
    params.setdefault("type", "object")
    props = params.setdefault("properties", {})
    params["properties"] = {"thought": THOUGHT, **props}
    params["required"] = ["thought", *[r for r in params.get("required", []) if r != "thought"]]
    return d
