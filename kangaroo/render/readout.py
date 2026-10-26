"""The report: short sentences in ASD-STE100 style (see ste.py), and evidence beside every claim.

The order follows Optimizely's experiment plan-and-results template
and the usual experiment readout: the result and the call first, then the plan, the results, why
they moved, the data checks and the next steps. Full tables go to the appendix.

    # <experiment>
    ## Summary         result label, launch call, grade, ready for triage (one line each)
    ## Plan            hypothesis, primary metrics with bars, guardrails with limits, arms, dates
    ## Results         each contract metric: change with its 95% CI, and the result against its bar or limit
    ## Why it moved    the strongest causes with their evidence, causes ruled out, changes with no cause
    ## Data quality    only the checks that did not pass
    ## Next steps      one numbered list: what to do before triage
    ## Appendix        every data check, every cause with its cuts, similar experiments
    then the support scale, the evidence sources and the provenance

Every sentence comes from structured results (verdicts, checks, cut actions, claims), not from
the tools' prose. A cause is "Confirmed" only when the claim names the statements behind it.
Internal state names (packet, seal, receipt, check IDs) never appear.
"""

import re

GRADE = {"decision_grade": "Decision grade", "directional_only": "Directional only", "not_permitted": "Not readable"}
SUPPORT = {"confirmed": "Confirmed", "consistent": "Consistent", "inferred": "Likely", "exploratory": "Exploratory"}
ORDER = {"confirmed": 0, "consistent": 1, "inferred": 2, "exploratory": 3}
CALL = {"ship": "Ship", "dont_ship": "Do not ship", "escalate": "Escalate to triage", "keep_running": "Keep the test running",
        "no_effect": "Stop the test. It has no effect", "cant_tell": "No decision", "not_ab": "No decision. This is not an A/B test",
        "not_made": "Not made"}
CHECK = {"PASS": "Pass", "WARN": "Warning", "UNKNOWN": "Not known", "BLOCK": "Fail"}
ROLE = {"primary": "Primary", "guardrail": "Guardrail"}
BANNED = ("packet", "seal", "receipt", "M0-", "accepted_m", "checkpoint")
NOT_APPROVED = "not approved yet"


# ---------- numbers ----------

def pct(x):
    v = 100 * x
    return f"{v:+.1f}%" if abs(v) >= 10 else f"{v:+.2f}%"


def ci(lo, hi):
    return f"{pct(lo)} to {pct(hi)}"


def change(m):
    """'-4.70% (95% CI -6.36% to -3.03%)' from a scorecard or cut metric."""
    if not m or m.get("rel_delta") is None:
        return "not available"
    if not m.get("rel_ci"):
        return f"{pct(m['rel_delta'])} (no interval)"
    return f"{pct(m['rel_delta'])} (95% CI {ci(*m['rel_ci'])})"


def clear(m):
    return bool(m and m.get("rel_ci")) and (m["rel_ci"][0] > 0 or m["rel_ci"][1] < 0)


def moved(m):
    """'increased', 'decreased' or 'did not change clearly'."""
    if not clear(m):
        return "did not change clearly"
    return "increased" if m["rel_delta"] > 0 else "decreased"


def said(m):
    """'increased 72.2% (95% CI +59.3% to +85.1%)', or 'did not change clearly: -1.06% (95% CI ...)'."""
    if not clear(m):
        return f"did not change clearly: {change(m)}"
    return f"{moved(m)} {change(m).lstrip('+-')}"


def _sentence(text):
    text = text.strip().rstrip(".")
    return text[:1].upper() + text[1:] + "." if text else ""


def _lower(text):
    return text[:1].lower() + text[1:] if text and not text[:2].isupper() else text


# ---------- what each pair says ----------

class Pair:
    """One (control, test) pair's results, with the lookups the sections share."""

    def __init__(self, p, library):
        self.p, self.test, self.grade = p, p["test"], p["grade"]
        self.blocked = p["grade"]["grade"] == "not_permitted"
        self.metrics = {m["metric"].lower(): m for m in p["metrics"]}
        self.evidence = {e["metric"].lower(): e for e in p.get("evidence", {}).get("metrics", [])}
        self.features = {f.lower() for f in p.get("feature_metrics", [])}
        self.patterns = {x["id"]: x for x in library["patterns"]} if library else {}
        self.verdicts = [v for v in p["decision"]["verdicts"] if v["role"] in ("primary", "guardrail")
                         and "the contract names none" not in v["metric"]]
        self.no_contract = any("the contract names none" in v["metric"] for v in p["decision"]["verdicts"])
        family_of = {name.lower(): f for f, name in p["explained"].get("families", {}).items()}
        families = (library or {}).get("families", {})
        self.band = lambda name: families.get(family_of.get(name.lower()), {}).get("practical") or 0.01  # noqa: E731

    def matters(self, name):
        """A clear change at least as large as its family's practical size, and not a guardrail inside its limit."""
        m = self.metric(name)
        inside = any(v["metric"].lower() == name.lower() and v["role"] == "guardrail"
                     and v["verdict"] in ("within", "no_harm_detected") for v in self.verdicts)
        return clear(m) and abs(m["rel_delta"]) >= self.band(name) and not inside

    def metric(self, name):
        return self.metrics.get(name.lower())

    def missing_primaries(self):
        return [v["metric"] for v in self.verdicts if v["role"] == "primary" and v["verdict"] == "missing"]

    def causes(self):
        """Live candidate causes, strongest first, each with the claims it explains."""
        by = {}
        for c in self.p["claims"]:
            if c["candidate_id"]:
                by.setdefault(c["candidate_id"], []).append(c)
        return sorted(by.items(), key=lambda kv: ORDER.get(kv[1][0]["support_level"], 4))

    def orphans(self):
        return [c for c in self.p["claims"] if not c["candidate_id"] and self.matters(c["metric"])
                and c["metric"].lower() not in self.features]

    def warns(self):
        """Low power matters only when a primary did not change clearly; other warnings always matter."""
        flat = any(v["role"] == "primary" and self.metric(v["metric"]) and not clear(self.metric(v["metric"]))
                   for v in self.verdicts)
        return [w for w in self.grade["warns"] if flat or not w.lower().startswith("power")]


def unbarred(pr, step=False):
    """The primaries without a bar, by name; or by count when none has a bar (a metric name can hold a comma)."""
    unset = pr.p["decision"].get("unset_bars") or []
    total = sum(1 for v in pr.verdicts if v["role"] == "primary")
    if not unset:
        return None
    if len(unset) == total and total > 1:
        return f"a bar for each of the {total} primaries" if step else f"its {total} primaries"
    return f"the bar for {'; '.join(unset)}" if step else "; ".join(unset)


def _limit(v):
    if v.get("threshold") is None:
        return ""
    return f"{100 * v['threshold']:.2f}%" if v.get("unit", "relative") == "relative" else f"{v['threshold']:g}"


def call_line(pr):
    """The launch call, and the one result that decides it."""
    d = pr.p["decision"]
    if pr.blocked:
        return "None until the data problem is fixed."
    missing = pr.missing_primaries()
    if pr.no_contract:
        return "Not made. The contract names no primary metric."
    if missing:
        return f"Not made. {', '.join(missing)} {'are' if len(missing) > 1 else 'is'} not in the scorecard."
    if unbarred(pr):
        return f"Not made. The contract has no bar for {unbarred(pr)}."
    head = CALL.get(d["call"], d["call_text"]) + "."
    dec = d.get("decider") or {}
    v = next((x for x in d["verdicts"] if x["metric"] == dec.get("metric") and x["role"] == dec.get("role")), None)
    if not v or v.get("point") is None:
        return head
    level = f"{100 * v.get('level', 0.95):g}% CI"
    value = (f"{pct(v['point'])} ({level} {ci(v['low'], v['high'])})" if v.get("unit", "relative") == "relative"
             else f"{v['point']:+g} ({level} {v['low']:+g} to {v['high']:+g})")
    word = "increased" if v["point"] > 0 else "decreased"
    value = value.replace(pct(v["point"]), pct(v["point"]).lstrip("+-"), 1) if v.get("unit", "relative") == "relative" else value
    limit = _limit(v)
    tail = {"breached": f"This is more than its limit of {limit}." if limit else "This is a clear harm. The contract gives no limit.",
            "harm_within_margin": f"This is inside its limit of {limit}." if limit else "",
            "better": f"This meets its bar of {limit}." if limit else "",
            "below_bar": f"This is below its bar of {limit}." if limit else "",
            "worse": "The contract expected the opposite direction.",
            "undetermined": "The change is not clear."}.get(v["verdict"], "")
    if v["verdict"] == "undetermined":
        return f"{head} {v['metric']} did not change clearly: {pct(v['point'])} ({level} {ci(v['low'], v['high'])})."
    return f"{head} {v['metric']} {word} {value}. {tail}".strip()


def triage_line(pr, steps):
    if pr.blocked:
        return "No. Fix the data problem first."
    if pr.grade["grade"] == "directional_only":
        return "For discussion only. Do not use it for a decision."
    return "Yes." if not steps else "Yes, after the next steps."


def next_check(p):
    """(the one check to run next, the open question it bears on): the first planned check aimed
    at the first open question that has a candidate, else the first planned check."""
    plan = p["explained"]["deep_dives"]
    if not plan or p["grade"]["grade"] == "not_permitted":
        return (plan[0] if plan else None), None
    for q in p["explained"]["questions"]:
        if q["status"].startswith("confirmed") or not q["candidates"]:
            continue
        for d in plan:
            if d.get("pattern") in q["candidates"]:
                return d, q
    return plan[0], None


def todo(pr):
    p, steps = pr.p, []
    arm = "the test arm" if pr.test == "test" else f"the {pr.test} arm"
    for c in p["m0"]:
        if not c["affects_grade"] or c["status"] not in ("UNKNOWN", "BLOCK"):
            continue
        if c["status"] == "BLOCK" and c["id"] == "M0-2":
            steps.append(f"Find where {arm} loses users: exposure logging, redirects or crashes. Fix it and run the test again.")
        elif c["status"] == "BLOCK":
            steps.append(_sentence(f"Fix this problem first: {c['reason']}"))
        elif c["id"] == "M0-3" and c["details"].get("no_contract"):
            steps.append("Write the contract: the primary metrics, their bars and the guardrails. To start, run "
                         "`kangaroo contract-template --scorecard FILE --out contract.json`. Then run analyze again with `--contract`.")
        elif c["id"] == "M0-3":
            steps.append(f"Get {', '.join(c['details'].get('missing') or c['details'].get('unfilled') or [])} back in the "
                         "scorecard. Examine the metric pipeline jobs.")
        elif c["id"] == "M0-1":
            steps.append(f"Get the experiment config ({c['details'].get('command') or 'the config CLI'}). Then run kangaroo again.")
        elif c["id"] == "M0-2":
            steps.append("Name the scorecard metric that counts all exposed users. Then kangaroo can test the sample split.")
        elif c["id"] == "duration":
            steps.append("Find the planned end date of the test.")
    if unbarred(pr) and not pr.blocked:
        steps.append(f"Write {unbarred(pr, step=True)} in the contract. Take each bar from the test plan, not from the results.")
    dur = next((c for c in p["m0"] if c["id"] == "duration"), None)
    if dur and dur["details"].get("end") and str(dur["details"].get("as_of")) < str(dur["details"]["end"]):
        steps.append(f"Wait for the planned end ({dur['details']['end']}), or use this report for discussion only.")
    waiting = [a for a in p["actions"] if a["status"] == "access gap" and str(a["source_or_access_gap"]).startswith("waiting")]
    if waiting:
        steps.append(f"Run the {len(waiting)} statements that wait for the warehouse, with the warehouse tool. "
                     "Then run analyze again.")
    gaps = sorted({a["source_or_access_gap"] for a in p["actions"] if a["status"] == "access gap"
                   and not str(a["source_or_access_gap"]).startswith("waiting")})
    if gaps:
        n = sum(1 for a in p["actions"] if a["status"] == "access gap")
        tables = re.sub(r"^not approved for queries: ", "", gaps[0])
        steps.append(f"Get access to these tables: {tables}. Then run kangaroo again to run {n} more cuts.")
    if pr.blocked:
        return steps
    for cid, claims in pr.causes():
        q = pr.patterns.get(cid, {}).get("owner_question")
        if q and claims[0]["support_level"] in ("confirmed", "consistent", "inferred"):
            steps.append(f"Prepare an answer: {q}")
    for cid, claims in pr.causes():
        pending = [w for w in claims[0].get("awaiting") or [] if "none in the library" not in w and "query sets" not in w]
        if claims[0]["support_level"] == "consistent" and pending:
            steps.append(f"Run the control cut: {pending[0]}.")
    d, _ = next_check(p)
    if d and not d.get("sql"):
        steps.append(f"If time permits, examine this: {d['cut']}.")
    return list(dict.fromkeys(steps))


# ---------- sections ----------

RESULT = {"better": "Meets the bar", "below_bar": "Below the bar", "worse": "Opposite direction",
          "undetermined": "No clear change", "within": "Inside the limit", "no_harm_detected": "Inside the limit",
          "harm_within_margin": "Worse, inside the limit", "breached": "Past the limit", "missing": "Not in the scorecard",
          "no_interval": "No interval"}
NO_THRESHOLD = {"better": "Better. No bar", "no_harm_detected": "No clear harm. No limit", "breached": "Worse. No limit"}


def result_text(pr, v):
    m = pr.metric(v["metric"])
    level = pr.p["decision"]["settings"].get("levels", {}).get("primary")
    if v.get("threshold") is None and v["verdict"] in NO_THRESHOLD:
        return NO_THRESHOLD[v["verdict"]]
    if v["verdict"] == "undetermined" and clear(m) and level and v["role"] == "primary":
        return f"No clear change at {100 * level:.2f}%"  # clear at 95%, but the call reads primaries at a wider level
    return RESULT.get(v["verdict"], v["verdict"])


def conclusion(pr):
    """The result label of the plan-and-results template, from the registered verdicts only."""
    if pr.blocked:
        return "Not readable"
    prim = [v for v in pr.verdicts if v["role"] == "primary"]
    if pr.no_contract or not prim or len(pr.missing_primaries()) == len(prim):
        return "Not known"
    good = any(v["verdict"] in ("better", "below_bar") for v in prim)
    bad = (any(v["verdict"] == "worse" for v in prim)
           or any(v["verdict"] == "breached" for v in pr.verdicts if v["role"] == "guardrail"))
    return "Mixed" if good and bad else "Positive" if good else "Negative" if bad else "Inconclusive"


def result_line(pr):
    """'Positive. 1 of 4 primaries improved clearly. 1 guardrail is worse.'"""
    label = conclusion(pr)
    if label == "Not readable":
        block = next((c for c in pr.p["m0"] if c["status"] == "BLOCK"), None)
        return f"Not readable. {_sentence(block['reason'])}" if block else "Not readable."
    if label == "Not known":
        return "Not known. The contract names no primary metric."
    prim = [v for v in pr.verdicts if v["role"] == "primary"]
    up = sum(v["verdict"] in ("better", "below_bar") for v in prim)
    down = sum(v["verdict"] == "worse" for v in prim)
    harm = sum(v["verdict"] == "breached" for v in pr.verdicts if v["role"] == "guardrail")
    parts = [f"{label}.", f"{up} of {len(prim)} primar{'y' if len(prim) == 1 else 'ies'} improved clearly."]
    if down:
        parts.append(f"{down} changed in the opposite direction.")
    if harm:
        parts.append(f"{harm} guardrail{'s are' if harm > 1 else ' is'} worse.")
    return " ".join(parts)


def grade_line(pr, approvals):
    a = approvals.get(pr.test) or (NOT_APPROVED if pr.grade["grade"] == "decision_grade" else None)
    text = GRADE[pr.grade["grade"]] + (f", {a}" if a else "") + "."
    if pr.grade["grade"] == "directional_only":
        reasons = pr.grade.get("reasons") or []
        dur = next((c for c in pr.p["m0"] if c["id"] == "duration"), None)
        if reasons:
            text += f" {_sentence(reasons[0])}"
        elif dur and dur["details"].get("end") and str(dur["details"].get("as_of")) < str(dur["details"]["end"]):
            text += f" The test has not ended. Its planned end is {dur['details']['end']}."
    return text


def summary(prs, approvals):
    lines = ["## Summary", ""]
    if len(prs) == 1:
        pr = prs[0]
        lines += [f"**Result:** {result_line(pr)}", ""]
        if not pr.blocked:
            lines += [f"**Launch call:** {call_line(pr)}", "", f"**Grade:** {grade_line(pr, approvals)}", ""]
        return lines + [f"**Ready for triage:** {triage_line(pr, todo(pr))}", ""]
    lines += ["| Arm | Result | Launch call | Grade | Ready for triage |", "|---|---|---|---|---|"]
    for pr in prs:
        call = "None" if pr.blocked else CALL.get(pr.p["decision"]["call"], pr.p["decision"]["call_text"])
        call = "Not made" if not pr.blocked and (pr.missing_primaries() or pr.no_contract) else call
        lines.append(f"| {pr.test} | {conclusion(pr)} | {call} | {grade_line(pr, approvals).split('.')[0]} | "
                     f"{triage_line(pr, todo(pr))} |")
    lines.append("")
    for pr in prs:
        lines.append(f"- **{pr.test}:** {result_line(pr) if pr.blocked else call_line(pr)}")
    return lines + [""]


def plan(prs, control):
    pr = prs[0]
    hyp = pr.p.get("hypothesis")
    limit = lambda v, word: f"{v['metric']} ({word} {_limit(v)})" if _limit(v) else f"{v['metric']} (no {word})"  # noqa: E731
    prim = [limit(v, "bar") for v in pr.verdicts if v["role"] == "primary"]
    guard = [limit(v, "limit") for v in pr.verdicts if v["role"] == "guardrail"]
    dur = next((c for c in pr.p["m0"] if c["id"] == "duration"), None) or {"details": {}}
    lines = ["## Plan", "",
             f'- **Hypothesis:** "{hyp.rstrip(".")}".' if hyp else "- **Hypothesis:** the contract gives none.",
             f"- **Primary metrics:** {'; '.join(prim)}." if prim else "- **Primary metrics:** none. There is no contract.",
             f"- **Guardrails:** {'; '.join(guard)}." if guard else "- **Guardrails:** none in the contract.",
             f"- **Arms:** {', '.join(p.test for p in prs)} against {control}."]
    if dur["details"].get("as_of"):
        end = dur["details"].get("end")
        lines.append(f"- **Dates:** scorecard snapshot {dur['details']['as_of']}" + (f", planned end {end}." if end else "."))
    return lines + [""]


def cell(m):
    """'-1.06% (-2.44% to +0.32%)': the table header names the 95% CI."""
    if not m or m.get("rel_delta") is None:
        return "Not available"
    return f"{pct(m['rel_delta'])} ({ci(*m['rel_ci'])})" if m.get("rel_ci") else f"{pct(m['rel_delta'])} (no interval)"


def results(prs):
    several = len(prs) > 1
    lines = ["## Results", ""]
    readable = [pr for pr in prs if not pr.blocked]
    if not readable:
        return lines + ["Not readable until the data problem is fixed.", ""]
    rows, more = [], 0
    for pr in readable:
        for v in pr.verdicts:
            rows.append((v["metric"], ROLE[v["role"]], pr.test, cell(pr.metric(v["metric"])), result_text(pr, v)))
        shown = {v["metric"].lower() for v in pr.verdicts}
        for f in pr.p.get("feature_metrics", []):
            m = pr.metric(f)
            if m and f.lower() not in shown:
                rows.append((f, "Feature", pr.test, cell(m), "Changed as intended" if clear(m) else "No clear change"))
                shown.add(f.lower())
        others = {}
        for c in pr.p["claims"]:
            if c["metric"].lower() not in shown and pr.matters(c["metric"]):
                others.setdefault(c["metric"].lower(), c["metric"])
        others = sorted(others.values(), key=lambda name: -abs(pr.metric(name)["rel_delta"]))
        rows += [(name, "Other", pr.test, cell(pr.metric(name)), _sentence(moved(pr.metric(name))).rstrip(".")) for name in others[:3]]
        more += max(0, len(others) - 3)
    order = {}
    for r in rows:
        order.setdefault(r[0].lower(), len(order))
    rows.sort(key=lambda r: order[r[0].lower()])  # one metric's arms together
    if several:
        lines += ["| Metric | Role | Arm | Change (95% CI) | Result |", "|---|---|---|---|---|"]
        lines += [f"| {n} | {role} | {arm} | {chg} | {res} |" for n, role, arm, chg, res in rows]
    else:
        lines += ["| Metric | Role | Change (95% CI) | Result |", "|---|---|---|---|"]
        lines += [f"| {n} | {role} | {chg} | {res} |" for n, role, _, chg, res in rows]
    lines.append("")
    if more:
        lines += [f"Kangaroo found {more} more clear change{'s' if more > 1 else ''}. "
                  "Run `kangaroo why METRIC` to see one of them.", ""]
    for pr in prs:
        if pr.blocked:
            lines += [f"The {pr.test} arm is not readable until the data problem is fixed.", ""]
    note = interval_note(prs)
    return lines + ([note, ""] if note else [])


def _cut_evidence(pr, a, refs):
    e = pr.evidence.get((a.get("produces") or "").lower())
    role = "Control cut" if a.get("role") == "control" else "Cut"
    ref = refs.setdefault(a["statement"]["statement_id"], (len(refs) + 1, a.get("produces") or a["exact_next_task"]))[0]
    verdict = {"confirms": "This agrees with the cause.", "refutes": "This rules out the cause."}.get(
        a.get("outcome"), "This does not confirm the cause and does not rule it out.")
    caveat = " The full-page share changed, so this cut compares different query sets." if a.get("caveat") else ""
    return f"{role} [S{ref}]: {a.get('produces') or a['exact_next_task']} {change(e)}. {verdict}{caveat}"


def cause_row(pr, cid, claims, refs):
    """(name, support, evidence) for one candidate cause."""
    c = claims[0]
    support = SUPPORT.get(c["support_level"], "Possible")
    ran = [a for a in pr.p["actions"] if a["status"] == "ran" and a.get("target_pattern") == cid and a.get("statement")]
    if cid == "as_hypothesized":
        shown = [x for x in claims if pr.metric(x["metric"])][:2]
        evidence = " ".join(_sentence(f"{x['metric']} {said(pr.metric(x['metric']))}")
                            for x in shown) + " Only metrics changed. No cut tested the hypothesis."
        hyp = pr.p.get("hypothesis")
        name = f'The hypothesis in the contract: "{hyp.rstrip(".")}"' if hyp else "The hypothesis in the contract"
    elif ran:
        evidence = " ".join(_cut_evidence(pr, a, refs) for a in ran)
        name = c["candidate"]
    else:
        together = "; ".join(f"{x['metric']} {pct(pr.metric(x['metric'])['rel_delta'])}" for x in claims[:3]
                             if pr.metric(x["metric"]) and pr.metric(x["metric"]).get("rel_delta") is not None)
        gap = any(a["status"] == "access gap" and a.get("target_pattern") == cid for a in pr.p["actions"])
        evidence = (f"Metrics changed together: {together}. " if together else "") + (
            "No cut ran: kangaroo cannot read the query table." if gap else "No cut tested it.")
        if c["support_level"] == "confirmed" and c["statement_ids"]:
            evidence = "The scorecard values agree."
        name = c["candidate"]
    pending = [w for w in c.get("awaiting") or [] if "query sets" not in w]
    if c["support_level"] == "consistent" and pending:
        evidence += f" Not confirmed until this cut agrees: {pending[0]}."
    return _sentence(name).rstrip("."), support, evidence


def ruled_out(pr, refs):
    out = []
    for inst in pr.p["explained"].get("ruled_out") or []:
        ran = [a for a in pr.p["actions"] if a["status"] == "ran" and a.get("target_pattern") == inst["id"]
               and a.get("outcome") == "refutes" and a.get("statement")]
        if ran:
            out.append((_sentence(inst["name"]).rstrip("."), " ".join(_cut_evidence(pr, a, refs) for a in ran)))
    return out


def why(prs, refs, confirmed):
    lines = ["## Why it moved", ""]
    readable = [pr for pr in prs if not pr.blocked]
    if not readable:
        return lines + ["Not examined: the data problem comes first.", ""]
    for pr in readable:
        if len(prs) > 1:
            lines += [f"### {pr.test}", ""]
        causes, items = pr.causes(), []
        for cid, claims in causes[:3]:
            name, support, evidence = cause_row(pr, cid, claims, refs)
            items.append(f"- **{support}:** {name}. {evidence}")
        if len(causes) > 3:
            items.append(f"- {len(causes) - 3} more causes are in the appendix.")
        items += [f"- **Ruled out:** {name}. {evidence}" for name, evidence in ruled_out(pr, refs)]
        orphans = pr.orphans()
        if orphans:
            listed = "; ".join(f"{c['metric']} ({pct(pr.metric(c['metric'])['rel_delta'])})" for c in orphans)
            items.append(f"- **No cause found:** no cause in the library explains {len(orphans)} clear "
                         f"change{'s' if len(orphans) > 1 else ''}: {listed}.")
        lines += (items or ["- No metric changed clearly."]) + [""]
    for c in confirmed:
        lines += [f"Similar past experiment: {c['name']} ({c.get('layer')}). Cause: {_sentence(c['cause'])} "
                  f"Lesson: {_sentence(c['lesson'])}", ""]
    return lines


def power_text(c):
    weak = c["details"]["weak"]
    first = weak[0]
    more = (" 1 more primary is similar." if len(weak) == 2 else f" {len(weak) - 1} more primaries are similar.") if len(weak) > 1 else ""
    return (f"{first['metric']} can detect a change of {100 * first['mde']:.2f}% or more, but its bar is "
            f"{100 * first['bar']:.2f}%.{more}")


def quality(prs):
    lines, seen = ["## Data quality", ""], {}
    for pr in prs:
        for c in pr.p["m0"]:
            if not c["affects_grade"] or c["status"] == "PASS" or (pr.blocked and c["status"] != "BLOCK"):
                continue
            if c["status"] == "WARN" and c["id"] == "M0-5" and not any(w.lower().startswith("power") for w in pr.warns()):
                continue  # every primary moved clearly, so low power does not matter
            text = power_text(c) if c["id"] == "M0-5" and c["status"] == "WARN" and c["details"].get("weak") else _sentence(c["reason"])
            seen.setdefault((c["name"], c["status"], text), []).append(pr.test)
    for pr in prs:
        durability = next((q for q in pr.p["explained"]["standard"] if q["question"] == "Will it last?"), None)
        if not pr.blocked and durability and durability["ok"] is False and "fading" not in durability["answer"]:
            seen.setdefault(("durability", "UNKNOWN", "Kangaroo has no effect series by day since first exposure."), []).append(pr.test)
    checks = [c for c in prs[0].p["m0"] if c["affects_grade"]]
    if not seen:
        return lines + [f"All {len(checks)} checks pass.", ""]
    for (name, status, text), arms in seen.items():
        arm = "" if len(prs) == 1 or len(arms) == len(prs) else f" Arms: {', '.join(arms)}."
        lines.append(f"- **{name[:1].upper() + name[1:]}:** {CHECK.get(status, status)}. {text}{arm}")
    failing = {name for name, _, _ in seen}
    passed = sum(1 for c in checks if c["name"] not in failing)
    return lines + ["", f"{passed} other check{'s pass' if passed != 1 else ' passes'}. "
                        "The appendix gives each check with its evidence.", ""]


def next_steps(prs):
    steps = [s for pr in prs for s in todo(pr)]
    changed = sorted({d for pr in prs for d in (pr.p["m0_details"].get("diffs") or [])})
    if changed:
        steps.append(f"Confirm that the config change to {changed[0]} is the full change." if len(changed) == 1 else
                     f"Confirm that the config changes to {', '.join(changed[:-1])} and {changed[-1]} are the full change.")
    steps = list(dict.fromkeys(steps))
    return ["## Next steps", ""] + ([f"{n}. {s}" for n, s in enumerate(steps, 1)] or ["None."]) + [""]


def causes_table(pr, refs):
    rows = []
    for n, (cid, claims) in enumerate(pr.causes(), 1):
        name, support, evidence = cause_row(pr, cid, claims, refs)
        rows.append(f"| C{n} | {name} | {support} | {evidence} |")
    rows += [f"| - | {name} | Ruled out | {evidence} |" for name, evidence in ruled_out(pr, refs)]
    if not rows:
        return ["No cause to explain: no metric changed clearly."]
    return ["| # | Cause | Support | Evidence |", "|---|---|---|---|"] + rows


def checks_table(pairs):
    seen, order = {}, {}
    for pr in pairs:
        for i, c in enumerate(pr.p["m0"]):
            order.setdefault(c["name"], i)
            key = (c["name"], c["status"], c["reason"], c.get("source"))
            seen.setdefault(key, []).append(pr.test)
    seen = dict(sorted(seen.items(), key=lambda kv: order[kv[0][0]]))  # one check's rows together, in check order
    several = len(pairs) > 1
    head = ["| Check | Arm | Result | Evidence |", "|---|---|---|---|"] if several else ["| Check | Result | Evidence |", "|---|---|---|"]
    lines = list(head)
    for (name, status, reason, source), arms in seen.items():
        ev = _sentence(reason) + (f" Source: {source}." if source else "")
        arm = "all" if len(arms) == len(pairs) else ", ".join(arms)
        cells = [name[:1].upper() + name[1:]] + ([arm] if several else []) + [CHECK.get(status, status), ev]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def interval_note(pairs):
    readable = [pr for pr in pairs if not pr.blocked and not pr.missing_primaries()]
    if not readable:
        return None
    s = readable[0].p["decision"]["settings"]
    levels, base = s.get("levels", {}), s.get("ci_level", 0.95)
    if levels.get("primary") and abs(levels["primary"] - base) > 1e-9:
        n = sum(1 for v in readable[0].verdicts if v["role"] == "primary")
        return (f"The launch call uses {100 * levels['primary']:.2f}% intervals for the {n} primaries, because they share "
                f"one {100 * (1 - base):.0f}% error rate. The tables show {100 * base:.0f}% intervals.")
    return None


def appendix(prs, refs, confirmed, drafts):
    """What the body leaves out: every data check, the causes past the first 3, and the uncited drafts."""
    lines = ["## Appendix", "", "### Data checks", ""] + checks_table(prs) + [""]
    for pr in prs:
        if not pr.blocked and len(pr.causes()) > 3:
            lines += [f"### Causes{f': {pr.test}' if len(prs) > 1 else ''}", ""] + causes_table(pr, refs) + [""]
    if all(pr.blocked for pr in prs):
        return lines
    lines += ["### Similar experiments", ""]
    lines.append("- Confirmed: see Why it moved." if confirmed else "- No confirmed similar experiment.")
    if drafts:
        lines.append("- Drafts that no person has reviewed, so not cited: " + ", ".join(d["name"] for d in drafts) + ".")
    return lines + [""]


def render(display, control, pairs, confirmed, drafts, provenance, approvals, cfg_present, library=None):
    prs = [Pair(p, library) for p in pairs]
    refs = {}
    lines = [f"# {display}", ""]
    lines += summary(prs, approvals)
    lines += plan(prs, control)
    lines += results(prs)
    lines += why(prs, refs, confirmed)
    lines += quality(prs)
    lines += next_steps(prs)
    lines += appendix(prs, refs, confirmed, drafts)
    lines += ["---", "",
              "Support: Confirmed means that a cut and its control cut agree. Consistent means that one cut agrees, "
              "but the control cut does not confirm it. Likely means that metrics changed together, and no cut tested it.", ""]
    if refs:
        lines += ["Sources: " + "; ".join(f"S{n} = {sid} ({what})" for sid, (n, what) in sorted(refs.items(), key=lambda kv: kv[1][0])), ""]
    lines += [f"Provenance: {provenance}", ""]
    text = "\n".join(lines)
    body = text.split("\n---\n")[0].lower()
    for name in sorted({m["metric"] for p in pairs for m in p["metrics"]} | {display}, key=len, reverse=True):
        body = body.replace(name.lower(), "")  # team names are theirs; only the agent's words are checked
    leaked = [w for w in BANNED if w.lower() in body]
    if leaked:
        raise ValueError(f"internal state names in the readout body: {', '.join(leaked)}")
    return text


def team_names(pairs):
    """Names the STE check skips: metrics, cuts, arms and causes come from the team or the library data."""
    names = {m["metric"] for p in pairs for m in p["metrics"]} | {p["test"] for p in pairs}
    names |= {a.get("produces") or "" for p in pairs for a in p["actions"]} | {a["exact_next_task"] for p in pairs for a in p["actions"]}
    names |= {e["metric"] for p in pairs for e in p.get("evidence", {}).get("metrics", [])}
    names |= {d["cut"] for p in pairs for d in p["explained"]["deep_dives"]}
    names |= {d for p in pairs for d in (p["m0_details"].get("diffs") or [])}
    return names


def summary_text(text):
    """What a chat reply gives: the title, the Summary and the Next steps."""
    parts = [text[:text.find("\n")].strip()]
    for head in ("## Summary", "## Next steps"):
        start = text.find(head)
        if start < 0:
            continue
        end = re.search(r"^## ", text[start + 3:], re.M)
        parts.append((text[start:start + 3 + end.start()] if end else text[start:]).strip())
    return "\n\n".join(parts) if len(parts) > 1 else text
