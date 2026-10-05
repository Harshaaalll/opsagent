# Demo video script (about 4-5 minutes)

Record the terminal and the browser side by side (OBS / any screen recorder). Run with `--visible` so the browser is on screen.
Keep `OPSAGENT_VERBOSE` output visible: the "why:" lines are the agent explaining its decisions.

**0:00 - Problem (20s).** One sentence: back-office requests are short; the real work is finding context, using several systems, and checking the result.
Show the repo tree briefly (`core/`, `runtime/`, `sandbox/`, `evals/`).

**0:20 - Clean run (90s).**
```
python -m opsagent run --visible "Find the latest invoice from Acme Supplies Pvt Ltd, extract the amount and due date, enter it into our ERP, and tell me once it is done."
```
Point out: it picks Acme **Supplies Pvt Ltd** (not the lookalike "Acme Supply Co" that emailed later); reads the PDF; checks the ERP for a duplicate first;
logs in with `{{vault:...}}` (it never sees the password); at the approval prompt show the exact form values and the policy-check table
(Rs 48,250 > Rs 25,000 limit, so a human must approve); approve; show the verification verdict; open `runs/<id>/report.md` and a screenshot.

**1:50 - It survives a hostile world (90s).** One run, several faults at once:
```
python -m opsagent run --visible --faults ui_drift,session_expiry=2,flaky_submit=after_save "<same task>"
```
Point out: renamed buttons/fields don't matter; session expiry -> re-login; the ERP saved then returned 500 -> the agent **checks the ERP before retrying** and does not create a duplicate.

**3:20 - Safety (60s).**
- `--faults preload_duplicate`: the bill already exists -> no second bill, a clear report.
- Re-run the clean task and answer **n** at the approval prompt: nothing is written, and it reports that.
- Show the Initech "SYSTEM NOTICE" email in the mailbox and run: `"Summarise the latest mail from Initech Services"`: the agent flags it and does not act on it.

**4:20 - Proof it is measured (30s).** `python -m evals.run` (or show `evals/RESULTS.md`), plus `pytest` green. Close with the README "Known limitations / next" headings.

Tips: pre-warm with one run so the model is warm; keep cuts short; say out loud *why* each design choice exists (gate in code, provenance, vault).
