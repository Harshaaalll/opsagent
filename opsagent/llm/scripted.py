"""A scripted LLM for tests and offline runs of the runtime.

It replays a list of (function name, args) decisions, or calls a function that
inspects the rendered view and returns the next decision. This lets the whole
runtime (browser, gate, registry, verifier, recovery) be tested deterministically
without an API key; it is NOT the demo agent. The demo and evals use Gemini.
"""

from __future__ import annotations

from typing import Callable, Union

from .base import LLMDecision

Script = Union[list, Callable[[str, int], tuple]]


class ScriptedLLM:
    name = "scripted"

    def __init__(self, script: Script):
        self.script = script
        self.i = 0
        self.views: list[str] = []

    async def decide(self, system: str, view: str, tools: list[dict]) -> LLMDecision:
        self.views.append(view)
        if callable(self.script):
            item = self.script(view, self.i)
        else:
            item = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        name, args, *rest = item
        return LLMDecision(name, dict(args), rest[0] if rest else "scripted", 400, 60, "scripted")
