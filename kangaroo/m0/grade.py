"""The M1 grade: one function, from data only. It replaces every acceptance flag, packet and
seal. The launch call is a separate output (decide.py) and never changes the grade.

- not_permitted: a BLOCK on the pair. M1 runs only the cuts that diagnose it.
- directional_only: an UNKNOWN that affects the grade, or the flight is before its planned end.
- decision_grade: everything else. A person's approval is a stamp on top of it, never part of it.
"""

import hashlib
import json
import os

TEXT = {"not_permitted": "Not permitted", "directional_only": "Directional only", "decision_grade": "Decision grade"}


def grade(checks, mid_flight):
    affecting = [c for c in checks if c["affects_grade"]]
    blocks = [c for c in affecting if c["status"] == "BLOCK"]
    unknowns = [c for c in affecting if c["status"] == "UNKNOWN"]
    warns = [c for c in affecting if c["status"] == "WARN"]
    if blocks:
        g, reasons = "not_permitted", [f"{c['name']}: {c['reason']}" for c in blocks]
    elif unknowns or mid_flight is not False:
        g = "directional_only"
        duration = next((c for c in checks if c["id"] == "duration"), None)
        reasons = [duration["reason"]] if mid_flight and duration else []
        reasons += [c["reason"] for c in unknowns]
    else:
        g, reasons = "decision_grade", []
    return {"grade": g, "text": TEXT[g], "reasons": reasons, "warns": [f"{c['name']}: {c['reason']}" for c in warns],
            "blocks": [c["id"] for c in blocks], "unknowns": [c["id"] for c in unknowns]}


def readout_sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def approval_status(run_dir, graded, readout_text):
    """'approved' only for a decision-grade pair whose stamp matches this readout and names every WARN."""
    path = os.path.join(run_dir, "approval.json")
    if graded["grade"] != "decision_grade":
        return None
    if not os.path.exists(path):
        return "not approved yet"
    with open(path, encoding="utf-8") as handle:
        stamp = json.load(handle)
    if stamp.get("readout_sha") != readout_sha(readout_text):
        return "not approved yet (the approval is for an older report)"
    missing = [w for w in graded["warns"] if w.split(":")[0] not in " ".join(stamp.get("warns_acknowledged", []))]
    if missing:
        return f"not approved yet (the approval does not name: {', '.join(m.split(':')[0] for m in missing)})"
    return f"approved by {stamp.get('approver')} on {stamp.get('time', '')[:10]}"
