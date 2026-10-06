"""Gemini backend (google-genai SDK) with tiered fallback.

Failure handling, in order: transient errors (429/5xx/timeouts) retry with
backoff; if the primary model keeps failing, one fallback model is tried and the
decision is marked `degraded`; if that fails too, LLMError is raised and the
run stops with a clear reason rather than guessing.

Function calling mode is ANY, so the model must call a function: there is no
free-text path for the runtime to misparse.
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Optional

from google import genai
from google.genai import types

from .base import LLMDecision, LLMError, with_thought


class GeminiLLM:
    def __init__(self, model: Optional[str] = None, fallback: Optional[str] = None,
                 api_key: Optional[str] = None, thinking_budget: int = 512, temperature: float = 0.1,
                 retries: int = 2):
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY is not set (copy .env.example to .env and fill it in)")
        self.client = genai.Client(api_key=key)
        self.model = model or os.environ.get("OPSAGENT_MODEL", "gemini-flash-latest")
        # comma-separated chain, tried in order after the primary
        chain = fallback or os.environ.get("OPSAGENT_FALLBACK_MODEL", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.5-flash,gemini-3.6-flash,gemini-3.7-flash,gemini-3.8-flash")
        self.fallbacks = [m.strip() for m in chain.split(",") if m.strip()]
        self.fallback = self.fallbacks[0] if self.fallbacks else None
        self.thinking_budget = thinking_budget
        self.temperature = temperature
        self.retries = retries
        self.name = f"gemini:{self.model}"
        self._dead: set[str] = set()

    async def _call(self, model: str, system: str, view: str, decls: list[dict]):
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=d["name"], description=d["description"],
                                          parameters_json_schema=d["parameters"]) for d in decls])],
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.ANY)),
            thinking_config=types.ThinkingConfig(thinking_budget=self.thinking_budget),
        )
        return await self.client.aio.models.generate_content(model=model, contents=view, config=cfg)

    @staticmethod
    def _classify(exc: Exception) -> str:
        """'retry' (transient: wait and try the same model), 'skip' (this model is unusable right now:
        daily quota exhausted or retired; go to the next model), or 'fatal' (our request is wrong)."""
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        msg = str(exc)
        if code == 429:
            return "skip" if "PerDay" in msg else "retry"   # daily quota will not recover within a run
        if code == 404:
            return "skip"
        if code in (408, 500, 502, 503, 504) or isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return "retry"
        return "fatal"

    async def decide(self, system: str, view: str, tools: list[dict]) -> LLMDecision:
        decls = [with_thought(t) for t in tools]
        last: Optional[Exception] = None
        chain = [(self.model, False)] + [(m, True) for m in self.fallbacks if m != self.model]
        for model, degraded in chain:
            if model in self._dead:
                continue
            for attempt in range(self.retries):
                try:
                    resp = await asyncio.wait_for(self._call(model, system, view, decls), timeout=90)
                    calls = resp.function_calls or []
                    um = resp.usage_metadata
                    pt = (um.prompt_token_count or 0) if um else 0
                    ot = ((um.candidates_token_count or 0) + (getattr(um, "thoughts_token_count", 0) or 0)) if um else 0
                    if not calls:
                        # model answered in prose despite mode ANY: report it as a no-op the loop can correct
                        return LLMDecision("__invalid__", {"text": (resp.text or "")[:300]}, "", pt, ot, model, degraded)
                    fc = calls[0]
                    args = dict(fc.args or {})
                    thought = str(args.pop("thought", ""))
                    return LLMDecision(fc.name, args, thought, pt, ot, model, degraded)
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    kind = self._classify(exc)
                    if kind == "skip":
                        self._dead.add(model)      # do not spend another request discovering this again
                        break
                    if kind == "fatal":
                        raise LLMError(f"request rejected: {type(exc).__name__}: {str(exc)[:300]}")
                    await asyncio.sleep(min(2 ** attempt * 1.5, 20) + random.random())
        raise LLMError(f"model unavailable: {type(last).__name__}: {str(last)[:200]}")

    async def transcribe(self, audio: bytes, mime: str = "audio/wav") -> str:
        """Speech to text (English, translating Hindi/Hinglish). Same fallback chain and quota handling as decide()."""
        prompt = ("Transcribe this spoken work request exactly. If it is not in English, translate it to English. "
                  "Output only the request text: no quotes, no commentary. If there is no speech, output NO_SPEECH.")
        last: Optional[Exception] = None
        for model in [self.model] + [m for m in self.fallbacks if m != self.model]:
            if model in self._dead:
                continue
            for attempt in range(self.retries):
                try:
                    resp = await asyncio.wait_for(self.client.aio.models.generate_content(
                        model=model, contents=[prompt, types.Part.from_bytes(data=audio, mime_type=mime)],
                        config=types.GenerateContentConfig(temperature=0.0)), timeout=90)
                    return (resp.text or "").strip()
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    kind = self._classify(exc)
                    if kind == "skip":
                        self._dead.add(model)
                        break
                    if kind == "fatal":
                        raise LLMError(f"transcription rejected: {type(exc).__name__}: {str(exc)[:300]}")
                    await asyncio.sleep(min(2 ** attempt * 1.5, 20) + random.random())
        raise LLMError(f"transcription unavailable: {type(last).__name__}: {str(last)[:200]}")
