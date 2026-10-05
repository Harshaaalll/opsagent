"""Redaction for audit logs and anything that leaves the process.

Secrets are never given to the model in the first place (see the vault), so this
is the second line of defence: anything that still looks sensitive is masked
before it is written to disk.
"""

from __future__ import annotations

import re
from typing import Any

_PATTERNS = [
    (re.compile(r"\b\d{12}\b"), "[id]"),                               # aadhaar-like
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[pan]"),
    (re.compile(r"\b\d{9,18}\b"), "[acct]"),                            # bank account numbers
    (re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token)\s*[=:]\s*\S+"), r"\1=[redacted]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "[gemini-key]"),
]


def redact_text(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value
