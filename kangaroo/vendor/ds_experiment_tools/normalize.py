#!/usr/bin/env python3
"""Normalize a scorecard export and an experiment config into scorecard.json and config.json.

    # Statsig Console API responses (FILE:GROUP when there are several test groups)
    python3 normalize.py --pulse pulse.json [--pulse pulse_b.json:g_test_b] --request request.json \
        [--experiment experiment.json] [--audit audit_logs.json] [--exposures exposures.json] [--out-dir DIR]

    # any CSV export, through a column map kept in the private overlay
    python3 normalize.py --csv export.csv --map scorecard_map.json [--request request.json] [--out-dir DIR]

request.json records what the results were requested with ({"control", "test",
"cuped", "confidence", ...}); the Pulse response doesn't echo it. Exit 0 on
success, 2 on input the adapter can't read (the message names what's missing).
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adapters  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pulse", action="append", help="a pulse_results response, as FILE or FILE:GROUP; repeat per test group")
    parser.add_argument("--csv")
    parser.add_argument("--map", help="column map for --csv")
    parser.add_argument("--request")
    parser.add_argument("--experiment")
    parser.add_argument("--audit")
    parser.add_argument("--exposures", help='{"<group name>": <exposed units>, ...}')
    parser.add_argument("--out-dir", default=".")
    args = parser.parse_args(argv)
    try:
        request = adapters.load_json(args.request) if args.request else {}
        if args.pulse:
            files, groups = [], []
            for item in args.pulse:
                path, _, group = item.partition(":") if not os.path.exists(item) else (item, "", "")
                files.append(path)
                groups.append(group or None)
            scorecard = adapters.statsig_pulse([adapters.load_json(p) for p in files], request,
                                               groups if any(groups) else None)
        elif args.csv and args.map:
            scorecard = adapters.mapped_csv(args.csv, adapters.load_json(args.map), request)
        else:
            parser.error("give --pulse, or --csv with --map")
        outputs = {"scorecard.json": scorecard}
        if args.experiment:
            config = adapters.statsig_config(adapters.load_json(args.experiment),
                                             adapters.load_json(args.audit) if args.audit else None)
            if args.exposures:
                config["exposures"] = adapters.load_json(args.exposures)
            outputs["config.json"] = config
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as err:
        print(f"normalize: {type(err).__name__}: {err}", file=sys.stderr)
        return 2
    os.makedirs(args.out_dir, exist_ok=True)
    for name, content in outputs.items():
        with open(os.path.join(args.out_dir, name), "w", encoding="utf-8") as handle:
            json.dump(content, handle, indent=1)
    metrics = sum(len(c["metrics"]) for c in scorecard["comparisons"])
    unknown = [k for k, v in scorecard["analysis"].items() if v is None]
    print(f"normalize: {metrics} metrics in {len(scorecard['comparisons'])} comparison(s) -> "
          f"{', '.join(outputs)} in {args.out_dir}")
    if unknown:
        print(f"normalize: not recorded in the request, so unknown: {', '.join(unknown)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
