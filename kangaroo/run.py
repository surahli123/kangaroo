#!/usr/bin/env python3
"""One run of the search experiment agent, end to end.

    python3 -m kangaroo.run --experiment <name | config id | console URL> --overlay DIR \
        --warehouse sqlite:PATH --configs DIR --out RUN_DIR [--knowledge DIR] [--cards DIR]

Steps: resolve the experiment; read T1's latest snapshot and daily series; run M0 on that
data and grade each (control, test) pair; run M1 per pair (launch call, explanations, and
the cuts the plan can script, fed back as evidence); render the readout; write the live trace, a card draft, a review form and the frozen case inputs.

Writes into RUN_DIR: readout.md, readout.html, trace.json, review.json,
card_draft.json and case/ (the aggregate inputs the learning loop replays). Prints counts only.
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import explain  # noqa: E402

from learning import cards as cardlib  # noqa: E402
from learning import knowledge  # noqa: E402
from kangaroo import LIBRARY  # noqa: E402
from kangaroo.m0.checks import run_m0  # noqa: E402
from kangaroo.m0.grade import approval_status, readout_sha  # noqa: E402
from kangaroo.m1 import cuts as cutlib  # noqa: E402
from kangaroo.m1 import method  # noqa: E402
from kangaroo.render import readout as rd  # noqa: E402
from kangaroo.render.html import to_html  # noqa: E402
from kangaroo.sources import config as cfgsrc  # noqa: E402
from kangaroo.sources import resolver  # noqa: E402
from kangaroo.sources import t1 as t1src  # noqa: E402
from kangaroo.sources.warehouse import (CommandWarehouse, HandoffWarehouse, RecordingWarehouse,  # noqa: E402
                                            SqliteWarehouse)
from kangaroo.trace import Trace  # noqa: E402


def load_json(path, default=None):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return default


def write_json(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1, default=str)


def run(experiment, overlay, warehouse, configs, out_dir, knowledge_dir=None, cards_dir=None, contract=None,
        wh_overrides=None):
    os.makedirs(os.path.join(out_dir, "case"), exist_ok=True)
    warehouse = RecordingWarehouse(warehouse)
    wh_cfg = load_json(os.path.join(overlay, "warehouse.json"))
    if not wh_cfg:
        raise FileNotFoundError(f"{overlay}/warehouse.json is missing")
    wh_cfg = {**wh_cfg, **(wh_overrides or {})}
    std = {**load_json(os.path.join(overlay, "standards.json"), {}), "srm_unit_fallback": False}
    kdir = knowledge_dir or overlay
    library = explain.load_library(kdir, base=LIBRARY)
    templates = cutlib.load_templates(kdir)
    version = knowledge.version_hash(kdir)
    code_version = knowledge.run_version(kdir, overlay)
    cols, table = t1src.columns(wh_cfg), wh_cfg["tables"]["t1"]

    who = resolver.resolve(experiment, configs, warehouse, table, cols)
    name, cfg = who["experiment_name"], who["config"]
    run_id = f"{name}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
    trace = Trace(run_id, name, version)
    trace.data["code_version"] = code_version
    trace.step("resolve", how=who["how"], config_id=who["config_id"], notes=who["notes"])
    trace.receipt(who["statement"], origin="resolve", source_role="identity")

    latest = warehouse.query(t1src.latest_sql(table, cols), {"experiment": name}, label="T1 latest snapshot")
    trace.receipt({k: latest[k] for k in ("statement_id", "label", "row_count", "sql_sha")}, "T1 latest snapshot", "scorecard")
    registered = (cfg or {}).get("sequential")  # the flight's own setting wins over the overlay's default
    sc = t1src.scorecard_from_rows(latest["rows"], cols, wh_cfg.get("prefer_cuped", True), wh_cfg.get("ci_level", 0.95),
                                   wh_cfg.get("prefer_sequential", True) if registered is None else registered)
    aliases = {k.lower(): v for k, v in (load_json(os.path.join(kdir, "metric_aliases.json"), {}) or {}).items()}
    read_as = sorted({(m["metric"], aliases[m["metric"].lower()]) for c in sc["comparisons"] for m in c["metrics"]
                      if m["metric"].lower() in aliases})
    for c in sc["comparisons"]:  # approved aliases: a renamed scorecard metric reads as the contract's name
        for m in c["metrics"]:
            m["metric"] = aliases.get(m["metric"].lower(), m["metric"])
    contract = contract or load_json(os.path.join(overlay, "contracts", f"{name}.json"))
    if not contract:  # no contract lowers the grade (M0 coverage is UNKNOWN); M1 still runs
        trace.step("contract", status="missing", where=f"{overlay}/contracts/{name}.json")
        contract = {"_missing": True, "confirmed": False, "primaries": {"rule": "any", "metrics": []}, "guardrails": None,
                    "hypothesis": None, "intended_params": [], "feature_metrics": [], "explanations": []}
    split_metric = wh_cfg.get("split_count_metric")
    # The series covers the split metric and the primaries, plus any scorecard name that starts with
    # one (a renamed 'Search Success Rate v2'). It depends on the scorecard and the contract only, never
    # on the knowledge, so a frozen case answers it under any lesson.
    wanted = [m for m in [split_metric] + [s["metric"] for s in (contract.get("primaries") or {}).get("metrics", [])] if m]
    names = sorted({str(r["metric"]) for r in latest["rows"]})
    watched = list(dict.fromkeys(wanted + [n for n in names if any(n.lower().startswith(w.lower()) for w in wanted)]))
    series = warehouse.query(t1src.series_sql(table, cols), {"experiment": name, "metrics": watched}, label="T1 daily series")
    series = {**series, "rows": [{**r, "metric": aliases.get(str(r["metric"]).lower(), r["metric"])}
                                 for r in t1src.same_mode(series["rows"], sc["analysis"])]}
    trace.receipt({k: series[k] for k in ("statement_id", "label", "row_count", "sql_sha")}, "T1 daily series", "data quality")
    trace.action({"kind": "query", "owner": "agent", "run_id": run_id, "pair": "all", "target_pattern": None,
                  "exact_next_task": "exposed units and primaries by day and arm (T1 series)", "diagnoses": ["M0-2", "M0-6"],
                  "falsifier": "units fall or jump between snapshots", "status": "ran",
                  "statement": {k: series[k] for k in ("statement_id", "row_count")}}, grade_after="before M0")

    m0, mid = run_m0(sc, series["rows"], cfg, contract, std, wh_cfg, configs.command)
    split = {}
    for c in sc["comparisons"]:
        m = next((x for x in c["metrics"] if split_metric and x["metric"].lower() == split_metric.lower()), None)
        if m:
            split[sc["control_group"]] = int(m["control_units"] or 0)
            split[c["test_group"]] = int(m["test_units"] or 0)
    ctx = {"scorecard": sc, "contract": contract, "standards": std, "library": library, "templates": templates,
           "warehouse": warehouse, "wh_cfg": wh_cfg, "experiment": name, "config": cfg, "split": split,
           "t1_statement": latest["statement_id"], "max_cuts": wh_cfg.get("max_cuts", 8)}
    pairs = []
    for comparison in sc["comparisons"]:
        test = comparison["test_group"]
        out = method.investigate(ctx, test, m0[test], mid[test], trace)
        out["m0"] = m0[test]
        out["feature_metrics"] = contract.get("feature_metrics") or []
        out["hypothesis"] = contract.get("hypothesis")
        out["metrics"] = comparison["metrics"]
        out["m0_details"] = next(c["details"] for c in m0[test] if c["id"] == "M0-1")
        trace.claims(test, out["claims"])
        pairs.append(out)

    families = pairs[0]["explained"]["families"] if pairs else {}
    card = cardlib.draft(run_id, cfg, name, pairs, families)
    confirmed, drafts = cardlib.similar(cardlib.load(cards_dir), name, (cfg or {}).get("layer"), (cfg or {}).get("tags"),
                                        card["families_moved"])
    display = (cfg or {}).get("name") or name
    onoff = lambda v: {True: "on", False: "off"}.get(v, "not known")  # noqa: E731
    provenance = (f"T1 snapshot {sc['analysis']['as_of']} ({latest['statement_id']}), with CUPED {onoff(sc['analysis']['cuped'])} "
                  f"and sequential testing {onoff(sc['analysis']['sequential'])}; config "
                  f"{(cfg or {}).get('id') or 'not fetched'}; knowledge version {version[:12]}; code and settings version "
                  f"{code_version[:12]}; run {run_id}." + (" Read as approved aliases: " + "; ".join(f"'{a}' as '{b}'" for a, b in read_as) + "."
                                            if read_as else ""))
    percent = "rel_delta" in (cols.get("_percent_fields") or [])
    daily = [{**r, "rel_delta": (r["rel_delta"] / 100 if percent else r["rel_delta"]) if r.get("rel_delta") is not None else None}
             for r in series["rows"]]
    text = rd.render(display, sc["control_group"], pairs, confirmed, drafts, provenance, {}, cfg is not None, library, daily)
    sha = readout_sha(text)
    approvals = {p["test"]: approval_status(out_dir, p["grade"], text) for p in pairs}
    if any(approvals.values()):
        text = rd.render(display, sc["control_group"], pairs, confirmed, drafts, provenance, approvals, cfg is not None, library, daily)
    figs = rd.figures(pairs, library, daily)
    os.makedirs(os.path.join(out_dir, "charts"), exist_ok=True)
    for fname, svg in figs.items():
        with open(os.path.join(out_dir, "charts", fname), "w", encoding="utf-8") as handle:
            handle.write(svg)
    with open(os.path.join(out_dir, "readout.md"), "w", encoding="utf-8") as handle:
        handle.write(text)
    with open(os.path.join(out_dir, "readout.html"), "w", encoding="utf-8") as handle:
        handle.write(to_html(text, f"{display}: experiment report", figs))

    claims = [c for p in pairs for c in p["claims"]]
    counts = {"pairs": len(pairs), "claims": len(claims), "unsupported": sum(len(method.unsupported(p["claims"], p["actions"], library)) for p in pairs),
              "confirmed": sum(c["support_level"] == "confirmed" for c in claims),
              "consistent": sum(c["support_level"] == "consistent" for c in claims),
              "cuts_ran": sum(a["status"] == "ran" for p in pairs for a in p["actions"]),
              "access_gaps": sum(a["status"] == "access gap" for p in pairs for a in p["actions"]),
              "grades": {p["test"]: p["grade"]["grade"] for p in pairs},
              "calls": {p["test"]: p["decision"]["call"] for p in pairs},
              "next_check": next_check_proxies(pairs, templates, wh_cfg)}
    trace.finish(sha, counts)
    trace.write(os.path.join(out_dir, "trace.json"))
    write_json(os.path.join(out_dir, "card_draft.json"), card)
    if cards_dir:
        cardlib.save(cards_dir, card)
    write_json(os.path.join(out_dir, "review.json"), {
        "run_id": run_id, "reviewer": None, "date": None,
        "claims": [{"claim_id": c["claim_id"], "pair": p["test"], "metric": c["metric"], "candidate": c["candidate"],
                    "support_level": c["support_level"], "verdict": None, "note": ""} for p in pairs for c in p["claims"]],
        "checks": [{"pair": p["test"], "cut": a["exact_next_task"], "status": a["status"], "useful": None}
                   for p in pairs for a in p["actions"]],
        "missing": []})
    case = os.path.join(out_dir, "case")
    write_json(os.path.join(case, "scorecard.json"), sc)
    write_json(os.path.join(case, "series.json"), series["rows"])
    write_json(os.path.join(case, "config.json"), cfg)
    write_json(os.path.join(case, "contract.json"), contract)
    write_json(os.path.join(case, "evidence.json"), {p["test"]: p["evidence"] for p in pairs})
    warehouse.save(os.path.join(case, "queries.json"))
    write_json(os.path.join(case, "meta.json"), {"experiment": name, "run_id": run_id, "knowledge_version": version,
                                                 "input": experiment, "config_id": (cfg or {}).get("id"),
                                                 "wh_cfg": {k: wh_cfg.get(k) for k in ("split_count_metric", "ci_level")}})
    return {"run_id": run_id, "readout": text, "pairs": pairs, "counts": counts, "trace": trace.data, "card": card}


def next_check_proxies(pairs, templates, wh_cfg):
    """The plan's first measure without a reviewer, on the check the report tells the person to
    run next (readout.next_check): is it scriptable, runnable under current access, and aimed at
    the top open question?"""
    out = {}
    approved = set(wh_cfg.get("approved") or [])
    for p in pairs:
        top, question = rd.next_check(p)
        if not top:
            out[p["test"]] = {"has_next_check": False}
            continue
        open_q = [q for q in p["explained"]["questions"] if not q["status"].startswith("confirmed") and q["candidates"]]
        out[p["test"]] = {"has_next_check": True, "scriptable": bool(top.get("needs")),
                          "runnable": bool(top.get("sql")) and top["sql"] in templates
                          and all(t in approved for t in cutlib.needed_tables(wh_cfg)),
                          "targets_top_question": bool(open_q) and question is not None and question is open_q[0]}
    return out


def scorecard_rows(path):
    import csv
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    else:
        doc = load_json(path)
        rows = doc.get("rows", doc) if isinstance(doc, dict) else doc
    if not rows:
        raise ValueError(f"{path} has no rows")
    return rows


T1_REQUIRED = ("experiment", "metric", "control_group", "test_group", "snapshot", "rel_delta", "rel_ci_lo", "rel_ci_hi")


def check_scorecard_columns(rows, colmap):
    cols = list(rows[0])
    missing = [f"{colmap[f]} (t1_columns key '{f}')" for f in T1_REQUIRED if colmap[f] not in cols]
    if missing:
        raise ValueError(f"the scorecard file has no column {', '.join(missing)}. Its columns are: {', '.join(cols)}. "
                         "Map them in t1_columns of the overlay's warehouse.json")


def scorecard_warehouse(path, out_dir, table, colmap=None):
    """A scorecard file (CSV, or JSON rows) in T1's shape -> a one-table SQLite warehouse.
    With colmap (kangaroo name -> file column): a missing required column is an error that
    names the t1_columns key to set; a missing optional column is added as empty."""
    import sqlite3
    rows = scorecard_rows(path)
    cols = list(rows[0])
    if colmap:
        check_scorecard_columns(rows, colmap)
        fields = [f for f in colmap if not f.startswith("_")]
        extra = [colmap[f] for f in fields if colmap[f] not in cols]
        rows = [{**r, **{c: None for c in extra}} for r in rows]
        cols += extra
    db_path = os.path.join(out_dir, "case", "scorecard.sqlite")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    db = sqlite3.connect(db_path)
    db.execute(f"CREATE TABLE {table} ({', '.join(c + ' NUMERIC' for c in cols)})")
    db.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(cols))})",
                   [[None if r[c] in ("", None) else r[c] for c in cols] for r in rows])
    db.commit()
    db.close()
    return SqliteWarehouse(db_path), rows


def contract_template(rows, cols):
    """A contract skeleton listing the scorecard's metrics, for the owner to fill in."""
    metrics = sorted({r[cols["metric"]] for r in rows})
    return {"experiment_id": rows[0][cols["experiment"]], "name": rows[0][cols["experiment"]], "design": "ab", "confirmed": False,
            "ci_level": 0.95, "hypothesis": "(what the change should do)",
            "primaries": {"rule": "any", "metrics": [{"metric": "(one of the metrics below)", "direction": "increase", "bar": None}]},
            "guardrails": [], "intended_params": [], "feature_metrics": [], "_metrics_in_the_scorecard": metrics,
            "_bars": "Take each bar from the test plan, not from the results: the smallest relative change that is worth "
                     "a launch (0.005 is 0.5%). Without a bar the call is not made. A bar of 0 means that any clear gain is "
                     "enough. For each guardrail, give the margin: the largest harm that you accept."}


def warehouse_from(spec, dialect="spark"):
    """The one place that turns --warehouse into a warehouse: sqlite:PATH at home; at work,
    cmd:COMMAND (the script of the team's Databricks skill) or handoff:DIR (the Databricks MCP,
    which only the chat agent can call). See sources/warehouse.py."""
    kind, _, target = spec.partition(":")
    if kind == "sqlite":
        return SqliteWarehouse(target)
    if kind == "cmd":
        return CommandWarehouse(target, dialect)
    if kind == "handoff":
        return HandoffWarehouse(target, dialect)
    raise ValueError(f"unknown warehouse '{kind}': use sqlite:PATH, cmd:COMMAND or handoff:DIR")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--experiment", help="name, config id or console URL (default: the only one in --scorecard)")
    parser.add_argument("--overlay", default=os.path.join(os.path.dirname(HERE), "overlay.example"))
    parser.add_argument("--warehouse", help="sqlite:PATH")
    parser.add_argument("--scorecard", help="a scorecard file (CSV or JSON rows in T1's shape) instead of a warehouse")
    parser.add_argument("--contract", help="the contract (pre-registration) JSON; default: <overlay>/contracts/<name>.json")
    parser.add_argument("--config", help="one canonical config JSON")
    parser.add_argument("--write-contract-template", help="with --scorecard: write a contract skeleton here and stop")
    parser.add_argument("--configs")
    parser.add_argument("--out", required=True)
    parser.add_argument("--knowledge")
    parser.add_argument("--cards")
    args = parser.parse_args(argv)
    try:
        overrides, experiment = None, args.experiment
        if args.scorecard:
            wh_cfg = load_json(os.path.join(args.overlay, "warehouse.json"))
            cols = t1src.columns(wh_cfg)
            warehouse, rows = scorecard_warehouse(args.scorecard, args.out, wh_cfg["tables"]["t1"])
            names = sorted({r[cols["experiment"]] for r in rows})
            if args.write_contract_template:
                write_json(args.write_contract_template, contract_template(rows, cols))
                print(f"run: wrote a contract skeleton for {names[0]} to {args.write_contract_template}")
                return 0
            experiment = experiment or (names[0] if len(names) == 1 else None)
            if not experiment:
                raise ValueError(f"the scorecard holds {len(names)} experiments; pass --experiment")
            overrides = {"approved": ["t1"]}  # a scorecard alone has no query-level table, so cuts are access gaps
        elif args.warehouse:
            warehouse = warehouse_from(args.warehouse)
        else:
            raise ValueError("pass --warehouse sqlite:PATH or --scorecard FILE")
        configs = cfgsrc.OneConfig(load_json(args.config)) if args.config else cfgsrc.JsonDirConfig(args.configs)
        result = run(experiment, args.overlay, warehouse, configs, args.out, args.knowledge, args.cards,
                     load_json(args.contract) if args.contract else None, overrides)
    except (resolver.Unresolved, FileNotFoundError, ValueError) as err:
        print(f"run: {err}", file=sys.stderr)
        return 2
    c = result["counts"]
    print(f"run: {result['run_id']}: {c['pairs']} pairs, grades {c['grades']}, {c['claims']} claims "
          f"({c['confirmed']} confirmed, {c['unsupported']} unsupported), {c['cuts_ran']} cuts ran, {c['access_gaps']} access gaps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
