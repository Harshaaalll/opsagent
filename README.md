# OpsAgent: an autonomous AI operator that finishes back-office work and proves it

> *"Find the latest invoice from Acme Supplies, extract the amount and due date, enter it into our ERP, and tell me once it is done."*

OpsAgent takes a short company request, works out the missing steps from company knowledge, operates a **real browser** to do the work,
recovers when the environment misbehaves, asks a person when it must, and **only reports "done" after independently re-reading the
system and checking the values against the source documents**. Built for CentrAlign AI's *AI Employee / Autonomous Company Operator* problem
(Founding Engineer track).

Everything it touches is a **local mock company** (vendor mail portal + ERP) with PDF invoices and switchable faults. No real credentials, no third-party systems.

## What happens on a run

```
 request ──► retrieve company policy ──► PLAN ──► ACT ──► OBSERVE ──► ADAPT ─┐
                                                   ▲                         │
                                                   └─────────────────────────┘
                                         finish(claims) ──► VERIFY (independent) ──► report + evidence
                                                              │ fail: back to work with specific feedback
                         high-risk click ──► POLICY GATE (code) ──► auto │ human approval │ block
```

Concretely, for the invoice task the agent: signs into the mail portal with vault credentials it never sees, finds the *latest invoice
from that exact vendor* (not a lookalike vendor, a reminder, or a credit note), downloads and reads the PDF, checks the ERP for an existing
bill, signs into the ERP, fills the form, hits the approval gate, saves, then proves the bill exists with the right values.

## Quick start

```bash
git clone https://github.com/Harshaaalll/opsagent && cd opsagent
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium-headless-shell      # or: playwright install chromium (needed for --visible)
cp .env.example .env                            # then put your GEMINI_API_KEY in .env

python -m opsagent run "Find the latest invoice from Acme Supplies Pvt Ltd, extract the amount and due date, enter it into our ERP, and tell me once it is done."
```

The sandbox starts automatically on `localhost:8765` (or run it yourself with `python -m opsagent sandbox`). The terminal shows each step *and the
agent's stated reason*; when the write is high-risk you get an approval prompt listing the exact form values and every policy check.

| Flag | Meaning |
|---|---|
| `--visible` | watch the browser work |
| `--faults ui_drift,session_expiry=2,flaky_submit=after_save` | make the world hostile (also: `preload_duplicate`, `mail_amount_mismatch`, `flaky_submit=before_save`) |
| `--model gemini-2.5-pro` | choose the model |

Every run writes `runs/<id>/`: `report.md` (summary, verification verdicts, approvals, each step with its reason), `steps.jsonl`, `audit.jsonl`
(redacted tool-call log), and `screenshots/` (visual evidence).

```bash
pytest                              # 23 tests, no API key needed (scripted LLM drives the real browser + sandbox)
python -m evals.run                 # 12 scenarios against the real Gemini agent -> evals/RESULTS.md
python -m evals.run --only s04_flaky_after_save --repeat 3
```

## Architecture

```
opsagent/
  core/      contracts.py  tool contracts, risk ladder (read / write_low / write_high), per-call risk_fn + preview
             registry.py   THE one door to the outside world: permissions, input contract, risk, approval digest, breaker, retries, audit
             breaker.py    circuit breaker per tool        budget.py   stop reasons, stall detection, cost meter
             knowledge.py  clause-level BM25 + aliases      vault.py    {{vault:...}} placeholders      redact.py  audit redaction
  tools/     browser.py    Playwright; DOM -> text snapshot with numbered elements   system.py  read_pdf, erp_find_bills, search_company_knowledge
  runtime/   agent.py      the loop + approval handling + records      gate.py   deterministic policy gate + approvers
             context.py    shaping / durable facts / compaction        verifier.py   independent verification + provenance
             prompt.py     system prompt + control actions (plan, remember, ask_human, finish)
  llm/       gemini.py     function-calling, retry, fallback model     scripted.py   deterministic LLM for tests
sandbox/     FastAPI mock company: mail portal, ERP (UI + read API), PDF invoices, fault injection
company/     policies/*.md (human-readable procedures) + policy.yaml (machine-enforced thresholds)
evals/       scenarios, trace-level grader, runner        tests/   23 tests
```

### The core ideas (and why)

1. **The model proposes; code decides.** The LLM only ever emits one function call per step. Everything with consequences passes through
   `ToolRegistry.call`. Risk is decided *per call from the live page* (a click on a link is a read; a click on "Save bill" is a financial write).
2. **Approval is bound to the exact effect.** A high-risk call computes a *preview* (the form values about to be submitted). The approval
   carries a digest of tool + args + preview, and the registry **recomputes it at execution time**, so an approval cannot be replayed onto a
   different page state (`STALE_APPROVAL`). The policy gate runs explicit checks (trusted vendor, amount limit, currency, dates, **duplicate
   found through the ERP API, a second channel**) and the human sees every check. Thresholds live in `company/policy.yaml`, not in code or the prompt.
3. **"Done" is a request, not a statement.** `finish(completed)` must carry *claims*: a read tool plus expected values. The verifier (no LLM)
   re-reads the world and compares, **and** requires each value to have been read from a source document. A value the agent merely typed cannot verify
   itself. (An early version of this had a bug where the verifier's own feedback leaked into its evidence; there is a regression test for it.)
4. **Generic browser, not a script.** Observations are text snapshots with numbered elements and their labels; the agent acts on labels, so renamed buttons
   and reordered forms (`ui_drift`) don't break it. Nothing in the loop knows about invoices; the procedure lives in retrievable company knowledge.
5. **Reliability is designed, not hoped for.** Budgets and stall detection are checked *before* each step; reads retry, non-idempotent writes never auto-retry
   (and the agent is told to check the ERP before retrying a failed save); a circuit breaker stops hammering a dead tool; the LLM client retries then falls back to a smaller model.
6. **Context is managed as a budget.** Only the newest page snapshot is verbatim; older steps compact to one line; durable facts (PDF text, ERP lookups, `remember`) are pinned.
7. **Memory has two layers.** Company knowledge (policy clauses, retrieved by BM25 with alias expansion) and lessons the agent writes after verified runs (`company/lessons.jsonl`), retrieved the same way.
8. **Security.** Credentials are placeholders resolved inside the tool layer (the model never sees them, they never reach logs); navigation is allow-listed; all page/email/PDF text is treated as data
   (the sandbox mailbox contains a prompt-injection email, and eval `s10` checks it is ignored).

## Models, frameworks and components used

- **LLM:** Google Gemini via the `google-genai` SDK (default `gemini-2.5-flash`, fallback `gemini-2.5-flash-lite`), native function calling in `ANY` mode.
- **Browser:** Playwright (Chromium). **Sandbox:** FastAPI + uvicorn. **PDFs:** reportlab (generate), pypdf (read). **Validation:** pydantic. **Tests:** pytest, pytest-asyncio.
- **No agent framework** (no LangChain/LangGraph): the loop is ~300 lines I can walk through line by line.
- **Pre-built code disclosure:** `core/contracts.py`, `core/breaker.py`, `core/registry.py`, `core/budget.py`, `runtime/context.py` and `core/knowledge.py` are **ported and adapted from my own earlier project
  [`sanwaad`](https://github.com/Harshaaalll/sanwaad)** (tool registry with risk ladder, circuit breaker, loop budgets, context compaction, clause-level retrieval). Adapted here for browser work:
  per-call `risk_fn`, effect `preview` + digest re-validation, provenance verification, and the browser/sandbox/evals are new for this project.
- **AI coding tools:** this project was built with **Claude Code (Anthropic)** assisting with implementation. I reviewed the design and code and can explain and modify it.

## Assumptions

- The task environment is a sandbox I built (mail portal + ERP). The browser layer is generic, but I have only validated it on these apps.
- The PDF is the authoritative source for invoice data (company policy `AP-01`); email text is secondary (`AP-06`).
- Approvals are given by a person at the terminal; in evals a scripted approver stands in.
- Currency is INR; the ERP is the system of record; the ERP exposes a read-only API for lookups.

## Known limitations (honest list)

- **Validated on one domain.** Two systems, one workflow family (plus read-only and different-vendor variants). Generalisation is demonstrated, not proven.
- **DOM-text perception only.** No vision/desktop-app control; canvas-heavy or heavily dynamic apps would need a screenshot-based fallback.
- **Retrieval is BM25 + hand-written aliases.** No dense embeddings; vocabulary the aliases don't cover can be missed.
- **Verification is only as good as the claims it can express:** a read tool + expected values. The provenance check is a string/number/date match, not semantic.
- **Claims are passed as JSON strings** (to keep the function schema flat for the model); a malformed claim costs a verifier retry.
- **Single process, no durable queue:** a crash loses the run (the record exists, but it can't resume). No concurrency, scheduling, or multi-tenant isolation.
- **Approvals are CLI-only**; no web console/Slack, no approver roles.
- **LLM variance:** results differ run to run; `evals.run --repeat N` exists to measure it. See `evals/RESULTS.md` for the numbers I actually obtained.
- **Lessons memory has no review step**; a bad lesson could persist (they're plain JSONL and easy to audit).

## What I would build next (2 weeks)

1. **Durable execution:** checkpoint every step, resume after a crash, background queue + scheduling + retries with backoff.
2. **Vision fallback / desktop control:** screenshot-grounded actions when the DOM is not enough; keep the same registry/risk/approval path.
3. **Connector abstraction:** declare a new system (login flow, tools, risk labels, verification reads) in config; expose tools over MCP (`ToolSpec.as_mcp_tool` is already there).
4. **Earned autonomy:** move a task type from "always ask" to "auto-approve" only after measured agreement with human reviewers (a pattern from `sanwaad`), with automatic demotion.
5. **Approval console** (web/Slack) with roles, expiry and digests; richer policy language.
6. **Dense retrieval + lesson curation;** broader eval set (50+ scenarios, adversarial pages, multi-app tasks) and regression gating in CI.
7. **Voice input** (reuse my Indian-language STT work in `stt-tts-archive`) for hands-free task requests.

## Repository hygiene

`.env` is gitignored; `.env.example` contains only mock-sandbox credentials. `runs/` and generated invoices are gitignored.
