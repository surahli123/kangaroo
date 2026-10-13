"""The readout a triage reader acts on, in a fixed order per (control, test) pair:

1. The grade and why, whether the flight is ready for triage, and the launch call
2. What moved, and what was expected to move but didn't
3. Why, as far as the data shows, strongest support first, with the cuts that ran
Then, for the flight: what to do before triage, what to ask the engineer, and similar
experiments (confirmed cards only).

Internal state names (packet, seal, receipt, check IDs) never appear in the body; the footer
carries the provenance. A cause is written as confirmed only when a cut or the scorecard
confirmed it and the claim names the statement.
"""

import re

from kangaroo.render import brief

STRENGTH = {"confirmed": "Confirmed", "consistent": "Fits the data, not yet confirmed", "inferred": "Likely",
            "exploratory": "Exploratory, not for the decision"}
ORDER = {"confirmed": 0, "consistent": 1, "inferred": 2, "exploratory": 3}
OUTCOME = {"confirms": "supports it", "refutes": "rules it out", "inconclusive": "no clear change"}
BANNED = ("packet", "seal", "receipt", "M0-", "accepted_m", "checkpoint")


def _sentence(text):
    text = text.strip().rstrip(".")
    return text[:1].upper() + text[1:] + "." if text else ""


def _missing_primaries(decision):
    return [v["metric"] for v in decision["verdicts"] if v["role"] == "primary" and v["verdict"] == "missing"]


def _bare_change(change):
    m = re.match(r"didn't move clearly \((.*)\)$", change)
    return m.group(1) if m else change


def headline(pairs, display):
    grades = [p["grade"]["text"] for p in pairs]
    return f"# {display}: {grades[0] if len(set(grades)) == 1 else 'grades differ by arm'}"


def readiness_line(p):
    g, r = p["grade"], p["explained"]["readiness"]
    confirmed = [c["candidate"] for c in p["claims"] if c["support_level"] == "confirmed" and c["statement_ids"]]
    confirmed = list(dict.fromkeys(confirmed))
    so_far = f" Confirmed so far: {'; '.join(confirmed)}." if confirmed else ""
    if g["grade"] == "not_permitted":
        diagnosed = any(a["status"] == "ran" and a.get("kind") == "cut" for a in p["actions"])
        tail = " Only cuts that diagnose it ran." if diagnosed else " No deep-dive cut ran: fix this first."
        return f"Ready for triage: no. {_sentence('; '.join(g['reasons']))}{tail}"
    if g["grade"] == "directional_only":
        return f"Ready for triage: for a directional discussion, not a decision.{so_far}"
    reason = r["reasons"][0]
    if re.match(r"\d+ of \d+ movements have no candidate", reason):
        orphans = [c["metric"] for c in p["claims"] if not c["candidate_id"] and not c["change"].startswith("didn't move")
                   and c["metric"].lower() not in {f.lower() for f in p.get("feature_metrics", [])}]
        reason = (f"{', '.join(orphans)} moved with no explanation in the library" if orphans else
                  "some contract metrics didn't move as expected, and nothing in the library explains why")
    return f"Ready for triage: {r['text']}. {_sentence(reason)}{so_far}"


def pair_block(p, control, approval, shared_warn=False, seen=None):
    seen = {} if seen is None else seen
    d, g = p["decision"], p["grade"]
    lines = [f"## {p['test']} against {control}", ""]
    head = f"**{g['text']}**" + (f", {approval}" if g["grade"] == "decision_grade" and approval else "") + "."
    if g["reasons"]:
        head += " " + " ".join(_sentence(x) for x in g["reasons"][:3])
    lines.append(head)
    lines.append(readiness_line(p))
    missing = _missing_primaries(d)
    if missing:
        lines.append(f"Launch call: not made while {len(missing)} contract metric{'s are' if len(missing) > 1 else ' is'} missing.")
    elif g["grade"] == "not_permitted":
        lines.append("Launch call: none until the blocking problem is resolved.")
    else:
        lines.append(f"Launch call (scripted): {d['call_text']}. {_sentence(d['reason'])}")
    if g["warns"] and not shared_warn and g["grade"] != "not_permitted":
        lines += [f"Warning: {_sentence(w)}" for w in g["warns"]]
    if g["grade"] == "not_permitted":
        lines += ["", "Movements are not interpretable until the problem above is resolved; the cuts below diagnose it."]
        return lines
    features = {f.lower() for f in p.get("feature_metrics", [])}
    moved = [c for c in p["claims"] if not c["change"].startswith("didn't move")]
    intended = [c for c in moved if c["metric"].lower() in features]
    moved = [c for c in moved if c["metric"].lower() not in features]
    still = [c for c in p["claims"] if c["change"].startswith("didn't move")]
    lines += ["", "What moved:", ""]
    lines += [f"- {c['metric']} {c['change']} (the intended change)." for c in intended]
    lines += [f"- {c['metric']} {c['change']}." for c in moved[:6]] or (["- Nothing else moved clearly."] if intended else
                                                                    ["- Nothing triage will ask about moved clearly."])
    if still:
        lines += ["", "Expected to move but didn't: " + "; ".join(f"{c['metric']} {_bare_change(c['change'])}" for c in still) + "."]
    lines += ["", "Why, as far as the data shows:", ""]
    explained = sorted({c["candidate_id"]: c for c in p["claims"] if c["candidate_id"]}.values(),
                       key=lambda c: ORDER.get(c["support_level"], 3))
    for c in explained:
        strength = STRENGTH.get(c["support_level"], "Possible")
        if c["support_level"] == "confirmed" and not c["statement_ids"]:
            strength = "Likely"  # never state a cause as confirmed without the data behind it
        if c["candidate_id"] in seen:
            lines.append(f"- [{strength}] {c['candidate']}, as for {seen[c['candidate_id']]}.")
        else:
            seen[c["candidate_id"]] = p["test"]
            lines.append(f"- [{strength}] {c['candidate']}. {_sentence(c['mechanism'])}")
        for a in p["actions"]:
            if a["status"] == "ran" and a.get("target_pattern") == c["candidate_id"]:
                role = "Control cut" if a.get("role") == "control" else "Cut"
                caveat = (f" Caveat: {a['caveat']}, so this compares different query sets and can't confirm it."
                          if a.get("caveat") else "")
                lines.append(f"  - {role}: {a['exact_next_task']}: {OUTCOME.get(a['outcome'], a['outcome'])} "
                             f"({a.get('detail') or 'no detail'}).{caveat}")
        if c["support_level"] == "consistent" and c.get("awaiting"):
            lines.append(f"  - Not confirmed until: {'; '.join(c['awaiting'][:2])}.")
    orphans = [c for c in p["claims"] if not c["candidate_id"] and not c["change"].startswith("didn't move")
               and c["metric"].lower() not in features]
    chance = [c for c in orphans if c["status"] == "possibly chance"]
    unexplained = [c for c in orphans if c["status"] != "possibly chance"]
    if unexplained:
        lines.append("- No explanation in the library for: " + "; ".join(f"{c['metric']} ({c['change'].split(' (')[0]})"
                                                                         for c in unexplained)
                     + ". The move is clear, so it needs a cause; say 'unexplained' rather than guess.")
    if chance:
        lines.append("- Not registered, and no explanation: " + "; ".join(f"{c['metric']} ({c['change'].split(' (')[0]})"
                                                                       for c in chance)
                     + ". With this many metrics some move by chance; treat these as possibly chance.")
    if not explained and not orphans:
        lines.append("- Nothing to explain.")
    return lines


def before_triage(pairs):
    arms_of = {}
    for p in pairs:
        items = [c["reason"] for c in p["m0"] if c["affects_grade"] and c["status"] == "UNKNOWN"]
        gaps = {}
        for a in p["actions"]:
            if a["status"] == "access gap":
                gaps.setdefault(a["source_or_access_gap"], []).append(a["exact_next_task"])
        for gap, cuts in gaps.items():
            items.append(f"run '{cuts[0]}' once its tables are approved ({gap})" if len(cuts) == 1 else
                         f"run {len(cuts)} deep-dive cuts once their tables are approved ({gap}): {'; '.join(cuts)}")
        for a in p["actions"]:
            if a["status"] == "no result":
                items.append(f"'{a['exact_next_task']}' gave no result ({a['source_or_access_gap']})")
        durability = next((q for q in p["explained"]["standard"] if q["question"] == "Will it last?"), None)
        if durability and not durability["ok"] and p["grade"]["grade"] != "not_permitted":
            items.append(f"durability: {durability['answer'].split(': ', 1)[-1]}")
        items += [f"{d['cut']} (tests: {d['tests']})" for d in p["explained"]["deep_dives"] if not d.get("sql")][:2]
        for item in items:
            arms_of.setdefault(item, []).append(p["test"])
    if len(pairs) == 1:
        return list(arms_of)
    return [item + ("" if len(arms) == len(pairs) else f" [{', '.join(arms)}]") for item, arms in arms_of.items()]


def ask_engineer(pairs, cfg_present):
    asks = [] if cfg_present else ["What does each arm change? The config wasn't available to this run."]
    changed = sorted({d for p in pairs for d in (p["m0_details"].get("diffs") or [])})
    if changed:
        asks.append(f"The config shows the arms changing {', '.join(changed)}. Is that everything they change?")
    for p in pairs:
        features = {f.lower() for f in p.get("feature_metrics", [])}
        for q in p["explained"]["standard"]:
            if q.get("note") or q["ok"] is not False:
                continue
            if q["question"].startswith("Is the data trustworthy"):
                arm = f" for {p['test']}" if len(pairs) > 1 else ""
                asks.append(f"The data check{arm} says: {q['answer'].split(': ', 1)[-1]}. What happened?")
        if p["grade"]["grade"] == "not_permitted":
            continue
        for c in p["claims"]:
            if (c["support_level"] != "confirmed" and not c["change"].startswith("didn't move") and c["status"] != "possibly chance"
                    and c["metric"].lower() not in features and c["candidate_id"] != "as_hypothesized"):
                asks.append(f"{c['question']}" + (f" Our best guess: {c['candidate'][:1].lower() + c['candidate'][1:]}." if c["candidate"] else ""))
        for c in p["claims"]:
            if c["change"].startswith("didn't move") and "expected" in c["question"]:
                asks.append(c["question"])
    by_metric, out = {}, []
    for a in dict.fromkeys(asks):
        metric = re.match(r"(?:Why did )?(.+?) (?:was expected|rise|fall|rose|fell)", a)
        key = metric.group(1) if metric and "expected" in a else a
        if key in by_metric:
            if "fell" in a or "rose" in a:
                out[by_metric[key]] = a
            continue
        by_metric[key] = len(out)
        out.append(a)
    return out[:5]


def similar_block(confirmed, drafts):
    lines = [f"- {c['name']} ({c.get('layer')}): {_sentence(c['cause'])} Lesson: {_sentence(c['lesson'])}" for c in confirmed]
    if not confirmed:
        lines.append("- None confirmed yet.")
    if drafts:
        lines.append("- Related drafts, not yet reviewed, so not cited: " + ", ".join(d["name"] for d in drafts) + ".")
    return lines


def summary(pairs, control):
    """For several arms: one row per arm, then what holds for every arm, said once."""
    first = pairs[0]
    features = first.get("feature_metrics", [])
    lead = [v["metric"] for v in first["decision"]["verdicts"] if v["role"] == "primary"][:1]
    deciders = list(dict.fromkeys(p["decision"]["decider"].get("metric") for p in pairs if p["decision"]["decider"].get("metric")))
    cols = list(dict.fromkeys(features + lead + deciders))[:4]
    by_arm = {p["test"]: {m["metric"].lower(): m for m in p["metrics"]} for p in pairs}
    head = ["Arm", "Grade", "Call"] + cols
    lines = [f"{len(pairs)} arms against {control}.", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for p in pairs:
        call = "not made" if _missing_primaries(p["decision"]) or p["grade"]["grade"] == "not_permitted" else p["decision"]["call_text"]
        cells = []
        for c in cols:
            m = by_arm[p["test"]].get(c.lower())
            if not m or m.get("rel_delta") is None:
                cells.append("missing")
            else:
                clear = m.get("rel_ci") and (m["rel_ci"][0] > 0 or m["rel_ci"][1] < 0)
                cells.append(f"{100 * m['rel_delta']:+.2f}%" + ("" if clear else " (not clear)"))
        lines.append("| " + " | ".join([p["test"], p["grade"]["text"], call] + cells) + " |")
    lines.append("")
    arms_of = {}
    for p in pairs:
        for w in p["grade"]["warns"]:
            arms_of.setdefault(w, []).append(p["test"])
    for w, arms in arms_of.items():
        lines.append(f"Warning, {'for every arm' if len(arms) == len(pairs) else 'for ' + ', '.join(arms)}: {_sentence(w)}")
    lines.append("Arms are compared with control, not with each other; a difference between arms needs its own interval.")
    return lines


def interval_note(pairs):
    if all(p["grade"]["grade"] == "not_permitted" or _missing_primaries(p["decision"]) for p in pairs):
        return None  # no call was made, so its intervals need no note
    levels = pairs[0]["decision"]["settings"].get("levels", {})
    base = pairs[0]["decision"]["settings"].get("ci_level", 0.95)
    if levels.get("primary") and abs(levels["primary"] - base) > 1e-9:
        n = sum(1 for v in pairs[0]["decision"]["verdicts"] if v["role"] == "primary")
        return (f"The launch call reads the {n} primaries at {100 * levels['primary']:.2f}% intervals, because they share "
                f"the {100 * (1 - base):.0f}% error rate; the lists show {100 * base:.0f}% intervals.")
    return None


def render(display, control, pairs, confirmed, drafts, provenance, approvals, cfg_present, library=None):
    lines = [headline(pairs, display), ""]
    asks = ask_engineer(pairs, cfg_present)
    if library:
        lines += brief.render(pairs, library, asks, control, confirmed)
        lines += ["# Details", ""]
    shared = len(pairs) > 1
    if shared:
        lines += summary(pairs, control) + [""]
    seen = {}
    for p in pairs:
        lines += pair_block(p, control, approvals.get(p["test"]), shared_warn=shared, seen=seen) + [""]
    note = interval_note(pairs)
    if note:
        lines += [note, ""]
    lines += ["## Before triage", ""] + ([f"- {_sentence(i)}" for i in before_triage(pairs)] or ["- Nothing open."]) + [""]
    lines += ["## Ask the engineer", ""] + ([f"- {a}" for a in asks] or ["- Nothing yet."]) + [""]
    if all(p["grade"]["grade"] == "not_permitted" for p in pairs):
        lines += ["## Similar experiments", "", "- Not compared: the results can't be read until the blocking problem is fixed.", ""]
    else:
        lines += ["## Similar experiments", ""] + similar_block(confirmed, drafts) + [""]
    lines += ["---", f"Provenance: {provenance}", ""]
    text = "\n".join(lines)
    body = text.split("\n---\n")[0].lower()
    # Metric and experiment names come from the team; only the agent's own words are checked.
    for name in sorted({m["metric"] for p in pairs for m in p["metrics"]} | {display}, key=len, reverse=True):
        body = body.replace(name.lower(), "")
    leaked = [w for w in BANNED if w.lower() in body]
    if leaked:
        raise ValueError(f"internal state names in the readout body: {', '.join(leaked)}")
    return text
