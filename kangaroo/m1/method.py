"""M1: why the metrics moved, which cut to run next, and whether the flight is ready for
triage. One (control, test) pair at a time.

The method is ds-experiment's: decide.py makes the launch call (unchanged), and explain.py
maps metric families, matches the enterprise patterns, and scores cut results against each
pattern's confirms_when and refutes_when. This module adds what an agent needs on top:

- it runs the planned cuts that have a SQL template, through the fixed runner, and feeds
  each result back to explain.py, so a candidate becomes confirmed or refuted by data;
- before the first full-page cut it checks that the full-page share of queries didn't move,
  since a filter on a surface the treatment shifts compares different query sets;
- on a not_permitted pair it runs only cuts that diagnose the blocking checks (the library
  has none yet, so a blocked pair runs no deep dive and the readout says so);
- once a cut agrees with a candidate, its control cut runs next, since only both together
  confirm it, and a full-page cut taken while the full-page share moved can't confirm;
- it records every cut as an action (falsifier, predicted outcomes, statement or access gap)
  with the grade at that point;
- it turns explain.py's questions into structured claims, each with a support level and the
  statements behind it.
"""

import decide
import explain

from kangaroo.m0.grade import grade
from kangaroo.m1 import cuts as cutlib
from kangaroo.sources import config as cfgsrc

NO_PRIMARY = "the primary metric (the contract names none)"


def prereg_for(contract, test_group):
    return {**contract, "test_group": test_group}


def action_record(dive, test, run_id):
    return {"kind": "cut", "owner": "agent", "run_id": run_id, "pair": test, "target_pattern": dive.get("pattern"),
            "tests": dive.get("tests"), "exact_next_task": dive["cut"], "template": dive.get("sql"),
            "falsifier": dive.get("refutes"), "predicted_outcomes": {"confirms": dive.get("confirms"), "refutes": dive.get("refutes")},
            "diagnoses": dive.get("diagnoses", []), "role": dive.get("role", "where"), "status": "planned",
            "source_or_access_gap": None, "statement": None, "outcome": None}


def investigate(ctx, test, pair_checks, mid_flight, trace):
    """Run M1 for one pair. ctx carries the scorecard, configs, contract, standards, library,
    templates, warehouse and its overlay settings."""
    sc, contract = ctx["scorecard"], ctx["contract"]
    prereg = prereg_for(contract, test)
    actions = []
    blocked = grade(pair_checks, mid_flight)["grade"] == "not_permitted"
    daily = None if blocked else durability_series(ctx, test, trace, actions)
    # The launch call sees this pair's two groups only, so a bad arm never decides a clean one.
    pair_config = cfgsrc.for_decide(ctx.get("config"), ctx.get("split"), keep=(sc["control_group"], test))
    try:
        decision = decide.decide(sc, prereg, pair_config, ctx["standards"], daily)
    except decide.InputError as err:
        if "no primary" not in str(err):
            trace.step("decide", pair=test, status="refused", reason=str(err))
            raise
        # No primary in the contract: the call isn't made, and everything else still runs.
        trace.step("decide", pair=test, status="no call", reason=str(err))
        prereg = {**prereg, "primaries": {**(prereg.get("primaries") or {}), "metrics": [
            {"metric": NO_PRIMARY, "direction": "increase", "bar": 0.0}]}}
        decision = decide.decide(sc, prereg, pair_config, ctx["standards"], daily)
    # A primary with no bar: the owner has not said which change is worth a launch. decide.py reads a
    # missing bar as "any clear gain", so the call is replaced here (before explain reads it). A bar of 0
    # is the owner's own choice and keeps the call.
    unset = [m["metric"] for m in (prereg.get("primaries") or {}).get("metrics") or [] if m.get("bar") is None]
    if unset:
        trace.step("decide", pair=test, status="no call", reason=f"no bar for {', '.join(unset)}")
        decision = {**decision, "call": "not_made", "call_text": "Not made", "unset_bars": unset,
                    "reason": f"the contract has no bar for {', '.join(unset)}"}
    graded = grade(pair_checks, mid_flight)
    trace.grade(test, graded, after="M0")
    evidence = {"metrics": [], "cuts": []}
    explained = explain.explain(decision, sc, prereg, ctx["library"], evidence, pair_config)
    tried = set()
    surface = {"checked": False, "moved": None}
    for _ in range(ctx["max_cuts"]):
        planned = [d for d in explained["deep_dives"] if d.get("sql") and d["sql"] not in tried and d["sql"] in ctx["templates"]]
        if graded["grade"] == "not_permitted":
            planned = [d for d in planned if set(d.get("diagnoses", [])) & set(graded["blocks"])]
        # Once a cut agrees with a candidate, its control cut goes next: it is what confirms it.
        agreed = {a["target_pattern"] for a in actions if a.get("outcome") == "confirms" and a.get("role") != "control"}
        planned.sort(key=lambda d: not (d.get("role") == "control" and d.get("pattern") in agreed))
        if not planned:
            break
        dive = planned[0]
        tried.add(dive["sql"])
        tpl = ctx["templates"][dive["sql"]]
        if tpl.get("full_page") and not surface["checked"] and "full_page_share" in ctx["templates"]:
            surface["checked"] = True
            guard = _run(ctx, "full_page_share", test, trace)
            record = {**action_record({"cut": "full-page share of all queries, by arm (a guard for full-page cuts)",
                                       "sql": "full_page_share", "refutes": "the share reaching the full page moved"},
                                      test, trace.run_id), "kind": "guard"}
            record.update(status="ran" if guard.get("metric") else "access gap" if guard.get("gap") else "no result",
                          statement=guard.get("statement"), source_or_access_gap=guard.get("gap") or guard.get("problem"),
                          produces=(guard.get("metric") or {}).get("metric"), outcome="inconclusive")
            trace.action(record, grade_after=graded["grade"])
            actions.append(record)
            if guard.get("metric"):
                evidence["metrics"].append(guard["metric"])
                state = explain.state(guard["metric"], ctx["library"]["families"]["full_page_share"]["practical"])
                surface["moved"] = state in ("up", "down")
                surface["text"] = f"the full-page share of queries {explain.change_text(guard['metric'])}"
        action = action_record(dive, test, trace.run_id)
        out = _run(ctx, dive["sql"], test, trace)
        if out.get("gap"):
            action.update(status="access gap", source_or_access_gap=out["gap"])
        elif out.get("problem"):
            action.update(status="no result", source_or_access_gap=out["problem"], statement=out.get("statement"))
        else:
            caveat = (surface.get("text") if surface.get("moved") and tpl.get("full_page")
                      and not tpl.get("surface_shift_is_the_question") else None)
            evidence["metrics"].append(out["metric"])
            if caveat:  # it compares different query sets, so it can't confirm anything
                evidence["cuts"].append({"sql": dive["sql"], "caveat": caveat})
            explained = explain.explain(decision, sc, prereg, ctx["library"], evidence, pair_config)
            hit = next((r for r in explained["cut_results"] if r["cut"] == dive["cut"]), None)
            action.update(status="ran", statement=out["statement"], produces=out["metric"]["metric"],
                          outcome=hit["outcome"] if hit else "inconclusive",
                          detail=hit["detail"] if hit else None, caveat=caveat)
        graded = grade(pair_checks, mid_flight)
        trace.action(action, grade_after=graded["grade"])
        actions.append(action)
    claims = build_claims(explained, actions, ctx["t1_statement"], ctx.get("series_statement"))
    return {"test": test, "decision": decision, "explained": explained, "grade": graded, "actions": actions,
            "claims": claims, "evidence": evidence, "surface": surface, "config_for_decide": pair_config}


def durability_series(ctx, test, trace, actions):
    """The lead primary by days since first exposure, for the novelty check, once per run."""
    if "primary_by_exposure_day" not in ctx["templates"]:
        return None
    cache = ctx.setdefault("_series", {})
    if "result" not in cache:
        record = {**action_record({"cut": "lead primary by days since first exposure", "sql": "primary_by_exposure_day",
                                   "refutes": "the effect is steady across exposure days"}, "all", trace.run_id),
                  "kind": "series", "diagnoses": ["durability"]}
        try:
            res = cutlib.run_cut(ctx["warehouse"], "primary_by_exposure_day", ctx["templates"], ctx["wh_cfg"], ctx["experiment"],
                                 ctx["scorecard"]["analysis"]["as_of"], ctx["scorecard"]["control_group"],
                                 [c["test_group"] for c in ctx["scorecard"]["comparisons"]],
                                 ctx["wh_cfg"].get("min_units_per_cell", 100))
            trace.receipt(res["statement"], origin="cut primary_by_exposure_day", source_role="durability")
            record.update(status="ran", statement=res["statement"], outcome="series")
            ctx["series_statement"] = res["statement"]["statement_id"]
            cache["result"] = res["series"]
        except cutlib.AccessGap as gap:
            trace.gap("cut primary_by_exposure_day", str(gap))
            record.update(status="access gap", source_or_access_gap=str(gap))
            cache["result"] = {}
        trace.action(record, grade_after="before M1")
        actions.append(record)
    points = cache["result"].get(test)
    return points if points and len(points) >= 4 else None


def _run(ctx, template_id, test, trace):
    try:
        res = cutlib.run_cut(ctx["warehouse"], template_id, ctx["templates"], ctx["wh_cfg"], ctx["experiment"],
                             ctx["scorecard"]["analysis"]["as_of"], ctx["scorecard"]["control_group"], [test],
                             ctx["wh_cfg"].get("min_units_per_cell", 100))
    except cutlib.AccessGap as gap:
        trace.gap(f"cut {template_id}", str(gap))
        return {"gap": str(gap)}
    trace.receipt(res["statement"], origin=f"cut {template_id}", source_role="deep dive")
    if not res["metrics"]:
        return {"problem": "; ".join(res["problems"]), "statement": res["statement"]}
    return {"metric": res["metrics"][0], "statement": res["statement"]}


SUPPORT = {"confirmed by a cut": "confirmed", "confirmed by the scorecard": "confirmed", "confirmed by the checks": "confirmed",
           "fits a cut": "consistent"}


def _cut_statements(actions, inst, roles=None):
    """Statements of the ran cuts that agreed with this candidate (optionally of some roles)."""
    return [a["statement"]["statement_id"] for a in actions
            if a["status"] == "ran" and a.get("target_pattern") == inst["id"] and a.get("outcome") == "confirms"
            and (roles is None or a.get("role", "where") in roles)]


def build_claims(explained, actions, t1_statement, series_statement=None):
    """One claim per question triage will ask. support_level:
    confirmed: a cut found the effect where the mechanism acts and its control cut ruled out the
        look-alikes (or the scorecard's own numbers or the checks confirm it);
    consistent: a cut agrees, but the control cut hasn't run, didn't agree, or the cut compared
        different query sets;
    inferred: a candidate from co-movement only; exploratory; or none."""
    live = {}
    for inst in explained["patterns"]:
        live.setdefault(inst["id"], inst)
    scouted = {i["id"]: i for i in explained.get("exploratory", [])}
    claims = []
    for n, q in enumerate(explained["questions"], 1):
        candidates = [live[c] for c in q["candidates"] if c in live]
        top = candidates[0] if candidates else next((scouted[c] for c in q["candidates"] if c in scouted), None)
        awaiting = []
        if top is None:
            level, statements = "none", []
        elif top["status"] in SUPPORT:
            level = SUPPORT[top["status"]]
            if top["status"] in ("confirmed by a cut", "fits a cut"):
                statements = _cut_statements(actions, top)
                awaiting = top.get("awaiting") or []
            elif top["id"] == "novelty" and series_statement:
                statements = [series_statement]
            else:
                statements = [t1_statement]
        elif top["id"] in scouted and top["id"] not in live:
            level, statements = "exploratory", []
        else:
            level, statements = "inferred", []
        claims.append({"claim_id": f"c{n}", "metric": q["metric"], "question": q["question"], "change": q["change"],
                       "candidate_id": top["id"] if top else None, "candidate": top["name"] if top else None,
                       "mechanism": top["mechanism"] if top else None, "support_level": level, "statement_ids": statements,
                       "awaiting": awaiting, "status": q["status"]})
    return claims


def unsupported(claims, actions, library):
    """Claims stated as confirmed that the action records don't back (the plan's second measure).
    Re-derived from what ran, not from the claim's own fields: a cut-confirmed claim needs a ran,
    uncaveated, agreeing cut where the mechanism acts, plus an agreeing control cut when the
    library defines one; any other confirmed claim needs a statement."""
    controls = {p["id"]: [d["cut"] for d in p.get("deep_dives", []) if d.get("role") == "control"] for p in library["patterns"]}
    needs_control = library.get("confirm_needs_control", False)
    out = []
    for c in claims:
        if c["support_level"] != "confirmed":
            continue
        ran = [a for a in actions if a["status"] == "ran" and a.get("target_pattern") == c["candidate_id"]
               and a.get("outcome") == "confirms"]
        if not ran:
            if not c["statement_ids"]:
                out.append(c)
            continue
        where = [a for a in ran if a.get("role", "where") != "control" and not a.get("caveat")]
        control = {a["exact_next_task"] for a in ran if a.get("role") == "control" and not a.get("caveat")}
        wanted = controls.get(c["candidate_id"]) or []
        if not where or (needs_control and (not wanted or not set(wanted) <= control)):
            out.append(c)
    return out
