"""Mock company environment: a mail/vendor portal and an ERP, in one FastAPI app.

Everything here is fake and local. It exists so the agent has a real browser
target with realistic ways to go wrong. Faults are switched on per scenario via
POST /__admin__/reset, so the SAME agent code can be evaluated against a clean
world and against a hostile one.

Faults (all off by default):
  session_expiry      ERP session dies after N authenticated requests
  ui_drift            labels / button text change (selectors would break; labels-based agents survive)
  flaky_submit        "before_save": first bill POST returns 500 without saving
                      "after_save" : first bill POST SAVES, then returns 500 (naive retry = duplicate)
  preload_duplicate   the latest Acme invoice is already in the ERP
  mail_amount_mismatch the email body states a different amount than the attached PDF
"""

from __future__ import annotations

import copy
import html
import secrets
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .seed import EMAILS, ERP_VENDORS, INITIAL_BILLS, make_invoice_pdfs

BASE = Path(__file__).parent
INVOICE_DIR = BASE / "invoices"
ERP_API_KEY = "erp-api-key-demo"
CREDS = {"erp": ("ap.clerk", "erp-demo-pass"), "mail": ("ap@northwind.test", "mail-demo-pass")}

app = FastAPI(title="Northwind sandbox")

STATE: dict = {}


def reset(faults: Optional[dict] = None) -> None:
    f = {"session_expiry": 0, "ui_drift": False, "flaky_submit": "none",
         "preload_duplicate": False, "mail_amount_mismatch": False}
    f.update(faults or {})
    bills = copy.deepcopy(INITIAL_BILLS)
    if f["preload_duplicate"]:
        bills.append({"id": len(bills) + 1, "vendor": "Acme Supplies Pvt Ltd", "invoice_number": "INV-ACM-0912",
                      "invoice_date": "2026-09-28", "due_date": "2026-10-28", "currency": "INR",
                      "amount": 48250.00, "notes": "entered by AP (earlier)", "status": "Open"})
    STATE.clear()
    STATE.update(faults=f, bills=bills, sessions={}, post_count=0, log=[])
    make_invoice_pdfs(INVOICE_DIR)


reset()


def label(key: str) -> str:
    drift = STATE["faults"]["ui_drift"]
    table = {
        "submit": ("Save bill", "Create entry"),
        "invoice_number": ("Invoice number", "Supplier ref"),
        "amount": ("Amount (incl. tax)", "Total payable"),
        "nav_new": ("New bill", "Add payable"),
        "nav_bills": ("Bills", "Payables"),
    }
    return table[key][1 if drift else 0]


def page(title: str, body: str, nav: str = "") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui;margin:0;background:#f5f6f8}}header{{background:#1f2a44;color:#fff;padding:12px 24px}}
header a{{color:#cfe0ff;margin-right:16px}}main{{max-width:880px;margin:24px auto;background:#fff;padding:24px;border-radius:8px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:8px;text-align:left}}
label{{display:block;margin:10px 0 4px}}input,select,textarea{{padding:6px;width:320px}}button{{padding:8px 16px;margin-top:14px}}
.err{{background:#fde8e8;color:#9b1c1c;padding:10px;border-radius:6px}}.ok{{background:#e6f6ea;color:#14532d;padding:10px;border-radius:6px}}</style>
</head><body><header><strong>{html.escape(title)}</strong> &nbsp; {nav}</header><main>{body}</main></body></html>""")


# ---------------------------------------------------------------- admin
@app.post("/__admin__/reset")
async def admin_reset(request: Request):
    body = await request.json() if (await request.body()) else {}
    reset(body.get("faults"))
    return {"ok": True, "faults": STATE["faults"]}


@app.get("/__admin__/state")
def admin_state():
    return {"faults": STATE["faults"], "bills": STATE["bills"], "log": STATE["log"][-30:]}


@app.get("/", response_class=HTMLResponse)
def index():
    return page("Northwind intranet", "<h2>Apps</h2><ul><li><a href='/mail/'>Vendor mail portal</a></li>"
                                      "<li><a href='/erp/'>Northwind ERP</a></li></ul>")


# ---------------------------------------------------------------- sessions
def _login_page(app_name: str, error: str = "", next_url: str = "") -> HTMLResponse:
    err = f"<div class='err'>{html.escape(error)}</div>" if error else ""
    return page(f"{app_name} - Sign in", f"""{err}<form method="post" action="/{app_name.lower().split()[0]}/login">
<input type="hidden" name="next" value="{html.escape(next_url)}">
<label for="u">Username</label><input id="u" name="username" autocomplete="off">
<label for="p">Password</label><input id="p" name="password" type="password">
<button type="submit">Sign in</button></form>""")


def _session_ok(request: Request, app_name: str) -> bool:
    tok = request.cookies.get(f"{app_name}_session")
    sess = STATE["sessions"].get(tok or "")
    if not sess or sess["app"] != app_name:
        return False
    ttl = STATE["faults"]["session_expiry"]
    if app_name == "erp" and ttl:
        sess["used"] += 1
        if sess["used"] > ttl:
            STATE["sessions"].pop(tok, None)
            STATE["log"].append("erp session expired (fault)")
            return False
    return True


def _do_login(app_name: str, username: str, password: str, next_url: str):
    if (username, password) != CREDS[app_name]:
        STATE["log"].append(f"{app_name} login failed")
        return _login_page(app_name.upper() if app_name == "erp" else "Mail", "Invalid username or password", next_url)
    tok = secrets.token_hex(12)
    STATE["sessions"][tok] = {"app": app_name, "used": 0}
    dest = next_url if next_url.startswith(f"/{app_name}/") else f"/{app_name}/"
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(f"{app_name}_session", tok, httponly=True)
    STATE["log"].append(f"{app_name} login ok")
    return resp


# ---------------------------------------------------------------- mail portal
def _mail_nav() -> str:
    return "<a href='/mail/'>Inbox</a>"


def _email_view(e: dict) -> dict:
    e = dict(e)
    if STATE["faults"]["mail_amount_mismatch"] and e["id"] == "m-acme-0912":
        e["body"] = e["body"].replace("48,250.00", "46,250.00")
    return e


@app.get("/mail/login", response_class=HTMLResponse)
def mail_login_get(next: str = ""):
    return _login_page("Mail portal", next_url=next)


@app.post("/mail/login")
def mail_login_post(username: str = Form(""), password: str = Form(""), next: str = Form("")):
    return _do_login("mail", username, password, next)


@app.get("/mail/", response_class=HTMLResponse)
def mail_inbox(request: Request, q: str = ""):
    if not _session_ok(request, "mail"):
        return RedirectResponse("/mail/login?next=/mail/", status_code=303)
    rows = ""
    for e in sorted(EMAILS, key=lambda m: m["date"], reverse=True):
        if q and q.lower() not in (e["from_name"] + e["subject"] + e["body"]).lower():
            continue
        rows += (f"<tr><td>{e['date']}</td><td>{html.escape(e['from_name'])}</td>"
                 f"<td><a href='/mail/message/{e['id']}'>{html.escape(e['subject'])}</a></td>"
                 f"<td>{'attachment' if e.get('attachment') else ''}</td></tr>")
    body = f"""<form method="get" action="/mail/"><label for="q">Search mail</label>
<input id="q" name="q" value="{html.escape(q)}"><button type="submit">Search</button></form>
<table><tr><th>Date</th><th>From</th><th>Subject</th><th></th></tr>{rows}</table>"""
    return page("Vendor mail portal", body, _mail_nav())


@app.get("/mail/message/{mid}", response_class=HTMLResponse)
def mail_message(request: Request, mid: str):
    if not _session_ok(request, "mail"):
        return RedirectResponse(f"/mail/login?next=/mail/message/{mid}", status_code=303)
    e = next((m for m in EMAILS if m["id"] == mid), None)
    if not e:
        raise HTTPException(404, "message not found")
    e = _email_view(e)
    att = (f"<p>Attachment: <a href='/mail/files/{e['attachment']}'>{html.escape(e['attachment'])}</a></p>"
           if e.get("attachment") else "")
    body = (f"<h2>{html.escape(e['subject'])}</h2><p>From: {html.escape(e['from_name'])} "
            f"&lt;{html.escape(e['from_addr'])}&gt;<br>Date: {e['date']}</p>"
            f"<pre style='white-space:pre-wrap'>{html.escape(e['body'])}</pre>{att}")
    return page("Vendor mail portal", body, _mail_nav())


@app.get("/mail/files/{name}")
def mail_file(request: Request, name: str):
    if not _session_ok(request, "mail"):
        return RedirectResponse("/mail/login", status_code=303)
    path = INVOICE_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf", filename=path.name)


# ---------------------------------------------------------------- ERP
def _erp_nav() -> str:
    return f"<a href='/erp/bills'>{label('nav_bills')}</a><a href='/erp/bills/new'>{label('nav_new')}</a>"


@app.get("/erp/login", response_class=HTMLResponse)
def erp_login_get(next: str = "", expired: int = 0):
    return _login_page("ERP", "Your session has expired. Please sign in again." if expired else "", next)


@app.post("/erp/login")
def erp_login_post(username: str = Form(""), password: str = Form(""), next: str = Form("")):
    return _do_login("erp", username, password, next)


@app.get("/erp/", response_class=HTMLResponse)
def erp_home(request: Request):
    return RedirectResponse("/erp/bills", status_code=303)


def _erp_guard(request: Request):
    if not _session_ok(request, "erp"):
        return RedirectResponse(f"/erp/login?expired=1&next={request.url.path}", status_code=303)
    return None


@app.get("/erp/bills", response_class=HTMLResponse)
def erp_bills(request: Request):
    if (r := _erp_guard(request)):
        return r
    rows = "".join(
        f"<tr><td>{b['vendor']}</td><td>{b['invoice_number']}</td><td>{b['invoice_date']}</td>"
        f"<td>{b['due_date']}</td><td>{b['currency']} {b['amount']:,.2f}</td><td>{b['status']}</td></tr>"
        for b in STATE["bills"])
    flash = request.query_params.get("saved")
    msg = f"<div class='ok'>Saved: {html.escape(flash)}</div>" if flash else ""
    return page("Northwind ERP", f"{msg}<h2>{label('nav_bills')}</h2><table><tr><th>Vendor</th>"
                f"<th>{label('invoice_number')}</th><th>Invoice date</th><th>Due date</th><th>Amount</th>"
                f"<th>Status</th></tr>{rows}</table>", _erp_nav())


def _bill_form(values: Optional[dict] = None, error: str = "") -> HTMLResponse:
    v = values or {}
    opts = "".join(f"<option value='{html.escape(x)}'{' selected' if v.get('vendor') == x else ''}>{html.escape(x)}</option>"
                   for x in ERP_VENDORS)
    err = f"<div class='err' role='alert'>{html.escape(error)}</div>" if error else ""
    fields = {
        "vendor": f"<label for='vendor'>Vendor</label><select id='vendor' name='vendor'><option value=''>-- select --</option>{opts}</select>",
        "invoice_number": f"<label for='inv'>{label('invoice_number')}</label><input id='inv' name='invoice_number' value='{html.escape(v.get('invoice_number', ''))}'>",
        "invoice_date": f"<label for='idate'>Invoice date</label><input id='idate' name='invoice_date' type='date' value='{v.get('invoice_date', '')}'>",
        "due_date": f"<label for='ddate'>Due date</label><input id='ddate' name='due_date' type='date' value='{v.get('due_date', '')}'>",
        "currency": f"<label for='cur'>Currency</label><select id='cur' name='currency'><option>INR</option><option>USD</option></select>",
        "amount": f"<label for='amt'>{label('amount')}</label><input id='amt' name='amount' inputmode='decimal' value='{v.get('amount', '')}'>",
        "notes": f"<label for='notes'>Notes</label><textarea id='notes' name='notes'>{html.escape(v.get('notes', ''))}</textarea>",
    }
    order = ["vendor", "invoice_number", "invoice_date", "due_date", "currency", "amount", "notes"]
    if STATE["faults"]["ui_drift"]:
        order = ["invoice_number", "vendor", "amount", "currency", "due_date", "invoice_date", "notes"]
    body = (f"<h2>{label('nav_new')}</h2>{err}<form method='post' action='/erp/bills/new'>"
            + "".join(fields[k] for k in order)
            + f"<button type='submit' data-risk='high' data-effect='creates a payable in the ERP'>{label('submit')}</button>"
              "<a href='/erp/bills' style='margin-left:12px'>Cancel</a></form>")
    return page("Northwind ERP", body, _erp_nav())


@app.get("/erp/bills/new", response_class=HTMLResponse)
def erp_new_get(request: Request):
    if (r := _erp_guard(request)):
        return r
    return _bill_form()


@app.post("/erp/bills/new")
async def erp_new_post(request: Request):
    if (r := _erp_guard(request)):
        return r
    form = dict((await request.form()).items())
    STATE["post_count"] += 1
    mode = STATE["faults"]["flaky_submit"]
    first = STATE["post_count"] == 1
    if mode == "before_save" and first:
        STATE["log"].append("flaky: 500 before save")
        return HTMLResponse("<h1>500 Internal Server Error</h1><p>Upstream database timeout. Try again.</p>", status_code=500)
    err = _validate_bill(form)
    if err:
        return _bill_form(form, err)
    bill = {"id": len(STATE["bills"]) + 1, "vendor": form["vendor"], "invoice_number": form["invoice_number"].strip(),
            "invoice_date": form["invoice_date"], "due_date": form["due_date"], "currency": form.get("currency", "INR"),
            "amount": round(float(form["amount"].replace(",", "")), 2), "notes": form.get("notes", ""), "status": "Open"}
    STATE["bills"].append(bill)
    STATE["log"].append(f"bill saved {bill['invoice_number']}")
    if mode == "after_save" and first:
        STATE["log"].append("flaky: 500 AFTER save")
        return HTMLResponse("<h1>500 Internal Server Error</h1><p>Gateway timeout while confirming. Try again.</p>", status_code=500)
    return RedirectResponse(f"/erp/bills?saved={bill['invoice_number']}", status_code=303)


def _validate_bill(f: dict) -> str:
    for k, name in (("vendor", "Vendor"), ("invoice_number", label("invoice_number")), ("invoice_date", "Invoice date"),
                    ("due_date", "Due date"), ("amount", label("amount"))):
        if not (f.get(k) or "").strip():
            return f"{name} is required."
    try:
        amt = float(f["amount"].replace(",", ""))
        if amt <= 0:
            raise ValueError
    except ValueError:
        return "Amount must be a positive number."
    try:
        if date.fromisoformat(f["due_date"]) < date.fromisoformat(f["invoice_date"]):
            return "Due date cannot be before the invoice date."
    except ValueError:
        return "Dates must be valid (YYYY-MM-DD)."
    if any(b["vendor"] == f["vendor"] and b["invoice_number"].lower() == f["invoice_number"].strip().lower()
           for b in STATE["bills"]):
        return "Duplicate invoice: this vendor already has a bill with that number."
    return ""


# ---------------------------------------------------------------- ERP REST API (used by tools + verifier)
@app.get("/api/erp/bills")
def api_bills(x_api_key: str = Header(""), invoice_number: str = "", vendor: str = ""):
    if x_api_key != ERP_API_KEY:
        return JSONResponse({"error": "invalid api key"}, status_code=401)
    out = [b for b in STATE["bills"]
           if (not invoice_number or b["invoice_number"].lower() == invoice_number.lower())
           and (not vendor or vendor.lower() in b["vendor"].lower())]
    return {"count": len(out), "bills": out}
