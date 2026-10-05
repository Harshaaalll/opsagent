"""Credential vault: the model never sees a secret.

The model types `{{vault:erp.password}}`; the tool layer swaps it for the real
value at the last moment, inside the browser. The model, the run state and the
audit log only ever contain the placeholder. Values come from environment
variables (VAULT_ERP_PASSWORD, ...), which in production would be a secrets
manager.
"""

from __future__ import annotations

import os
import re

_PLACEHOLDER = re.compile(r"\{\{vault:([a-z0-9_.]+)\}\}", re.I)

NAMES = ("erp.user", "erp.password", "mail.user", "mail.password")


class VaultError(Exception):
    pass


def resolve(text: str) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1).lower()
        if key not in NAMES:
            raise VaultError(f"unknown vault entry {key!r}; available: {', '.join(NAMES)}")
        val = os.environ.get("VAULT_" + key.replace(".", "_").upper())
        if val is None:
            raise VaultError(f"vault entry {key!r} is not configured")
        return val

    return _PLACEHOLDER.sub(sub, text)


def has_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER.search(text))
