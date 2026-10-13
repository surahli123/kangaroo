"""One command line for everything a person does, the same functions the MCP tools call.

    python3 -m kangaroo init
    python3 -m kangaroo analyze --scorecard S.csv [--contract C.json] [--config CFG.json]
    python3 -m kangaroo analyze --experiment NAME --warehouse sqlite:PATH [--configs DIR]
    python3 -m kangaroo contract-template --scorecard S.csv --out contract.json
    python3 -m kangaroo check-report [--run latest]   (the writing rules on a report)
    python3 -m kangaroo dry-run --out DIR      (each cut template's SQL with LIMIT 0, for a compile check)
    python3 -m kangaroo report [--run latest] [--part summary|details|all]
    python3 -m kangaroo why METRIC [--run latest]
    python3 -m kangaroo review correct|wrong|missing [--claim c2 | --metric NAME] [--note TEXT] [--reviewer NAME]
    python3 -m kangaroo lessons propose | list | evaluate ID [--warehouse sqlite:PATH] | approve ID --approver NAME
    python3 -m kangaroo approve-report --approver NAME [--warns power ...]
    python3 -m kangaroo cards list | confirm EXPERIMENT --cause TEXT --lesson TEXT --approver NAME
    python3 -m kangaroo runs
    python3 -m kangaroo setup [claude|codex|cursor] [--mcp]   (how to use it from a chat host)
    python3 -m kangaroo install-skill DIR    (copy the skill, with this folder's paths, into DIR)
    python3 -m kangaroo mcp            (the stdio MCP server; experimental, see experimental/mcp)
    python3 -m kangaroo demo --out DIR (the whole loop on synthetic data)

The workspace is KANGAROO_HOME (default ~/.kangaroo), or --home.
"""

import argparse
import sys

from kangaroo import api


def main(argv=None):
    p = argparse.ArgumentParser(prog="kangaroo", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    a = sub.add_parser("analyze")
    for flag in ("--experiment", "--scorecard", "--contract", "--config", "--warehouse", "--configs"):
        a.add_argument(flag)
    cr = sub.add_parser("check-report")
    cr.add_argument("--run", default="latest")
    dr = sub.add_parser("dry-run")
    dr.add_argument("--out", required=True)
    ct = sub.add_parser("contract-template")
    ct.add_argument("--scorecard", required=True)
    ct.add_argument("--out", required=True)
    r = sub.add_parser("report")
    r.add_argument("--run", default="latest")
    r.add_argument("--part", default="summary", choices=["summary", "details", "all"])
    w = sub.add_parser("why")
    w.add_argument("metric")
    w.add_argument("--run", default="latest")
    v = sub.add_parser("review")
    v.add_argument("verdict", choices=["correct", "wrong", "missing"])
    v.add_argument("--claim")
    v.add_argument("--metric")
    v.add_argument("--note", default="")
    v.add_argument("--reviewer")
    v.add_argument("--run", default="latest")
    le = sub.add_parser("lessons")
    le.add_argument("action", choices=["propose", "list", "evaluate", "approve"])
    le.add_argument("lesson_id", nargs="?")
    le.add_argument("--warehouse")
    le.add_argument("--approver")
    ar = sub.add_parser("approve-report")
    ar.add_argument("--approver", required=True)
    ar.add_argument("--warns", nargs="*", default=[])
    ar.add_argument("--run", default="latest")
    c = sub.add_parser("cards")
    c.add_argument("action", choices=["list", "confirm"])
    c.add_argument("experiment", nargs="?")
    c.add_argument("--cause")
    c.add_argument("--lesson")
    c.add_argument("--approver")
    sub.add_parser("runs")
    su = sub.add_parser("setup")
    su.add_argument("host", nargs="?", choices=["claude", "codex", "cursor"])
    su.add_argument("--mcp", action="store_true")
    sk = sub.add_parser("install-skill")
    sk.add_argument("dest")
    sub.add_parser("mcp")
    d = sub.add_parser("demo")
    d.add_argument("--out", required=True)
    args = p.parse_args(argv)
    h = args.home
    try:
        if args.cmd == "mcp":
            from kangaroo import mcp_server
            return mcp_server.main()
        if args.cmd == "demo":
            from kangaroo import demo
            return demo.main(["--out", args.out])
        if args.cmd == "init":
            out = api.init(h)
        elif args.cmd == "analyze":
            out = api.analyze(args.experiment, args.scorecard, args.contract, args.config, args.warehouse, args.configs, h)
        elif args.cmd == "check-report":
            out = api.check_report(args.run, h)
        elif args.cmd == "dry-run":
            out = api.dry_run(args.out, h)
        elif args.cmd == "contract-template":
            out = api.contract_template(args.scorecard, args.out, h)
        elif args.cmd == "report":
            out = api.report(args.run, args.part, h)
        elif args.cmd == "why":
            out = api.why(args.metric, args.run, h)
        elif args.cmd == "review":
            out = api.review(args.verdict, args.claim, args.metric, args.note, args.reviewer, args.run, h)
        elif args.cmd == "lessons":
            out = {"propose": lambda: api.propose(h), "list": lambda: api.list_lessons(h),
                   "evaluate": lambda: api.evaluate_lesson(args.lesson_id, args.warehouse, h),
                   "approve": lambda: api.approve_lesson(args.lesson_id, args.approver, h)}[args.action]()
        elif args.cmd == "approve-report":
            out = api.approve_report(args.approver, args.warns, args.run, h)
        elif args.cmd == "cards":
            out = api.list_cards(h) if args.action == "list" else api.confirm_card(args.experiment, args.cause, args.lesson,
                                                                                 args.approver, h)
        elif args.cmd == "setup":
            from kangaroo import hosts
            out = hosts.setup(args.host, h, args.mcp)
        elif args.cmd == "install-skill":
            from kangaroo import hosts
            out = hosts.install_skill(args.dest)
        else:
            out = api.list_runs(h)
    except (ValueError, FileNotFoundError, KeyError) as err:
        print(f"{args.cmd}: {err}", file=sys.stderr)
        return 2
    print(out["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
