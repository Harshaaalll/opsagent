"""The tool registry: the one door between the model and the outside world.

Every call goes through `ToolRegistry.call`, which applies the same checks in
the same order whatever the model asked for:

    1. does the tool exist?
    2. is it granted to this run? (least privilege)
    3. do the arguments satisfy the input contract?
    4. what is the risk of THIS call? (static, or decided from live page state)
    5. high-risk? compute the exact effect (preview), and require an approval
       bound to a digest of tool + args + preview. The digest is recomputed here,
       at execution time, so an approval for yesterday's form cannot submit
       today's.
    6. circuit breaker, timeout, retries only where repeating cannot repeat the
       effect (reads and idempotent tools)
    7. output contract
    8. redacted audit record

The model can propose anything. It can only DO what passes this function.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel, ValidationError

from .breaker import CircuitBreaker
from .contracts import (Approval, ErrorCode, Risk, ToolError, ToolFailure, ToolResult,
                        ToolSpec, digest_of)
from .redact import redact


def _summarise(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "input"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


class ToolRegistry:
    def __init__(self, audit_path: Optional[Path] = None, breaker: Optional[CircuitBreaker] = None):
        self._tools: dict[str, ToolSpec] = {}
        self._faults: dict[str, list[ToolError]] = {}
        self.audit_path = audit_path
        self.breaker = breaker or CircuitBreaker()

    # --- registration -------------------------------------------------------
    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def specs(self, allowed: Optional[Iterable[str]] = None) -> list[ToolSpec]:
        names = set(allowed) if allowed is not None else None
        return [s for s in self._tools.values() if names is None or s.name in names]

    # --- fault injection, for evals -----------------------------------------
    def inject_fault(self, name: str, error: ToolError, times: int = 1) -> None:
        self._faults.setdefault(name, []).extend([error] * times)

    def clear_faults(self) -> None:
        self._faults.clear()
        self.breaker.reset()

    def _next_fault(self, name: str) -> Optional[ToolError]:
        q = self._faults.get(name)
        return q.pop(0) if q else None

    # --- approvals ------------------------------------------------------------
    def approval_for(self, request: dict, *, by: str, human: bool, note: str = "") -> Approval:
        """Issue an approval for exactly the effect described by `request`
        (the `data` of a NEEDS_APPROVAL error)."""
        return Approval(by=by, human=human, tool=request["tool"], digest=request["digest"], note=note)

    # --- calling ----------------------------------------------------------------
    async def call(self, name: str, args: dict, *, allowed: Optional[Iterable[str]] = None,
                   approval: Optional[Approval] = None, run_id: Optional[str] = None) -> ToolResult:
        started = time.perf_counter()
        spec = self._tools.get(name)
        risk_seen = ""
        result = await self._call(spec, name, args, allowed, approval)
        risk_seen = result.risk
        result.ms = round((time.perf_counter() - started) * 1000, 1)
        result.audit_id = uuid.uuid4().hex[:10]
        self._audit(result, args, approval, run_id)
        return result

    async def _call(self, spec: Optional[ToolSpec], name: str, args: dict,
                    allowed: Optional[Iterable[str]], approval: Optional[Approval]) -> ToolResult:
        def refuse(code: ErrorCode, message: str, risk: str = "", data: Optional[dict] = None) -> ToolResult:
            return ToolResult(tool=name, ok=False, attempts=0, risk=risk,
                              error=ToolError(code=code, message=message, data=data or {}))

        if spec is None:
            return refuse(ErrorCode.NOT_FOUND, f"no tool named {name!r}")
        if allowed is not None and name not in set(allowed):
            return refuse(ErrorCode.NOT_PERMITTED, f"tool {name!r} is not granted to this run")
        try:
            inp = spec.input_model.model_validate(args or {})
        except ValidationError as exc:
            return refuse(ErrorCode.INVALID_INPUT, _summarise(exc))

        try:
            risk = await spec.risk_fn(inp) if spec.risk_fn else spec.risk
        except ToolFailure as exc:
            return refuse(exc.code, exc.message)

        if risk is Risk.WRITE_HIGH:
            try:
                preview = await spec.preview(inp) if spec.preview else {}
            except ToolFailure as exc:
                return refuse(exc.code, exc.message, risk.value)
            digest = digest_of(name, inp, preview)
            need = {"tool": name, "args": inp.model_dump(mode="json"),
                    "preview": preview, "digest": digest}
            if approval is None:
                return refuse(ErrorCode.NEEDS_APPROVAL,
                              f"{name} is a high-risk write and needs approval", risk.value, need)
            if approval.tool != name:
                return refuse(ErrorCode.NEEDS_APPROVAL, "approval was issued for a different tool",
                              risk.value, need)
            if approval.digest != digest:
                return refuse(ErrorCode.STALE_APPROVAL,
                              "the effect changed since it was approved (page or arguments differ); "
                              "approval is void", risk.value, need)

        retry_safe = risk is Risk.READ or spec.idempotent
        max_attempts = 1 + (spec.max_retries if retry_safe else 0)

        if not self.breaker.allows(name):
            return refuse(ErrorCode.CIRCUIT_OPEN, f"{name} is failing; not called (circuit open)", risk.value)

        last: Optional[ToolError] = None
        attempt = 0
        reported = False
        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    injected = self._next_fault(name)
                    if injected is not None:
                        raise ToolFailure(injected.code, injected.message, injected.retryable)
                    raw = await asyncio.wait_for(spec.handler(inp), timeout=spec.timeout_s)
                except (asyncio.TimeoutError, TimeoutError):
                    last = ToolError(code=ErrorCode.TIMEOUT, retryable=True,
                                     message=f"no response within {spec.timeout_s}s")
                except ToolFailure as exc:
                    last = ToolError(code=exc.code, message=exc.message, retryable=exc.retryable)
                except Exception as exc:  # never forward a raw exception to the model
                    last = ToolError(code=ErrorCode.UPSTREAM, retryable=False,
                                     message=f"unexpected {type(exc).__name__}: {str(exc)[:160]}")
                else:
                    try:
                        payload = raw.model_dump() if isinstance(raw, BaseModel) else raw
                        out = spec.output_model.model_validate(payload)
                    except ValidationError as exc:
                        self.breaker.record_outcome(name, ErrorCode.INVALID_OUTPUT)
                        reported = True
                        return ToolResult(tool=name, ok=False, attempts=attempt, risk=risk.value,
                                          error=ToolError(code=ErrorCode.INVALID_OUTPUT, message=_summarise(exc)))
                    self.breaker.record_outcome(name)
                    reported = True
                    return ToolResult(tool=name, ok=True, attempts=attempt, risk=risk.value,
                                      output=out.model_dump(mode="json"))
                self.breaker.record_outcome(name, last.code)
                reported = True
                if not last.retryable or not self.breaker.allows(name):
                    break
            return ToolResult(tool=name, ok=False, attempts=attempt, risk=risk.value, error=last)
        finally:
            if not reported:
                self.breaker.abandon(name)

    # --- audit ----------------------------------------------------------------
    def _audit(self, result: ToolResult, args: Any, approval: Optional[Approval], run_id: Optional[str]) -> None:
        if self.audit_path is None:
            return
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "audit_id": result.audit_id, "run_id": run_id,
            "tool": result.tool, "risk": result.risk, "args": redact(args),
            "ok": result.ok,
            "error_code": result.error.code.value if result.error else None,
            "error": result.error.message if result.error else None,
            "attempts": result.attempts, "ms": result.ms,
            "approved_by": approval.by if approval else None,
            "human_approved": approval.human if approval else None,
            "approval_digest": approval.digest if approval else None,
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass  # auditing must never be the reason a call fails
