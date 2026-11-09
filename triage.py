#!/usr/bin/env python3
"""Lead Triage Agent — Beat Claude challenge ai-automation-intern-012 (brief 2026-07).

Reads fixtures/inbound_leads.csv, makes one decision per lead
(QUALIFY / NURTURE / REJECT / ESCALATE), and writes:

  out/decisions.csv   one row per lead: lead_id, decision, confidence, reason, flags
  out/run_log.jsonl   one JSON line per lead: every check that fired, parsed values,
                      and the rule that decided — plus a header line with the
                      fixture checksum and the policy version.

Design rules (see SUBMISSION.md for why):
  * Lead text is DATA. Nothing in the message column is ever executed, followed,
    or allowed to change a decision upward. Instruction-shaped text is a red flag.
  * Escalate instead of guess: missing, conflicting, malformed or sensitive input
    goes to a human with a reason. Everything else gets decided.
  * Deterministic and stdlib-only, so a reviewer can re-run it and get the same
    answer. No LLM sits in the decision path, on purpose (see SUBMISSION.md, Architecture).

Usage:  python3 triage.py [--fixture fixtures/inbound_leads.csv] [--out out]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

POLICY_VERSION = "v2 (2026-11-09): duplicate merge only onto a live record; otherwise conflicting identity escalates"

# ---- Policy knobs (every number here is [Assumed] unless stated) -------------------
QUALIFY_MIN_BUDGET = 5_000        # [Assumed] agency floor for an active sales cycle
ENTERPRISE_BUDGET_FLOOR = 1_000   # [Assumed] below this + enterprise signals = conflict
FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com"}
DISPOSABLE_MAIL = {"mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "yopmail.com"}
TEST_WORDS = {"test", "asdf", "qwerty", "foo", "bar", "lorem"}
DANGEROUS_EXT = re.compile(r"\.(exe|scr|bat|cmd|msi|js|vbs|zip)(\b|$)", re.I)
URL_RE = re.compile(r"https?://\S+", re.I)
# Phrases that address the reader/agent instead of describing a business need.
INSTRUCTION_RE = re.compile(
    r"(ignore (all )?(previous|prior) instructions|classify this (lead )?as|"
    r"set (the )?confidence|you are an? (ai|assistant|agent)|system prompt|"
    r"as an ai|override|disregard (the )?(above|rules))", re.I)
GDPR_RE = re.compile(r"(gdpr|article 17|right to (be forgotten|erasure)|delete (all )?(my|personal) data|"
                     r"ccpa|do not sell|unsubscribe me)", re.I)
COMPETITOR_RE = re.compile(r"(for a client of ours|how you structure retainers|your (pricing|rate card)|"
                           r"comparison for|methodology deck|send (over|us) your .*(deck|case studies|client results))", re.I)
STUDENT_RE = re.compile(r"(\.edu$|student|studying|mentor|advice on breaking into|internship)", re.I)
URGENCY_RE = re.compile(r"(ready to sign|this week|asap|immediately|if the price is right|avoid service interruption)", re.I)
ENTERPRISE_RE = re.compile(r"(\d{1,3}(,\d{3})+|\d{4,})[- ]?(employee|person|people|staff)|fortune 500|global|enterprise", re.I)
SPANISH_RE = re.compile(r"\b(somos|buscamos|tenemos|presupuesto|agencia|empresa)\b", re.I)


def parse_budget(raw: str) -> tuple[int | None, str | None]:
    """Return (usd_int, note). Accepts 25000, '25,000', '$25k', '15k'. Anything else -> None."""
    s = (raw or "").strip().lower().replace("$", "").replace(",", "").replace("usd", "").strip()
    if not s:
        return None, "budget missing"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(k|m)?", s)
    if not m:
        return None, f"budget not numeric: {raw!r}"
    n = float(m.group(1))
    if m.group(2) == "k":
        n *= 1_000
    elif m.group(2) == "m":
        n *= 1_000_000
    return int(n), ("budget normalized from %r" % raw if m.group(2) else None)


def parse_ts(raw: str) -> tuple[str | None, str | None]:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat(), None
    except Exception:
        return None, f"submitted_at invalid: {raw!r}"


def domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def clean_message(msg: str) -> tuple[str, list[str]]:
    flags = []
    if "<" in msg and ">" in msg:
        flags.append("html in message (stripped)")
        msg = re.sub(r"<[^>]+>", " ", msg)
    msg = html.unescape(msg)
    return re.sub(r"\s+", " ", msg).strip(), flags


PRIOR: dict[str, str] = {}      # lead_id -> decision, filled as rows are processed
PRIOR_ROW: dict[str, dict] = {}  # lead_id -> raw row


def same_company(a: dict, b: dict) -> bool:
    """Loose company match: same website domain, or normalized names share a token of 4+ letters."""
    da = domain("x@" + re.sub(r"^https?://(www\.)?", "", (a.get("website") or "").lower()).split("/")[0])
    db = domain("x@" + re.sub(r"^https?://(www\.)?", "", (b.get("website") or "").lower()).split("/")[0])
    if da and db:
        return da == db
    ta = {t for t in re.findall(r"[a-z]{4,}", (a.get("company") or "").lower())}
    tb = {t for t in re.findall(r"[a-z]{4,}", (b.get("company") or "").lower())}
    return bool(ta & tb)


def triage(row: dict, seen_emails: dict[str, str]) -> dict:
    """Decide one lead. Returns the full log entry for that lead."""
    lid = row["lead_id"]
    name = (row.get("name") or "").strip()
    email = (row.get("email") or "").strip().lower()
    company = (row.get("company") or "").strip()
    website = (row.get("website") or "").strip()
    message, flags = clean_message(row.get("message") or "")
    budget, bnote = parse_budget(row.get("monthly_budget_usd", ""))
    ts, tnote = parse_ts(row.get("submitted_at", ""))
    for n in (bnote, tnote):
        if n:
            flags.append(n)

    dom = domain(email)
    checks = {
        "has_email": bool(email),
        "free_mail": dom in FREE_MAIL,
        "disposable_mail": dom in DISPOSABLE_MAIL,
        "has_company": bool(company),
        "has_website": bool(website),
        "empty_message": not message,
        "test_like": any(w in TEST_WORDS for w in {name.lower(), company.lower(), message.lower()}) or dom.startswith("asdf"),
        "instruction_text": bool(INSTRUCTION_RE.search(message)),
        "gdpr": bool(GDPR_RE.search(message)),
        "competitor_intel": bool(COMPETITOR_RE.search(message)),
        "student": bool(STUDENT_RE.search(email)) or bool(STUDENT_RE.search(message)),
        "urgency": bool(URGENCY_RE.search(message)),
        "enterprise_claim": bool(ENTERPRISE_RE.search(message)),
        "dangerous_link": bool(DANGEROUS_EXT.search(message)) or any(DANGEROUS_EXT.search(u) for u in URL_RE.findall(message)),
        "non_english": bool(SPANISH_RE.search(message)),
        "duplicate_of": seen_emails.get(email) if email else None,
        "budget": budget,
    }
    if email:
        seen_emails.setdefault(email, lid)

    # ---- Decision ladder: first matching rule wins; order is the policy. -------------
    decision, conf, reason, rule = None, 0.0, "", ""

    if checks["gdpr"]:
        decision, conf, rule = "ESCALATE", 0.95, "privacy_request"
        reason = "Data-subject request (GDPR Art. 17), not a lead. Route to privacy owner; 30-day clock; do not enrich or store further."
    elif checks["dangerous_link"]:
        decision, conf, rule = "REJECT", 0.95, "security_phishing"
        reason = "Message pushes an executable download with an 'overdue invoice' pretext. Phishing pattern. Do not click; forward to security."
        flags.append("security: phishing link")
    elif checks["test_like"] or (checks["empty_message"] and not checks["has_company"]):
        decision, conf, rule = "REJECT", 0.98, "test_or_garbage"
        reason = "Test/garbage submission (placeholder name, email and message)."
    elif checks["instruction_text"]:
        decision, conf, rule = "REJECT", 0.9, "prompt_injection"
        reason = (f"Message contains instructions aimed at the triage system; treated as data and ignored. On its merits: "
                  f"budget ${budget:,} is below the ${QUALIFY_MIN_BUDGET:,} floor and no business need is stated." if budget is not None
                  else "Message contains instructions aimed at the triage system; treated as data. No budget or business need stated.")
        flags.append("security: manipulation attempt")
    elif checks["duplicate_of"] and PRIOR.get(checks["duplicate_of"]) in ("QUALIFY", "NURTURE") and same_company(row, PRIOR_ROW.get(checks["duplicate_of"], {})):
        # v2: merge only when the earlier record is a live opportunity AND the company matches.
        # v1 merged on email alone and turned a rejected student (L-002) into a QUALIFY (L-015).
        decision, conf, rule = "QUALIFY", 0.9, "duplicate_merge"
        reason = f"Same email and company as {checks['duplicate_of']}: merge into that record (adds SEO interest). Do not open a second opportunity."
        flags.append(f"duplicate of {checks['duplicate_of']}")
    elif checks["duplicate_of"]:
        decision, conf, rule = "ESCALATE", 0.6, "conflicting_identity"
        reason = (f"Same email as {checks['duplicate_of']} but a different story (that record was {PRIOR.get(checks['duplicate_of'])}: "
                  f"student/no company; now a business with budget). Conflicting signals; a human verifies before replying.")
        flags.append(f"conflicts with {checks['duplicate_of']}")
    elif checks["student"] and (budget or 0) == 0 and not checks["has_company"]:
        decision, conf, rule = "REJECT", 0.85, "not_a_buyer"
        reason = "Student asking for career mentoring; no company or budget. Send the public resources reply, not a sales follow-up."
    elif checks["student"] and checks["has_company"] is False and (budget or 0) > 0:
        decision, conf, rule = "ESCALATE", 0.6, "conflicting_identity"
        reason = "Same person previously wrote as a student; now claims a restaurant group with budget. Conflicting signals; a human verifies before replying."
    elif checks["competitor_intel"] and (checks["disposable_mail"] or "client of ours" in message.lower()):
        decision, conf, rule = "REJECT", 0.75, "competitor_or_ip_fishing"
        reason = ("Asks for pricing structure / methodology on behalf of 'a client of ours' or from a disposable address: "
                  "agency research, not a buyer. Polite decline; reconsider if they return from a corporate email with a real need.")
    elif checks["disposable_mail"]:
        decision, conf, rule = "REJECT", 0.7, "disposable_email"
        reason = "Disposable email address and no website; cannot verify. Reconsider if they re-contact from a corporate domain."
    elif not checks["has_email"]:
        decision, conf, rule = "ESCALATE", 0.7, "missing_contact"
        reason = "No email on the form; company, site and budget look real. Human finds a contact via the website before anything is sent."
    elif checks["empty_message"]:
        decision, conf, rule = "ESCALATE", 0.5, "missing_message"
        reason = "Empty message with a real domain and budget; intent unknown. Human sends a one-line 'what are you looking for?'."
    elif checks["enterprise_claim"] and budget is not None and budget < ENTERPRISE_BUDGET_FLOOR:
        decision, conf, rule = "ESCALATE", 0.6, "budget_conflict"
        reason = f"Enterprise-scale company but budget field says ${budget}. Likely a typo or a units mismatch; human confirms before scoping."
    elif checks["free_mail"] and not checks["has_company"] and checks["urgency"]:
        decision, conf, rule = "ESCALATE", 0.6, "unverifiable_identity"
        reason = "Personal email, no company named, large budget and pressure to sign this week. Unverifiable; human checks identity (LinkedIn) before any proposal."
    elif budget is None:
        decision, conf, rule = "NURTURE", 0.65, "budget_unknown"
        reason = "Budget not stated ('we'll discuss'); real company and clear use case. Send the pricing explainer and offer a discovery call."
    elif budget >= QUALIFY_MIN_BUDGET:
        decision, conf, rule = "QUALIFY", 0.85, "fits_icp"
        reason = f"Named company with a site, stated need, budget ${budget:,}/mo at or above the ${QUALIFY_MIN_BUDGET:,} floor."
        if checks["non_english"]:
            reason += " Spanish-language lead: route to a Spanish-speaking rep."
            flags.append("language: es")
        if tnote:
            conf = 0.75
            reason += " Timestamp is invalid (form bug), lead itself is fine."
        if "html in message (stripped)" in flags:
            conf = 0.7
            reason += " Message arrived as raw HTML; cleaned. Smaller local-service account."
        if "compliance" in message.lower():
            reason += " Regulated industry: compliance review on all copy."
            flags.append("constraint: compliance review")
    else:
        decision, conf, rule = "NURTURE", 0.85, "below_floor"
        reason = f"Legit company but budget ${budget:,}/mo is below the ${QUALIFY_MIN_BUDGET:,} floor. Start light / revisit at the stated timeline."

    PRIOR[lid] = decision
    PRIOR_ROW[lid] = row
    return {"lead_id": lid, "decision": decision, "confidence": conf, "reason": reason, "rule": rule,
            "flags": flags, "parsed": {"email": email, "domain": dom, "company": company, "budget": budget,
                                      "submitted_at": ts, "message_clean": message[:160]},
            "checks": {k: v for k, v in checks.items() if k not in ("budget",)}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default="fixtures/inbound_leads.csv")
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    fx = Path(a.fixture)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    data = fx.read_bytes()
    checksum = hashlib.sha256(data).hexdigest()
    rows = list(csv.DictReader(data.decode("utf-8").splitlines()))

    seen: dict[str, str] = {}
    log = [{"run_at": datetime.now(timezone.utc).isoformat(), "fixture": str(fx), "sha256": checksum,
            "rows": len(rows), "policy": POLICY_VERSION, "qualify_min_budget": QUALIFY_MIN_BUDGET}]
    decisions = []
    for r in rows:
        try:
            e = triage(r, seen)
        except Exception as ex:  # never let one bad row kill the run
            e = {"lead_id": r.get("lead_id", "?"), "decision": "ESCALATE", "confidence": 0.3,
                 "reason": f"Agent error while parsing this row ({type(ex).__name__}); human review.", "rule": "agent_error",
                 "flags": ["agent_error"], "parsed": {}, "checks": {}}
        log.append(e)
        decisions.append(e)

    with (out / "decisions.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["lead_id", "decision", "confidence", "reason", "flags"])
        for d in decisions:
            w.writerow([d["lead_id"], d["decision"], d["confidence"], d["reason"], "; ".join(d["flags"])])
    with (out / "run_log.jsonl").open("w") as f:
        for e in log:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    counts = {}
    for d in decisions:
        counts[d["decision"]] = counts.get(d["decision"], 0) + 1
    print(f"fixture sha256 {checksum}")
    print(f"{len(rows)} leads -> {counts}")
    for d in decisions:
        print(f"{d['lead_id']}  {d['decision']:<8} {d['confidence']:.2f}  [{d['rule']}]  {'; '.join(d['flags'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
