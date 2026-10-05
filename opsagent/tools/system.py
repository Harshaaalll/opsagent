"""Non-browser tools: files, ERP lookup API, company knowledge.

`erp_find_bills` is a second *channel* into the ERP. The agent writes through
the browser UI; reading back through an API gives the duplicate check and the
verifier an independent view of the truth.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel, Field
from pypdf import PdfReader

from ..core.contracts import ErrorCode, Risk, ToolFailure, ToolSpec
from ..core.knowledge import Knowledge


class ReadPdfIn(BaseModel):
    path: str = Field(description="Workspace-relative path returned by browser_download, e.g. downloads/INV-1.pdf")


class FindBillsIn(BaseModel):
    vendor: str = Field(default="", description="Substring of the vendor name (optional)")
    invoice_number: str = Field(default="", description="Exact invoice number (optional)")


class SearchKnowledgeIn(BaseModel):
    query: str = Field(description="What you want to know, in plain words")


class ErpClient:
    def __init__(self, base_url: str, api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("ERP_API_KEY", "erp-api-key-demo")

    async def find_bills(self, vendor: str = "", invoice_number: str = "") -> dict:
        async with httpx.AsyncClient(timeout=8) as c:
            try:
                r = await c.get(f"{self.base_url}/api/erp/bills", headers={"X-API-Key": self.api_key},
                                params={"vendor": vendor, "invoice_number": invoice_number})
            except httpx.HTTPError as exc:
                raise ToolFailure(ErrorCode.UPSTREAM, f"ERP API unreachable: {type(exc).__name__}", retryable=True)
        if r.status_code >= 500:
            raise ToolFailure(ErrorCode.UPSTREAM, f"ERP API error {r.status_code}", retryable=True)
        if r.status_code != 200:
            raise ToolFailure(ErrorCode.NOT_PERMITTED, f"ERP API refused: HTTP {r.status_code}")
        return r.json()


def build_system_tools(workdir: Path, erp: ErpClient, knowledge: Knowledge) -> list[ToolSpec]:
    async def read_pdf(inp: ReadPdfIn):
        path = (workdir / inp.path).resolve()
        if workdir.resolve() not in path.parents:
            raise ToolFailure(ErrorCode.NOT_PERMITTED, "path is outside the run workspace")
        if not path.exists():
            raise ToolFailure(ErrorCode.NOT_FOUND, f"no such file: {inp.path}")
        try:
            text = "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
        except Exception as exc:
            raise ToolFailure(ErrorCode.INVALID_INPUT, f"could not parse PDF: {type(exc).__name__}")
        return {"path": inp.path, "text": text.strip()[:6000]}

    async def find_bills(inp: FindBillsIn):
        return await erp.find_bills(inp.vendor, inp.invoice_number)

    async def search_knowledge(inp: SearchKnowledgeIn):
        hits = knowledge.search(inp.query, k=3)
        return {"results": [{"id": c.id, "heading": c.heading, "text": c.text} for c, _ in hits]}

    return [
        ToolSpec("read_pdf", "Extract the text of a downloaded PDF. The PDF is the authoritative source for invoice data.",
                 ReadPdfIn, read_pdf, Risk.READ),
        ToolSpec("erp_find_bills", "Look up bills already in the ERP by vendor and/or invoice number (read-only API). "
                                   "Use before entering a bill (duplicates) and to check a save.",
                 FindBillsIn, find_bills, Risk.READ, timeout_s=8, max_retries=2, idempotent=True),
        ToolSpec("search_company_knowledge", "Search company procedures and policies (AP rules, approvals, systems, security).",
                 SearchKnowledgeIn, search_knowledge, Risk.READ),
    ]
