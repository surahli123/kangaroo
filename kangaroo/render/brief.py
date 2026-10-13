"""'In one minute': the top of the report, for the person who owns the experiment.

Four questions, in plain words, from the same structured results as the details below:
1. Can this go to triage, and why (not)?
2. What do the results say so far?
3. What do I do before triage?
4. What will triage ask me?

No interval jargon: a range is "anywhere from A to B". Every number comes from the run.
"""

GO = {"ready": "Yes.", "open_questions": "Yes, with open questions.", "not_ready": "Not yet."}


def _pct(x):
    return f"{100 * x:+.1f}%"


def _range(m):
    lo, hi = m["rel_ci"]
    return f"anywhere from {_pct(lo)} to {_pct(hi)}"


def _clear(m):
    return bool(m.get("rel_ci")) and (m["rel_ci"][0] > 0 or m["rel_ci"][1] < 0)


def _how_much(m):
    rel = m["rel_delta"]
    if rel >= 1:
        return f"is about {1 + rel:.1f} times what control has"
    word = "rose" if rel > 0 else "fell"
    return f"{word} {abs(100 * rel):.1f}%"


def plain_block(check, arm="test"):
    """A blocking check in words an experiment owner can act on."""
    d = check.get("details") or {}
    if check["id"] == "M0-2" and d.get("counts"):
        c, t = d["counts"]
        share, planned = 100 * t / (c + t), 100 * d.get("planned_test_share", 0.5)
        who = "The test arm" if arm == "test" else f"The {arm} arm"
        return (f"{who} has {share:.0f}% of the exposed users where the plan says {planned:.0f}%; a gap that size "
                "isn't chance, so users are being lost or double-counted somewhere.")
    return check["reason"][:1].upper() + check["reason"][1:].rstrip(".") + "."


def plain_warn(warn):
    """A warning ("Name: reason; reason") as one short sentence: its first part, and a count of the rest."""
    name, _, reason = warn.partition(": ")
    parts = [x for x in reason.split("; ") if x] or [warn]
    more = f" (and {len(parts) - 1} more)" if len(parts) > 1 else ""
    if name.lower() == "power":
        text = f"the test is small for its bar: {parts[0]}{more}, so a flat result doesn't rule out a real effect"
    else:
        text = f"{name.lower()}: {parts[0].rstrip('.')}{more}" if reason else warn
    return text.rstrip(".") + "."


def _warns_that_matter(p):
    """Low power matters only when a primary didn't clearly move; every other warning matters."""
    metrics = {m["metric"].lower(): m for m in p["metrics"]}
    flat = any(v["role"] == "primary" and metrics.get(v["metric"].lower()) and not _clear(metrics[v["metric"].lower()])
               for v in p["decision"]["verdicts"])
    return [w for w in p["grade"]["warns"] if flat or not w.lower().startswith("power")]


def can_go(p):
    g, r = p["grade"], p["explained"]["readiness"]
    if g["grade"] == "not_permitted":
        return "No.", [plain_block(c, p["test"]) for c in p["m0"] if c["status"] == "BLOCK"][:2] + [
            "The results can't be read until that is fixed."]
    if g["grade"] == "directional_only":
        return "Yes, to discuss direction, but not to decide.", ([x[:1].upper() + x[1:].rstrip(".") + "." for x in g["reasons"][:2]]
                                                                + [f"Check first: {plain_warn(w)}" for w in _warns_that_matter(p)[:1]])
    reason = r["reasons"][0] if r.get("reasons") else ""
    if reason[:1].isdigit() and "have no candidate" in reason:
        features = {f.lower() for f in p.get("feature_metrics", [])}
        orphans = [c["metric"] for c in p["claims"] if not c["candidate_id"] and not c["change"].startswith("didn't move")
                   and c["metric"].lower() not in features]
        reason = (f"{', '.join(orphans)} moved with no explanation yet" if orphans else
                  "some metrics didn't move as expected, and nothing explains why yet")
    if reason.lower().startswith("durability is unknown"):
        reason = "whether the effect lasts is unknown, because there is no effect series by days since exposure"
    why = [reason[:1].upper() + reason[1:].split(". ")[0].rstrip(".") + "."] if reason and r["verdict"] != "ready" else []
    why += [f"Check first: {plain_warn(w)}" for w in _warns_that_matter(p)[:2]]
    missing = [v for v in p["decision"]["verdicts"] if v["role"] == "primary" and v["verdict"] == "missing"]
    if not missing:
        decider = p["decision"]["decider"].get("metric")
        why.append(f"The scripted launch call is {p['decision']['call_text']}" + (f", decided by {decider}." if decider else "."))
    return GO.get(r["verdict"], r["text"] + "."), why


def results(p, library):
    if p["grade"]["grade"] == "not_permitted":
        return []
    metrics = {m["metric"].lower(): m for m in p["metrics"]}
    out = []
    for f in p.get("feature_metrics", []):
        m = metrics.get(f.lower())
        if m and m.get("rel_delta") is not None:
            out.append(f"The change did what it was built to do: {f} {_how_much(m)}." if _clear(m) else
                       f"The change may not have reached users: {f} didn't clearly move ({_range(m)}).")
    evidence = {e["metric"].lower(): e for e in p.get("evidence", {}).get("metrics", [])}
    shown = set()
    for c in p["claims"]:
        if c["support_level"] != "confirmed" or not c["statement_ids"] or c["candidate_id"] in shown:
            continue
        shown.add(c["candidate_id"])
        ran = [a for a in p["actions"] if a["status"] == "ran" and a.get("outcome") == "confirms"
               and (a.get("produces") or "").lower() in evidence and a.get("target_pattern") == c["candidate_id"]]
        line = f"The data confirms: {c['candidate'][:1].lower() + c['candidate'][1:]}"
        if ran:
            e = evidence[ran[0]["produces"].lower()]
            line += f" ({e['metric'][:1].lower() + e['metric'][1:]} {_how_much(e)})"
        out.append(line + ".")
    for c in p["claims"]:
        if c["support_level"] != "consistent" or c["candidate_id"] in shown:
            continue
        shown.add(c["candidate_id"])
        caveated = any(a.get("caveat") and a.get("target_pattern") == c["candidate_id"] and a["status"] == "ran" for a in p["actions"])
        why = ("the check that agrees compares different query sets" if caveated else
               "one check agrees, but the check that rules out look-alikes hasn't confirmed it")
        out.append(f"Fits the data, not yet confirmed: {c['candidate'][:1].lower() + c['candidate'][1:]} ({why}).")
    likely = [c for c in p["claims"] if c["support_level"] == "inferred" and c["candidate_id"] != "as_hypothesized"
              and c["candidate_id"] not in shown]
    if likely:
        out.append(f"Probably, not yet checked: {likely[0]['candidate'][:1].lower() + likely[0]['candidate'][1:]}.")
    moved, still = [], []
    for v in p["decision"]["verdicts"]:
        m = metrics.get(v["metric"].lower())
        if v["role"] != "primary" or not m or m.get("rel_ci") is None:
            continue
        if _clear(m):
            moved.append(f"{v['metric']} {_how_much(m)} ({_range(m)}).")
        else:
            still.append(f"{v['metric']} hasn't clearly moved: {_pct(m['rel_delta'])}, but the true change could be {_range(m)}.")
    out += moved + still[:1]
    harm = p["decision"]["decider"] if p["decision"]["call"] == "dont_ship" else None
    if harm and harm.get("metric") and harm.get("role") == "guardrail":
        m = metrics.get(harm["metric"].lower())
        if m:
            out.append(f"{harm['metric']} {_how_much(m)}, past the limit the team set for it.")
    return out[:6]


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


def todo(p, library, command="the config CLI"):
    steps = []
    by_id = {c["id"]: c for c in p["m0"]}
    for c in p["m0"]:
        if not c["affects_grade"] or c["status"] not in ("UNKNOWN", "BLOCK"):
            continue
        if c["id"] == "M0-3" and c["status"] == "UNKNOWN":
            steps.append(f"Get {', '.join(c['details'].get('missing', []))} back on the scorecard (check the metric pipeline jobs).")
        elif c["id"] == "M0-1" and c["status"] == "UNKNOWN":
            steps.append("Fetch the experiment config, so the run can check what each arm changes.")
        elif c["id"] == "M0-2" and c["status"] == "UNKNOWN":
            steps.append("Name the scorecard metric that counts all exposed users, so the sample split can be tested.")
        elif c["status"] == "BLOCK" and c["id"] == "M0-2":
            steps.append("Find where the test arm loses users (exposure logging, redirects, crashes), fix it, and rerun.")
        elif c["status"] == "BLOCK":
            steps.append(f"Fix this first: {c['reason']}.")
        elif c["id"] == "duration":
            steps.append("Find the planned end date, so the run knows whether the test is finished.")
    dur = by_id.get("duration")
    if dur and dur["details"].get("end") and str(dur["details"].get("as_of")) < str(dur["details"]["end"]):
        steps.append(f"Wait for the planned end ({dur['details']['end']}), or present this as a directional read.")
    if any(a["status"] == "access gap" for a in p["actions"]):
        steps.append("Ask for access to the query-level table, so the deep-dive checks can run and show whether the effect lasts.")
    patterns = {x["id"]: x for x in library["patterns"]}
    asked = set()
    blocked = p["grade"]["grade"] == "not_permitted"
    for c in [] if blocked else p["claims"]:  # a blocked arm's movements aren't interpretable yet
        q = patterns.get(c["candidate_id"] or "", {}).get("owner_question")
        if q and c["support_level"] in ("confirmed", "consistent", "inferred") and q not in asked:
            asked.add(q)
            steps.append(f"Bring an answer to: {q}")
    for c in [] if blocked else p["claims"]:
        pending = [w for w in c.get("awaiting") or [] if "none in the library" not in w and "query sets" not in w]
        if c["support_level"] == "consistent" and pending:
            steps.append(f"To confirm that {c['candidate'][:1].lower() + c['candidate'][1:]}, run: {pending[0]}.")
    d, q = next_check(p)
    if d and p["grade"]["grade"] != "not_permitted" and not any(a["status"] == "access gap" and a["exact_next_task"] == d["cut"]
                                                               for a in p["actions"]):
        steps.append(f"If there is time, check {d['cut']}" + (f" (it bears on: {q['question']})." if q else "."))
    elif p["grade"]["grade"] == "not_permitted" and d:
        steps.append(f"To find the cause, check {d['cut']}.")
    return list(dict.fromkeys(steps))[:5]


def render(pairs, library, asks, control, confirmed_cards=()):
    lines = ["## In one minute", ""]
    several = len(pairs) > 1
    answers = [(p["test"],) + can_go(p) for p in pairs]
    if len({(v, tuple(w)) for _, v, w in answers}) == 1:
        _, verdict, why = answers[0]
        verdict = verdict.rstrip(".") + (f", for every arm against {control}." if several else ".")
        lines.append(f"**Can this go to triage?** {verdict}" + (f" {' '.join(why)}" if why else ""))
    else:
        lines.append("**Can this go to triage?**")
        lines += [f"- {arm}: {verdict}" + (f" {' '.join(why)}" if why else "") for arm, verdict, why in answers]
    lines.append("")
    first = next((p for p in pairs if p["grade"]["grade"] != "not_permitted"), pairs[0])
    said = results(first, library)
    if said:
        lines += [f"What the results say{' (' + first['test'] + ')' if several else ''}:", ""] + [f"- {s}" for s in said] + [""]
    readable = any(p["grade"]["grade"] != "not_permitted" for p in pairs)
    for c in list(confirmed_cards)[:1] if readable else []:
        lines += [f"A similar past experiment, {c['name']}, taught: {c['lesson'].rstrip('.')}.", ""]
    steps = []
    for p in pairs:
        tag = f"({p['test']}) " if several and p["grade"]["grade"] == "not_permitted" else ""
        steps += [tag + s for s in todo(p, library) if tag + s not in steps]
    if steps:
        lines += ["What to do before triage:", ""] + [f"{n}. {s}" for n, s in enumerate(steps[:5], 1)] + [""]
    if asks:
        lines += ["What triage will ask you:", ""] + [f"- {a}" for a in asks[:3]] + [""]
    return lines
