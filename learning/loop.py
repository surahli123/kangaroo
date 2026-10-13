#!/usr/bin/env python3
"""The learning loop, one command per step. Every step prints counts only.

    python3 -m learning.loop freeze   --run RUN_DIR --cases CASES [--gold GOLD.json]
    python3 -m learning.loop propose  --runs RUNS_DIR --cases CASES --knowledge K --lessons LESSONS
    python3 -m learning.loop evaluate --lesson L.json --cases CASES --overlay O --knowledge K [--warehouse sqlite:PATH]
    python3 -m learning.loop approve  --lesson L.json --report R.json --approver NAME --knowledge K --lessons LESSONS \
        --cases CASES --overlay OVERLAY   (approval re-runs the evaluation on the cases)
    python3 -m learning.loop stamp    --run RUN_DIR --approver NAME --warns "power" ...
    python3 -m learning.loop confirm-card --cards CARDS --experiment NAME --cause TEXT --lesson TEXT --approver NAME
    python3 -m learning.loop stats    --runs RUNS_DIR

`propose` writes candidate lessons from reviews, unmapped metric names, and movements left
unexplained across flights. `evaluate` replays the frozen cases with and without the lesson
(its source flights held out) and writes the report next to the lesson. `approve` applies a
passed lesson after a person names themselves, and prints the new knowledge version.
"""

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from kangaroo import LIBRARY  # noqa: E402  (puts ds-experiment's tools on the path)

import explain  # noqa: E402

from learning import cards, evaluate, lessons  # noqa: E402
from kangaroo.sources.warehouse import SqliteWarehouse  # noqa: E402


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--run", required=True)
    f.add_argument("--cases", required=True)
    f.add_argument("--gold")
    p = sub.add_parser("propose")
    p.add_argument("--runs", required=True)
    p.add_argument("--cases", required=True)
    p.add_argument("--knowledge", required=True)
    p.add_argument("--lessons", required=True)
    e = sub.add_parser("evaluate")
    e.add_argument("--lesson", required=True)
    e.add_argument("--cases", required=True)
    e.add_argument("--overlay", required=True)
    e.add_argument("--knowledge", required=True)
    e.add_argument("--warehouse")
    a = sub.add_parser("approve")
    a.add_argument("--lesson", required=True)
    a.add_argument("--report", required=True)
    a.add_argument("--approver", required=True)
    a.add_argument("--knowledge", required=True)
    a.add_argument("--lessons", required=True)
    a.add_argument("--cases", required=True, help="the frozen cases: approval re-runs the evaluation on them")
    a.add_argument("--overlay", required=True)
    s = sub.add_parser("stamp")
    s.add_argument("--run", required=True)
    s.add_argument("--approver", required=True)
    s.add_argument("--warns", nargs="*", default=[])
    c = sub.add_parser("confirm-card")
    for flag in ("--cards", "--experiment", "--cause", "--lesson", "--approver"):
        c.add_argument(flag, required=True)
    st = sub.add_parser("stats")
    st.add_argument("--runs", required=True)
    args = parser.parse_args(argv)

    try:
        if args.cmd == "freeze":
            gold = None
            if args.gold:
                meta = load(os.path.join(args.run, "case", "meta.json"))
                gold = load(args.gold).get(meta["experiment"])
            dest = evaluate.freeze(args.run, args.cases, gold)
            print(f"freeze: case {os.path.basename(dest)} ({'with' if gold else 'no'} gold)")
        elif args.cmd == "propose":
            library = explain.load_library(args.knowledge, base=LIBRARY)
            found = []
            traces = [load(t) for t in glob.glob(os.path.join(args.runs, "*", "trace.json"))]
            for case in sorted(glob.glob(os.path.join(args.cases, "*", "scorecard.json"))):
                flight = os.path.basename(os.path.dirname(case))
                found += lessons.propose_mappings(load(case), library, flight)
                contract = load(os.path.join(os.path.dirname(case), "contract.json"))
                if contract:
                    found += lessons.propose_aliases(load(case), contract, flight,
                                                     load(os.path.join(args.knowledge, "metric_aliases.json")) or {})
            found += lessons.propose_patterns(traces)
            for review in glob.glob(os.path.join(args.runs, "*", "review.json")):
                r = load(review)
                if r.get("reviewer"):
                    flight = load(os.path.join(os.path.dirname(review), "trace.json"))["flight"]
                    found += lessons.from_review(r, flight)
            os.makedirs(os.path.join(args.lessons, "candidate"), exist_ok=True)
            merged = {}
            for lesson in found:  # one mapping proposal per (family, metric), with all its source flights
                key = lesson["id"]
                if key in merged:
                    merged[key]["source_flights"] = sorted(set(merged[key]["source_flights"]) | set(lesson["source_flights"]))
                else:
                    merged[key] = lesson
            for lesson in merged.values():
                save(os.path.join(args.lessons, "candidate", f"{lesson['id']}.json"), lesson)
            kinds = {}
            for lesson in merged.values():
                kinds[lesson["type"]] = kinds.get(lesson["type"], 0) + 1
            print(f"propose: {len(merged)} candidate lessons {kinds}")
        elif args.cmd == "evaluate":
            live = SqliteWarehouse(args.warehouse.partition(":")[2]) if args.warehouse else None
            lesson = load(args.lesson)
            report = evaluate.evaluate(lesson, args.cases, args.overlay, args.knowledge, live)
            out = args.lesson.replace(".json", ".report.json")
            save(out, report)
            print(f"evaluate: {lesson['id']}: {report['status']}; {len(report.get('cases', []))} held-out cases, "
                  f"{len(report.get('fit_cases', []))} fit cases, {len(report.get('regressions', []))} regressions, "
                  f"improved: {', '.join(report.get('improved', [])) or 'none'}")
        elif args.cmd == "approve":
            version = evaluate.approve(load(args.lesson), load(args.report), args.approver, args.knowledge, args.lessons,
                                       args.cases, args.overlay)
            print(f"approve: applied; knowledge version {version[:12]}")
        elif args.cmd == "stamp":
            stamp = evaluate.stamp_readout(args.run, args.approver, args.warns)
            print(f"stamp: approved {', '.join(stamp['pairs'])} by {stamp['approver']}")
        elif args.cmd == "confirm-card":
            card = cards.confirm(args.cards, args.experiment, args.cause, args.lesson, args.approver)
            print(f"confirm-card: {card['experiment_name']} confirmed")
        elif args.cmd == "stats":
            reviews = [load(r) for r in glob.glob(os.path.join(args.runs, "*", "review.json"))]
            traces = [load(t) for t in glob.glob(os.path.join(args.runs, "*", "trace.json"))]
            stats = lessons.review_stats(reviews)
            stats["unsupported_claims"] = sum(t["counts"]["unsupported"] for t in traces)
            proxies = [v for t in traces for v in t["counts"].get("next_check", {}).values() if v.get("has_next_check")]
            for key in ("scriptable", "runnable", "targets_top_question"):
                stats[f"next_check_{key}"] = f"{sum(1 for v in proxies if v.get(key))}/{len(proxies)}"
            print("stats: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    except (ValueError, FileNotFoundError, KeyError) as err:
        print(f"loop {args.cmd}: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
