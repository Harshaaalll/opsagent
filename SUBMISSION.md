# Submission notes: draft answers for the CentrAlign Google Form

> Drafts, written from what is actually built. **Fill the `[...]` placeholders from your real eval run and re-read every answer:
> the form says you must be able to explain, debug and modify all of it.** Never put passwords in the form.

**Role:** Founding Engineer

**Project: Github Repository link:** `https://github.com/Harshaaalll/opsagent`
**Live demo link:** none (runs locally; sandbox included)
**Demo Video Link:** `[paste after recording; see DEMO_SCRIPT.md]`

---

### In 3-5 sentences, what did you build?
OpsAgent is an autonomous AI operator that turns a short company request ("find the latest invoice from Acme, enter it in our ERP, tell me when done") into completed, verified work.
It drives a real Chromium browser against a mock company (vendor mail portal + ERP with PDF invoices), retrieves the company's procedures and policies to fill in what the request leaves unsaid,
and runs a plan-act-observe-adapt loop on Gemini function calling. High-risk writes go through a deterministic approval gate bound to the exact form values, and the agent can only finish after an
independent verifier re-reads the ERP and confirms each value came from the source PDF. Faults (expired sessions, UI changes, a server that saves then errors, duplicate invoices, conflicting sources, a prompt-injection email) are injectable, and an eval harness grades both outcomes and the path taken.

### Briefly explain your architecture
Goal -> retrieve company knowledge (BM25 over policy clauses + lessons from past runs) -> loop: the LLM emits exactly one function call per step (a registry tool or a control action: plan / remember / ask_human / finish).
Every tool call passes through one registry door: permission, input contract, per-call risk (decided from live page state), circuit breaker, retries only where safe, redacted audit.
High-risk calls compute a preview of the exact effect; a policy gate (code + `policy.yaml`) returns auto / human / block with an explicit checklist; the approval is bound to a digest that is re-validated at execution time.
`finish(completed)` requires claims (read-tool + expected values); a verifier with no LLM re-reads the system and requires provenance from a source document. Budgets, stall detection and context compaction bound every run.
Browser observations are text snapshots with numbered, labelled elements (not selectors), so UI drift doesn't break it.

### What parts of your system are genuinely autonomous?
Choosing and sequencing actions (navigation, finding the right email by vendor/date, reading the PDF, filling the form); deciding it needs to check for duplicates; recovering from an expired session by re-authenticating via the vault;
checking the ERP before retrying a failed save; deciding when sources conflict and a person must be asked; revising its plan; writing a lesson for future runs; deciding when it believes it is done and what evidence to offer.
None of this is scripted per task: the loop contains no invoice-specific logic. `[after evals: state the pass rate per scenario and what failed]`.

### What is currently hard coded or manually configured?
The sandbox apps and their data (and the fault toggles); the approval thresholds and trusted-vendor list (`company/policy.yaml`); the company procedures (markdown clauses I wrote); the allowed-hosts list;
which form-label substrings the gate uses to find vendor/amount/dates; which URLs count as the "system of record" excluded from provenance (`/erp/`); the vault entry names; the system prompt and the four control actions.
The gate's field matching is label-based, so a new system with different labels needs its patterns added.

### What models, frameworks, APIs, libraries, AI coding tools, or existing projects did you use?
Gemini (`gemini-2.5-flash`, fallback `-flash-lite`) via google-genai; Playwright; FastAPI/uvicorn; pydantic; pypdf; reportlab; httpx; PyYAML; pytest. No agent framework.
**Existing project:** several core modules (tool registry/contracts, circuit breaker, loop budgets, context compaction, clause retrieval) are ported and adapted from my own earlier repo `sanwaad`; disclosed in the README.
**AI coding tool:** Claude Code (Anthropic) assisted implementation; I reviewed the design/code.

### What is the biggest technical limitation of your current solution?
Perception and generalisation breadth: it reads pages as DOM text, so it cannot operate desktop apps or canvas-heavy UIs, and it has only been validated on two mock web apps. Closely related, verification is limited to what can be expressed as
"a read tool returns these values" with string/number/date provenance matching, which is strong for structured records but weak for semantic outcomes.

### If you had another 2 weeks, what would you build/change next?
Durable execution (checkpoint/resume, queue, scheduling); a screenshot-grounded fallback for non-DOM apps under the same risk/approval path; a connector abstraction so a new system is configuration (login flow, tools, risk labels, verification reads) and exposed over MCP;
"earned autonomy" so approval requirements relax only on measured human agreement; a web/Slack approval console with roles; dense retrieval + lesson curation; and a 50+ scenario eval suite in CI.

### Final Declaration
Tick only what is true for you. Disclosed: pre-built components (ported `sanwaad` modules, README) and AI coding tools (Claude Code, README). No confidential data or third-party access (mock sandbox only).
