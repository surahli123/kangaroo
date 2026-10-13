"""M0: setup and data trust, run on the data this run already fetched. No check waits for a
seal or a packet; a check that lacks its data says UNKNOWN and names what would fetch it.

Statuses: PASS, WARN, BLOCK, UNKNOWN. They map to ds-experiment's checks.py statuses
(pass, flag, fail, could_not_run). Every result names its source; a PASS without a source
is not allowed.

Checks are computed per (control, test) pair, because T1 is pairwise: one bad test arm
blocks itself, not its siblings.
"""

import checks as chk
import expstats
from decide import find

FROM_CORE = {"pass": "PASS", "flag": "WARN", "fail": "BLOCK", "could_not_run": "UNKNOWN"}
NAMES = {"M0-1": "config", "M0-2": "sample split", "M0-3": "metric coverage", "M0-4": "scorecard consistency",
         "M0-5": "power", "M0-6": "data quality", "duration": "duration", "stratification": "stratification"}
# the cuts that diagnose each check (actions carry `diagnoses` naming these checks)
DIAGNOSES = {"M0-2": ["exposed units by day and arm (T1 series)"], "M0-6": ["exposed units by day and arm (T1 series)"],
             "M0-3": ["metric pipeline job status for the missing metrics"], "M0-1": ["config diff between arms"]}


def result(cid, status, reason, source, affects_grade=True, **details):
    if status == "PASS" and not source:
        raise ValueError(f"{cid}: a PASS needs a source")
    return {"id": cid, "name": NAMES[cid], "status": status, "reason": reason, "source": source,
            "affects_grade": affects_grade, "diagnosed_by": DIAGNOSES.get(cid, []), "details": details}


def config_check(cfg, contract, arms, control, test, command):
    if not cfg:
        return result("M0-1", "UNKNOWN", "the config is not available", None, command=command)
    groups = {g.get("name"): g for g in cfg.get("groups", [])}
    groups.update({g.get("id"): g for g in cfg.get("groups", [])})
    missing = [a for a in (control, test) if a not in groups]
    if missing:
        return result("M0-1", "BLOCK", f"the config has no group for the scorecard arm {', '.join(missing)}", "config and T1")
    a, b = groups[control].get("parameters") or {}, groups[test].get("parameters") or {}
    diffs = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    intended = set(contract.get("intended_params") or [])
    if not diffs:
        return result("M0-1", "WARN", f"the config shows the same parameters for the {test} arm and the {control} arm", "config", diffs=diffs)
    if intended and set(diffs) - intended:
        extra = sorted(set(diffs) - intended)
        return result("M0-1", "BLOCK", f"the {test} arm also changes {', '.join(extra)}. The contract does not include this change", "config",
                      diffs=diffs)
    if not intended:
        return result("M0-1", "WARN", f"the contract gives no intended parameters. The {test} arm changes {', '.join(diffs)}", "config",
                      diffs=diffs)
    return result("M0-1", "PASS", f"the {test} arm changes only {', '.join(diffs)}, as the contract says", "config", diffs=diffs)


def split_check(comparison, cfg, contract, control, test, std, split_metric):
    groups = {g.get("name"): g for g in (cfg or {}).get("groups", [])}
    sizes = None
    if control in groups and test in groups and groups[control].get("size") and groups[test].get("size"):
        sizes = [groups[control]["size"], groups[test]["size"]]
    elif (contract.get("planned_split") or {}).get(test):
        sizes = [contract["planned_split"].get(control, 1), contract["planned_split"][test]]
    m = find(comparison["metrics"], split_metric) if split_metric else None
    if m and m.get("control_units") and m.get("test_units") and sizes:
        counts = [int(m["control_units"]), int(m["test_units"])]
        _, p = chk.chi_square_p(counts, [sizes[0] / sum(sizes), sizes[1] / sum(sizes)])
        share = f"the {test} arm has {100 * counts[1] / sum(counts):.1f}% of the exposed units. The plan is {100 * sizes[1] / sum(sizes):.1f}%"
        status = "BLOCK" if p < std.get("srm_alpha", 0.001) else "WARN" if p < std.get("srm_warn_alpha", 0.01) else "PASS"
        return result("M0-2", status, share + (f" (p = {p:.2f})" if p >= 0.01 else f" (p = {p:.1e})"), f"T1 '{m['metric']}' units",
                      p=p, counts=counts, planned_test_share=sizes[1] / sum(sizes))
    vendor = next((h for h in (cfg or {}).get("health_checks", []) if "balanced" in str(h.get("name", "")).lower()), None)
    if vendor:
        status = {"PASSED": "PASS", "WARNING": "WARN", "FAILED": "BLOCK"}.get(str(vendor.get("status")).upper(), "UNKNOWN")
        return result("M0-2", status, f"the vendor balance check gives {vendor.get('status')}", "vendor health check")
    why = ("the overlay names no metric that counts all exposed units" if not split_metric else
           f"'{split_metric}' is not in the scorecard" if not m else "the planned split is not known (no config)")
    return result("M0-2", "UNKNOWN", f"the sample split is not tested: {why}", None)


def coverage_check(comparison, contract):
    specs = ((contract.get("primaries") or {}).get("metrics") or []) + (contract.get("guardrails") or [])
    missing, empty, unfilled = [], [], []
    for s in specs:
        m = find(comparison["metrics"], s["metric"])
        if not m:
            missing.append(s["metric"])
        elif m.get("control_units") is None or m.get("test_units") is None:
            unfilled.append(s["metric"])  # the scorecard doesn't fill units for it: unknown, not zero
        elif not m["control_units"] or not m["test_units"]:
            empty.append(s["metric"])
    if empty:
        return result("M0-3", "BLOCK", f"{', '.join(empty)} has zero units in one arm", "T1", empty=empty, missing=missing)
    if unfilled and not missing:
        return result("M0-3", "UNKNOWN", f"the scorecard has no unit counts for {', '.join(unfilled)}, so coverage is not known", None, missing=[], unfilled=unfilled)
    if missing:
        return result("M0-3", "UNKNOWN", f"{', '.join(missing)} {'are' if len(missing) > 1 else 'is'} not in the scorecard", None, missing=missing)
    return result("M0-3", "PASS", f"all {len(specs)} contract metrics are in the scorecard, with units in both arms", "T1")


def consistency_check(comparison, contract, analysis):
    specs = ((contract.get("primaries") or {}).get("metrics") or []) + (contract.get("guardrails") or [])
    registered = [m for m in (find(comparison["metrics"], s["metric"]) for s in specs) if m]
    names = [m["metric"].lower() for m in comparison["metrics"]]
    dupes = sorted({n for n in names if names.count(n) > 1} | set(comparison.get("duplicates") or []))
    core = chk.scorecard_invariants(registered, analysis)
    status = FROM_CORE[core["status"]]
    reason = core["summary"]
    if status == "PASS":
        reason = f"the relative change, the absolute change and the interval agree for {len(registered)} contract metrics"
    if dupes and status == "PASS":
        status, reason = "WARN", (f"the snapshot has two rows for {len(dupes)} metric name{'s' if len(dupes) > 1 else ''} "
                                  f"({', '.join(dupes[:3])}). Kangaroo uses the row with a p-value. Make sure that the "
                                  f"scorecard shows the same row")
    return result("M0-4", status, reason, "T1 snapshot" if status != "UNKNOWN" else None, dupes=dupes)


def power_check(comparison, contract, level=0.95):
    specs = (contract.get("primaries") or {}).get("metrics") or []
    weak, measured, details = [], 0, []
    for s in specs:
        m = find(comparison["metrics"], s["metric"])
        if not m or not m.get("rel_ci") or not s.get("bar") or s.get("bar_unit") == "absolute":
            continue
        measured += 1
        mde = expstats.achieved_mde(expstats.se_from_ci(m["rel_ci"][0], m["rel_ci"][1], level))
        if mde > 2 * s["bar"]:
            weak.append(f"{s['metric']} {100 * mde:.2f}% (bar {100 * s['bar']:.2f}%)")
            details.append({"metric": s["metric"], "mde": mde, "bar": s["bar"]})
    if not measured:
        return result("M0-5", "UNKNOWN", "power is not known: no primary has an interval and a relative bar", None)
    if weak:
        return result("M0-5", "WARN", f"{len(weak)} of {measured} primaries can detect only changes larger than 2 times "
                      f"their bar. Smallest change that each can detect: {'; '.join(weak)}", "T1 intervals", weak=details)
    return result("M0-5", "PASS", f"{measured} primaries can detect a change at their bars", "T1 intervals")


def quality_check(series, test, split_metric):
    """Units by day for one metric: the exposed-unit count if named, else one primary (mixing
    metrics would compare one metric's units with another's)."""
    rows = [r for r in series or [] if r["test_group"] == test and split_metric
            and str(r["metric"]).lower() == split_metric.lower()]
    if len(rows) < 3:
        return result("M0-6", "UNKNOWN", "data quality is not known: the scorecard has fewer than 3 daily snapshots", None)
    rows.sort(key=lambda r: str(r["snapshot"]))
    problems = []
    for arm in ("control_units", "test_units"):
        values = [r[arm] for r in rows if r[arm] is not None]
        steps = [b - a for a, b in zip(values, values[1:])]
        if any(s < 0 for s in steps):
            problems.append(f"{arm.split('_')[0]} units decrease between snapshots")
        positive = sorted(s for s in steps if s > 0)
        if positive:
            median = positive[len(positive) // 2]
            if any(s > 4 * median and s > 50 for s in steps[1:]):
                problems.append(f"{arm.split('_')[0]} units increase suddenly on one day")
    if problems:
        return result("M0-6", "WARN", "; ".join(problems), "T1 daily series")
    return result("M0-6", "PASS", f"{len(rows)} daily snapshots. Units increase steadily in both arms", "T1 daily series")


def duration_check(cfg, contract, as_of):
    end = (cfg or {}).get("end")
    if not end and contract.get("planned", {}).get("end"):
        end = contract["planned"]["end"]
    if not end:
        return result("duration", "UNKNOWN", "the planned end date is not known. The config and the contract have no end date", None), None
    mid = str(as_of) < str(end)
    return result("duration", "PASS", f"the snapshot ({as_of}) is {'before' if mid else 'on or after'} the planned end ({end})",
                  "config end date", as_of=as_of, end=end), mid


def stratification_check(contract):
    declared = contract.get("stratification")
    if declared:
        return result("stratification", "PASS", f"the contract gives the stratification: {declared}", "contract", affects_grade=False)
    return result("stratification", "UNKNOWN", "the contract gives no stratification", None, affects_grade=False)


def run_m0(scorecard, series, cfg, contract, std, overlay_cfg, command):
    """{test group: [checks]} plus {test group: mid_flight}."""
    out, mid = {}, {}
    control = scorecard["control_group"]
    split_metric = overlay_cfg.get("split_count_metric")
    lead = next((m["metric"] for m in (contract.get("primaries") or {}).get("metrics") or []), None)
    for comparison in scorecard["comparisons"]:
        test = comparison["test_group"]
        checks = [config_check(cfg, contract, None, control, test, command),
                  split_check(comparison, cfg, contract, control, test, std, split_metric),
                  coverage_check(comparison, contract),
                  consistency_check(comparison, contract, scorecard["analysis"]),
                  power_check(comparison, contract),
                  quality_check(series, test, split_metric or lead)]
        dur, is_mid = duration_check(cfg, contract, scorecard["analysis"]["as_of"])
        checks += [dur, stratification_check(contract)]
        out[test], mid[test] = checks, is_mid
    return out, mid
