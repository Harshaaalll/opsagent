"""Browser tools: a real Chromium driven through Playwright, observed as text.

Design choices that matter:

* The model never sees raw HTML or pixels. Each observation is a compact
  *snapshot*: visible text plus a numbered list of interactive elements with
  their accessible labels. The model acts on `ref` numbers, not CSS selectors,
  so a renamed button or reordered form does not break it (selector-based
  scripts would).
* Risk is decided per click from the live page (`data-risk="high"` on the
  element, or a submit button of a form that writes), not from the tool name.
* High-risk clicks carry a `preview`: the exact form values about to be
  submitted. Approval binds to a digest of that preview.
* Secrets: `{{vault:...}}` placeholders are resolved here, inside the tool;
  typed passwords are never echoed back into observations.
* Navigation is restricted to an allowlist of hosts.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from pydantic import BaseModel, Field

from ..core import vault
from ..core.contracts import ErrorCode, Risk, ToolFailure, ToolSpec

_SNAPSHOT_JS = """
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const labelOf = el => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) return l.innerText.trim(); }
    const w = el.closest('label'); if (w) return w.innerText.trim();
    return (el.placeholder || el.innerText || el.value || el.name || '').trim();
  };
  document.querySelectorAll('[data-opsref]').forEach(e => e.removeAttribute('data-opsref'));
  const els = [...document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, [role=button]')].filter(vis);
  const items = els.map((el, i) => {
    el.setAttribute('data-opsref', String(i + 1));
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type') || '';
    const o = { ref: i + 1, tag, type, label: labelOf(el).slice(0, 80), risk: el.getAttribute('data-risk') || '' };
    if (tag === 'a') o.href = el.getAttribute('href');
    if (tag === 'input' || tag === 'textarea') o.value = type === 'password' ? (el.value ? '(set)' : '') : (el.value || '').slice(0, 80);
    if (tag === 'select') { o.value = el.value; o.options = [...el.options].map(x => x.text).slice(0, 12); }
    return o;
  });
  const alerts = [...document.querySelectorAll('[role=alert], .err, .ok')].map(e => e.innerText.trim()).filter(Boolean);
  return { title: document.title, url: location.href, alerts, text: document.body.innerText.slice(0, 2500), items };
}
"""

_FORM_JS = """
(ref) => {
  const el = document.querySelector(`[data-opsref="${ref}"]`);
  if (!el) return null;
  const form = el.closest('form');
  const fields = {};
  if (form) {
    for (const f of form.querySelectorAll('input:not([type=hidden]), select, textarea')) {
      let label = f.id && document.querySelector(`label[for="${f.id}"]`);
      label = label ? label.innerText.trim() : (f.name || f.placeholder || '');
      fields[label] = f.tagName === 'SELECT' ? (f.options[f.selectedIndex] ? f.options[f.selectedIndex].text : '') : f.value;
    }
  }
  return { label: (el.innerText || el.value || '').trim(), risk: el.getAttribute('data-risk') || '',
           effect: el.getAttribute('data-effect') || '', action: form ? form.getAttribute('action') : '',
           method: form ? (form.getAttribute('method') || 'get') : '', fields };
}
"""


_CURSOR_JS = """
(() => {
  const c = document.createElement('div');
  c.style.cssText = 'position:fixed;z-index:2147483647;width:20px;height:20px;border-radius:50%;background:rgba(10,107,91,.45);'
    + 'border:2px solid #0a6b5b;pointer-events:none;left:-40px;top:-40px;transition:left .3s ease,top .3s ease,transform .12s';
  const add = () => document.documentElement.appendChild(c);
  if (document.documentElement) add(); else document.addEventListener('DOMContentLoaded', add);
  addEventListener('mousemove', e => { c.style.left = (e.clientX - 10) + 'px'; c.style.top = (e.clientY - 10) + 'px'; }, true);
  addEventListener('mousedown', () => { c.style.transform = 'scale(.55)'; }, true);
  addEventListener('mouseup', () => { c.style.transform = 'scale(1)'; }, true);
})();
"""


def render_snapshot(snap: dict, max_text: int = 1200) -> str:
    lines = [f"PAGE: {snap['title']} | {snap['url']}"]
    if snap["alerts"]:
        lines.append("ALERTS: " + " || ".join(snap["alerts"])[:300])
    text = re.sub(r"\n{2,}", "\n", snap["text"]).strip()
    lines.append("TEXT: " + text[:max_text].replace("\n", " / "))
    lines.append("ELEMENTS:")
    for it in snap["items"]:
        d = f"[{it['ref']}] {it['tag']}{('/' + it['type']) if it['type'] else ''} \"{it['label']}\""
        if it.get("href"):
            d += f" -> {it['href']}"
        if it.get("value") not in (None, ""):
            d += f" value={it['value']!r}"
        if it.get("options"):
            d += f" options={it['options']}"
        if it["risk"]:
            d += f" (RISK:{it['risk'].upper()})"
        lines.append(d)
    return "\n".join(lines)


class BrowserSession:
    def __init__(self, workdir: Path, allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1"),
                 headless: bool = True, slow_mo: int = 0):
        self.workdir = workdir
        self.allowed_hosts = allowed_hosts
        self.headless = headless
        self.slow_mo = slow_mo
        self._pw = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.shots = 0
        self.last_snapshot: Optional[dict] = None
        (workdir / "screenshots").mkdir(parents=True, exist_ok=True)
        (workdir / "downloads").mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless, slow_mo=self.slow_mo)
        record_dir = os.environ.get("OPSAGENT_RECORD_DIR")       # demo recording only; off by default
        kw = {"record_video_dir": record_dir, "record_video_size": {"width": 1100, "height": 800}} if record_dir else {}
        self._ctx = await self._browser.new_context(viewport={"width": 1100, "height": 800}, **kw)
        if record_dir:
            await self._ctx.add_init_script(_CURSOR_JS)   # a visible cursor, so the recording shows what is being clicked
        self.page = await self._ctx.new_page()

    async def close(self) -> None:
        if self._ctx:
            await self._ctx.close()          # also finalises a recorded video, which is lost if only the browser is closed
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # -- helpers ---------------------------------------------------------------
    def _check_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ToolFailure(ErrorCode.INVALID_INPUT, "only http(s) URLs are allowed")
        if parsed.hostname not in self.allowed_hosts:
            raise ToolFailure(ErrorCode.NOT_PERMITTED,
                              f"host {parsed.hostname!r} is not on the allowlist {list(self.allowed_hosts)}")
        return url

    async def snapshot(self, shot: bool = True) -> tuple[dict, str]:
        snap = await self.page.evaluate(_SNAPSHOT_JS)
        self.last_snapshot = snap
        path = ""
        if shot:
            self.shots += 1
            path = str(self.workdir / "screenshots" / f"{self.shots:03d}.png")
            await self.page.screenshot(path=path)
        return snap, path

    async def _result(self, extra: Optional[dict] = None) -> dict:
        snap, shot = await self.snapshot()
        out = {"snapshot": render_snapshot(snap), "url": snap["url"], "screenshot": shot}
        status = getattr(self, "_last_status", None)
        if status and status >= 400:
            out["http_status"] = status
        out.update(extra or {})
        return out

    async def _fresh(self) -> None:
        if self.last_snapshot is None:
            await self.snapshot(shot=False)

    def _selector(self, ref: int) -> str:
        return f'[data-opsref="{ref}"]'

    async def _el(self, ref: int):
        await self._fresh()
        el = await self.page.query_selector(self._selector(ref))
        if el is None:
            raise ToolFailure(ErrorCode.NOT_FOUND,
                              f"element [{ref}] does not exist on the current page; take a fresh browser_snapshot")
        return el


# ---------------------------------------------------------------- tool inputs
class GotoIn(BaseModel):
    url: str = Field(description="Absolute URL on an allowed host, e.g. http://localhost:8765/mail/")


class Empty(BaseModel):
    pass


class ClickIn(BaseModel):
    ref: int = Field(description="Element number from the latest snapshot")


class TypeIn(BaseModel):
    ref: int = Field(description="Input/textarea element number")
    text: str = Field(description="Text to type; may contain {{vault:name}} placeholders for credentials")


class SelectIn(BaseModel):
    ref: int = Field(description="Select element number")
    option: str = Field(description="Visible option text to choose")


class DownloadIn(BaseModel):
    ref: int = Field(description="Link element number whose target file should be downloaded")


def build_browser_tools(session: BrowserSession) -> list[ToolSpec]:
    async def goto(inp: GotoIn):
        url = session._check_url(inp.url)
        resp = await session.page.goto(url, wait_until="domcontentloaded")
        session._last_status = resp.status if resp else None
        return await session._result()

    async def snapshot(inp: Empty):
        session._last_status = None
        return await session._result()

    async def risk_of_click(inp: ClickIn) -> Risk:
        await session._fresh()
        info = await session.page.evaluate(_FORM_JS, inp.ref)
        if info is None:
            raise ToolFailure(ErrorCode.NOT_FOUND, f"element [{inp.ref}] does not exist; take a fresh browser_snapshot")
        return Risk.WRITE_HIGH if info["risk"] == "high" else Risk.WRITE_LOW

    async def preview_click(inp: ClickIn) -> dict:
        info = await session.page.evaluate(_FORM_JS, inp.ref)
        if info is None:
            raise ToolFailure(ErrorCode.NOT_FOUND, f"element [{inp.ref}] does not exist; take a fresh browser_snapshot")
        return {"page": session.page.url, "button": info["label"], "effect": info["effect"],
                "submits_to": info["action"], "fields": info["fields"]}

    async def click(inp: ClickIn):
        el = await session._el(inp.ref)
        session._last_status = None

        def on_resp(r):
            if r.request.is_navigation_request() and r.frame == session.page.main_frame:
                session._last_status = r.status

        session.page.on("response", on_resp)
        try:
            await el.click(timeout=5000)
            await session.page.wait_for_load_state("domcontentloaded", timeout=8000)
        finally:
            session.page.remove_listener("response", on_resp)
        return await session._result()

    async def type_(inp: TypeIn):
        el = await session._el(inp.ref)
        try:
            real = vault.resolve(inp.text)
        except vault.VaultError as exc:
            raise ToolFailure(ErrorCode.INVALID_INPUT, str(exc))
        await el.fill(real, timeout=5000)
        session._last_status = None
        shown = inp.text if vault.has_placeholder(inp.text) else inp.text[:60]
        return await session._result({"typed": shown})

    async def select(inp: SelectIn):
        el = await session._el(inp.ref)
        try:
            await el.select_option(label=inp.option, timeout=5000)
        except Exception:
            raise ToolFailure(ErrorCode.INVALID_INPUT, f"no option labelled {inp.option!r} in element [{inp.ref}]")
        session._last_status = None
        return await session._result({"selected": inp.option})

    async def download(inp: DownloadIn):
        el = await session._el(inp.ref)
        href = await el.get_attribute("href")
        if not href:
            raise ToolFailure(ErrorCode.INVALID_INPUT, f"element [{inp.ref}] is not a link")
        url = session._check_url(urljoin(session.page.url, href))
        resp = await session._ctx.request.get(url)  # shares the browser's cookies/session
        if resp.status >= 400 or "login" in resp.url:
            raise ToolFailure(ErrorCode.UPSTREAM, f"download failed (HTTP {resp.status}); you may need to sign in again")
        name = Path(urlparse(url).path).name or "download.bin"
        dest = session.workdir / "downloads" / name
        dest.write_bytes(await resp.body())
        return {"saved_to": f"downloads/{name}", "bytes": dest.stat().st_size}

    return [
        ToolSpec("browser_goto", "Open a URL in the browser and return a snapshot of the page.", GotoIn, goto,
                 Risk.READ, timeout_s=20, max_retries=1, idempotent=True),
        ToolSpec("browser_snapshot", "Re-read the current page: visible text and numbered interactive elements.",
                 Empty, snapshot, Risk.READ),
        ToolSpec("browser_click", "Click an element by its number. Clicking a button marked RISK:HIGH submits a "
                                  "write to a company system and needs approval.", ClickIn, click, Risk.WRITE_LOW,
                 risk_fn=risk_of_click, preview=preview_click, timeout_s=20),
        ToolSpec("browser_type", "Type into an input by its number (replaces content). Use {{vault:name}} for credentials.",
                 TypeIn, type_, Risk.WRITE_LOW),
        ToolSpec("browser_select", "Choose an option (by visible text) in a select element.", SelectIn, select, Risk.WRITE_LOW),
        ToolSpec("browser_download", "Download the file a link points to into the run workspace; returns its path "
                                     "for read_pdf.", DownloadIn, download, Risk.READ, timeout_s=30),
    ]
