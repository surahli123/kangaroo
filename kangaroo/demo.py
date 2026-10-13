#!/usr/bin/env python3
"""The whole loop on synthetic data, in one command:

    python3 -m kangaroo.demo --out /tmp/kangaroo-demo

1. Build a synthetic warehouse (9 flights with planted mechanisms).
2. Past flights: run five, freeze them as cases, and have a person confirm the card of the
   first 3P flight (what happened and the lesson).
3. Learn: a reviewer flags a past flight whose mechanism the agent missed (its placement
   metric has another name). The agent proposes a mapping lesson from that flight alone; the
   evaluation holds that flight out and must show the lesson helping another past flight with
   no regression anywhere. A person approves it.
4. A new experiment arrives as a scorecard file (CSV) with its contract and config. Run it:
   the report says whether it can go to triage, what the results say, what to do first, and
   cites the confirmed similar experiment.
5. The same experiment against the warehouse, where the deep-dive cuts can run.

Writes DEMO.md in --out with each step's outcome and the reports' paths.
"""

import argparse
import csv
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "synth"))

import make_warehouse  # noqa: E402

from learning import cards, evaluate, knowledge, lessons  # noqa: E402
from kangaroo import LIBRARY  # noqa: E402
from kangaroo import run as runner  # noqa: E402
from kangaroo.sources import config as cfgsrc  # noqa: E402
from kangaroo.sources.warehouse import SqliteWarehouse  # noqa: E402

PAST = ["synth-3p-boost", "synth-3p-boost-v2", "synth-3p-pin", "synth-typeahead-regress", "synth-null", "synth-split-loss"]
REVIEWED = "synth-3p-boost-v2"  # the flight a reviewer flagged: its mechanism was missed
NEW = "synth-3p-carousel"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = os.path.abspath(args.out)
    if os.path.exists(out):
        shutil.rmtree(out)
    synth, overlay, k, card_dir, cases, lessons_dir, runs = (os.path.join(out, x) for x in (
        "synth", "overlay", "knowledge", "cards", "cases", "lessons", "runs"))
    for d in (k, card_dir, cases, lessons_dir, runs):
        os.makedirs(d)
    log = ["# Demo: one closed loop on synthetic data", ""]

    make_warehouse.main(["--out", synth])
    shutil.copytree(os.path.join(ROOT, "overlay.example"), overlay, ignore=shutil.ignore_patterns("contracts"))
    shutil.copytree(os.path.join(synth, "contracts"), os.path.join(overlay, "contracts"))
    gold = json.load(open(os.path.join(synth, "gold.json")))
    db = os.path.join(synth, "warehouse.sqlite")
    configs = cfgsrc.JsonDirConfig(os.path.join(synth, "configs"))
    log += ["1. Built a synthetic warehouse with 9 flights.", ""]

    log += ["2. Past flights, run and frozen as cases:", ""]
    for name in PAST:
        result = runner.run(name, overlay, SqliteWarehouse(db), configs, os.path.join(runs, name), k, card_dir)
        evaluate.freeze(os.path.join(runs, name), cases, gold[name])
        o = evaluate.outcome(result)
        log.append(f"   - {name}: grades {sorted({p['grade'] for p in o.values()})}, mechanism "
                   f"{'found' if evaluate.gold_score(o, gold[name]) == 2 else 'missed'} (planted: {gold[name]['mechanism']}).")
    cards.confirm(card_dir, "synth-3p-boost",
                  "The 3P offset moved 3P results to rank 1 on queries with a 3P candidate. 1P long clicks decreased on "
                  "those queries. Total success did not change.",
                  "Read 1P long clicks on 3P-eligible queries before total success. The total rate makes the loss look smaller.",
                  "demo-owner")
    log += ["", "   A person confirmed the card for synth-3p-boost (cause and lesson).", ""]

    import explain
    library = explain.load_library(k, base=LIBRARY)
    with open(os.path.join(cases, REVIEWED, "scorecard.json"), encoding="utf-8") as handle:
        proposed = lessons.propose_mappings(json.load(handle), library, REVIEWED)
    lesson = proposed[0]
    before = knowledge.version_hash(k)
    report = evaluate.evaluate(lesson, cases, overlay, k, SqliteWarehouse(db))
    log += [f"3. Learn. Proposed: map '{lesson['change']['metric']}' to the family {lesson['change']['family']} "
            f"(from {', '.join(lesson['source_flights'])}).",
            f"   Evaluation on {len(report['cases'])} held-out cases: {report['status']}; improved "
            f"{', '.join(report['improved']) or 'none'}; {len(report['regressions'])} regressions.", ""]
    after = evaluate.approve(lesson, report, "demo-owner", k, lessons_dir)
    log += [f"   Approved by demo-owner. Knowledge version {before[:12]} -> {after[:12]}.", ""]

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM t1_scorecard WHERE experiment_name = ?", (NEW,))]
    con.close()
    given = os.path.join(out, "given")
    os.makedirs(given)
    with open(os.path.join(given, "scorecard.csv"), "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    cfg = next(c for c in configs.by_id.values() if c["experiment_name"] == NEW)
    shutil.copy(os.path.join(synth, "contracts", f"{NEW}.json"), os.path.join(given, "contract.json"))
    json.dump(cfg, open(os.path.join(given, "config.json"), "w"), indent=1)
    sc_out = os.path.join(runs, f"{NEW}-from-scorecard")
    runner.main(["--scorecard", os.path.join(given, "scorecard.csv"), "--contract", os.path.join(given, "contract.json"),
                 "--config", os.path.join(given, "config.json"), "--overlay", overlay, "--knowledge", k,
                 "--cards", card_dir, "--out", sc_out])
    log += [f"4. A new experiment arrived as a scorecard file ({len(rows)} rows): given/scorecard.csv, with its contract "
            "and config.", f"   Report: runs/{NEW}-from-scorecard/readout.md", ""]

    wh_out = os.path.join(runs, f"{NEW}-with-warehouse")
    result = runner.run(NEW, overlay, SqliteWarehouse(db), configs, wh_out, k, card_dir)
    score = evaluate.gold_score(evaluate.outcome(result), gold[NEW])
    log += [f"5. The same experiment with the warehouse: {result['counts']['cuts_ran']} deep-dive cuts ran; the planted "
            f"mechanism is {'confirmed' if score == 2 else 'likely' if score == 1 else 'missed'}.",
            f"   Report: runs/{NEW}-with-warehouse/readout.md", ""]
    with open(os.path.join(out, "DEMO.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(log))
    print("\n".join(log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
