#!/usr/bin/env python3
"""Compare the agent's decisions with the manual pass (Tier 4 evidence: agent vs human, disagreements listed)."""
import csv, sys
agent = {r["lead_id"]: r for r in csv.DictReader(open(sys.argv[1] if len(sys.argv) > 1 else "out/decisions.csv"))}
manual = {r["lead_id"]: r for r in csv.DictReader(open("manual_pass.csv"))}
agree = [k for k in manual if agent[k]["decision"] == manual[k]["manual_decision"]]
print(f"{len(agree)}/{len(manual)} agree")
for k in manual:
    if agent[k]["decision"] != manual[k]["manual_decision"]:
        print(f"  {k}: agent={agent[k]['decision']} manual={manual[k]['manual_decision']}  ({manual[k]['note']})")
