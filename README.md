# Lead Triage Agent — Beat Claude 012

Submission for Single Grain's [Beat Claude](https://github.com/ericosiu/beat-claude) challenge **AI Automation Intern 012** (brief 2026-07), by Preston Crozier, built with Claude under his direction (full disclosure in SUBMISSION.md §5).

A deterministic first-pass triage agent for inbound leads. Every lead gets one of `QUALIFY / NURTURE / REJECT / ESCALATE`, a reason, a confidence, and a full per-row log. Lead text is treated as data, never as instructions.

## Run it

```bash
python3 triage.py            # writes out/decisions.csv and out/run_log.jsonl
python3 compare.py           # agent vs. manual pass (18/20 agree)
python3 compare.py out/v1/decisions_v1.csv   # the first run, with the bug (17/20)
sha256sum fixtures/inbound_leads.csv         # cc1927ca…c8599
```

Python 3.10+, standard library only, no network, no keys.

## What's here

| Path | What |
|---|---|
| `SUBMISSION.md` | The written answer (brief version, traps found, the bad output, what breaks next, meta question, evidence log, AI disclosure) |
| `triage.py` | The agent |
| `fixtures/inbound_leads.csv` | The challenge fixture, unmodified |
| `out/decisions.csv`, `out/run_log.jsonl` | Current run (policy v2) |
| `out/v1/` | First run, kept as evidence of the L-015 miss |
| `manual_pass.csv`, `compare.py` | Decisions made before coding, and the comparison |

## The one-paragraph version

Safety and sensitivity rules run first (privacy request, phishing link, garbage, instruction injection), then identity rules (duplicate, conflicting story, disposable or missing contact), then budget fit. Six rows escalate, each with a reason a human can act on in a minute. The first version wrongly qualified L-015 by merging on email alone; v2 merges only onto a live record with a matching company. Details in `SUBMISSION.md`.
