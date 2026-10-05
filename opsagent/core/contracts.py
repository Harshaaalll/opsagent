"""Tool contracts: a tool is an API, and gets designed like one.

Ported from my earlier project `sanwaad` and extended for browser work. Every
tool declares its name, input/output models, a risk level, a timeout and retry
semantics. Two additions matter for computer use:

  risk_fn   risk is not always a property of the tool. `browser_click` on a
            link is a read; `browser_click` on "Save bill" is a financial
            write. The tool decides per call, from what is actually on the page.
  preview   a high-risk call can describe its exact effect ("submit this form
            with these field values"). The approval is bound to a digest of
            that preview, and the registry re-computes it at execution time, so
            an approval cannot be replayed onto a different page state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Type

from pydantic import BaseModel, Field


class Risk(str, Enum):
    READ = "read"               # observes; changes nothing
    WRITE_LOW = "write_low"     # changes something recoverable (typing in a form, navigating)
    WRITE_HIGH = "write_high"   # financial, external or irreversible: needs an approval


class ErrorCode(str, Enum):
    INVALID_INPUT = "invalid_input"
    NOT_PERMITTED = "not_permitted"
    NEEDS_APPROVAL = "needs_approval"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"               # e.g. duplicate invoice
    TIMEOUT = "timeout"
    UPSTREAM = "upstream_error"
    INVALID_OUTPUT = "invalid_output"
    CIRCUIT_OPEN = "circuit_open"
    STALE_APPROVAL = "stale_approval"   # page changed between approval and execution


class ToolError(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    data: dict = Field(default_factory=dict)


class ToolFailure(Exception):
    """Raised by a handler to return a structured error instead of a crash."""

    def __init__(self, code: ErrorCode, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class Approval(BaseModel):
    """Permission for ONE call with ONE exact effect."""

    by: str
    human: bool
    tool: str
    digest: str
    note: str = ""


class ToolResult(BaseModel):
    tool: str
    ok: bool
    output: Optional[dict] = None
    error: Optional[ToolError] = None
    attempts: int = 0
    ms: float = 0.0
    audit_id: str = ""
    risk: str = ""


Handler = Callable[[Any], Awaitable[Any]]
RiskFn = Callable[[Any], Awaitable[Risk]]
PreviewFn = Callable[[Any], Awaitable[dict]]


class AnyOutput(BaseModel):
    model_config = {"extra": "allow"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: Type[BaseModel]
    handler: Handler
    risk: Risk = Risk.READ
    output_model: Type[BaseModel] = AnyOutput
    risk_fn: Optional[RiskFn] = None
    preview: Optional[PreviewFn] = None
    timeout_s: float = 20.0
    max_retries: int = 0
    idempotent: bool = False

    def as_function_schema(self) -> dict:
        """JSON-schema function declaration (Gemini / MCP-compatible shape)."""
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return {"name": self.name, "description": self.description, "parameters": schema}

    def as_mcp_tool(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_model.model_json_schema(),
            "annotations": {
                "readOnlyHint": self.risk is Risk.READ and self.risk_fn is None,
                "destructiveHint": self.risk is Risk.WRITE_HIGH or self.risk_fn is not None,
                "idempotentHint": self.idempotent,
            },
        }


def digest_of(*parts: Any) -> str:
    """Stable fingerprint, for binding approvals to an exact effect."""
    canon = json.dumps(
        [p.model_dump(mode="json") if isinstance(p, BaseModel) else p for p in parts],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
