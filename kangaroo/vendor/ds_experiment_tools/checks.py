"""Validity checks run before any metric is read. Each returns a dict
{check, status, summary, details}; status is pass, flag, fail or could_not_run.

They read the canonical scorecard and config (see adapters.py) and the
pre-registration. A fail blocks the call ("can't tell") unless the
pre-registration lists an explanation for it.
"""

import math
import os
import re
import sys
from datetime import date

import expstats

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                "ds-mode", "checks"))
from srm import chi_square_p  # noqa: E402

PASS, FLAG, FAIL, COULD_NOT_RUN = "pass", "flag", "fail", "could_not_run"
# Statsig health-check types and the check each one stands in for.
HEALTH = {"has_balanced_exposures": "sample_split", "pre_experimental_bias_check": "pre_period",
          "has_crossover": "crossover", "has_changed_parameter": "config_params",
          "exposures_data_is_behind": "data_freshness", "has_pulse_metrics": "data_freshness",
          "has_valid_unit_type": "config_unit"}
# Audit-log actions that change who is in the test or what they get.
DISRUPTIVE_EDITS = {"experiment_edit", "experiment_group_disable", "layer_parameters_edit", "layer_edit",
                    "experiment_overrides_edit", "experiment_update_subdimension_filter", "holdout_update"}
RESTARTS = {"experiment_restart", "restart_experiment_pulse"}


def result(check, status, summary, **details):
    return {"check": check, "status": status, "summary": summary, "details": details}


def _close(a, b, rel=0.02, abs_tol=1e-9):
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_tol)


def scorecard_invariants(metrics, analysis):
    """Catch a wrong column map: units, signs and intervals that don't add up.

    The relative and absolute changes must agree through the control mean (true
    for CUPED-adjusted deltas too); raw means must agree with the delta only
    when CUPED is off.
    """
    problems, checkable = [], 0
    for m in metrics:
        name, c, t = m["metric"], m.get("control_mean"), m.get("test_mean")
        rel, ab = m.get("rel_delta"), m.get("abs_delta")
        tol = abs(c or 0) * 2e-5 + 1e-12
        if c and rel is not None and (ab is not None or t is not None):
            checkable += 1
        for fixed, seq in ((m.get("rel_ci"), m.get("seq_rel_ci")), (m.get("abs_ci"), m.get("seq_abs_ci"))):
            if fixed and seq and (seq[1] - seq[0]) < 0.99 * (fixed[1] - fixed[0]):
                problems.append(f"{name}: the sequential interval is narrower than the fixed one, which sequential "
                                "testing never produces; the export's columns are probably swapped or mislabelled")
        if c and rel is not None and ab is not None and not _close(rel * c, ab, rel=0.05, abs_tol=tol):
            problems.append(f"{name}: relative {rel:g} x control {c:g} = {rel * c:g} but the absolute change is {ab:g}; "
                            "is a percent column read as a fraction, or the reverse?")
        rel_ci, abs_ci = m.get("rel_ci"), m.get("abs_ci")
        if c and rel_ci and abs_ci and not _close((rel_ci[1] - rel_ci[0]) * c, abs_ci[1] - abs_ci[0], rel=0.05, abs_tol=tol):
            problems.append(f"{name}: the relative and absolute intervals have different widths through the control mean")
        if c is not None and t is not None and ab is not None and analysis.get("cuped") is False:
            if not _close(t - c, ab, rel=0.05, abs_tol=tol):
                problems.append(f"{name}: test - control = {t - c:g} but the absolute change is {ab:g}")
        for point, ci, label in ((rel, rel_ci, "relative"), (ab, abs_ci, "absolute")):
            if point is not None and ci and not (min(ci) - 1e-12 <= point <= max(ci) + 1e-12):
                problems.append(f"{name}: the {label} interval {ci} doesn't contain its estimate {point:g}")
        if rel is not None and abs(rel) > 5:
            problems.append(f"{name}: a relative change of {rel:g} looks like percent read as a fraction")
    if problems:
        ratio = None
        for m in metrics:
            c, rel, ab = m.get("control_mean"), m.get("rel_delta"), m.get("abs_delta")
            if c and rel and ab:
                ratio = ab / (rel * c)
                break
        if ratio and (abs(ratio) > 20 or abs(ratio) < 0.05):
            factor = round(abs(ratio)) if abs(ratio) > 1 else round(1 / abs(ratio))
            summary = (f"the scorecard's relative and absolute changes disagree by about {factor} times, so a percent "
                       "column was probably read as a fraction or the reverse")
        else:
            summary = f"{len(problems)} scorecard values don't add up, starting with {problems[0].split(':')[0]}"
        return result("scorecard", FAIL, summary, problems=problems)
    if metrics and not checkable:
        return result("scorecard", COULD_NOT_RUN, "the export has no control means next to its changes, so a percent "
                      "column read as a fraction (or the reverse) can't be caught")
    return result("scorecard", PASS, f"{checkable} of {len(metrics)} registered metrics cross-checked "
                  "(relative, absolute and intervals agree)")


def sample_split(config, primary, alpha, explanations, warn_alpha=0.01, unit_fallback=True):
    """SRM on exposures if the config has them, else on the primary metric's units
    (unless unit_fallback is False: a primary whose denominator the treatment moves,
    such as queries or triggered users, would fail a good test).

    Two bands, as Statsig uses them: p below `alpha` (0.001) fails; p below
    `warn_alpha` (0.01) is flagged and listed as an open item without blocking.
    """
    health = _health(config, "sample_split")
    counts, source = None, None
    groups = (config or {}).get("groups") or []
    exposures = (config or {}).get("exposures") or {}
    active = [g for g in groups if not g.get("disabled")]
    if exposures and active and all(g.get("name") in exposures or g.get("id") in exposures for g in active):
        counts = [_count(exposures.get(g.get("name"), exposures.get(g.get("id")))) for g in active]
        source = "exposures"
    elif unit_fallback and primary and primary.get("control_units") and primary.get("test_units") and len(active) == 2:
        control_first = (active[0].get("id") == (config or {}).get("control_group")
                         or (active[0].get("name") or "").lower() == "control")
        units = [int(primary["control_units"]), int(primary["test_units"])]
        counts = units if control_first else units[::-1]
        source = f"units in '{primary['metric']}' (exposures not in the config)"
    if counts and active and all(g.get("size_pct") for g in active):
        total = sum(g["size_pct"] for g in active)
        ratios = [g["size_pct"] / total for g in active]
        stat, p = chi_square_p(counts, ratios)
        shares = " / ".join(f"{100 * c / sum(counts):.1f}%" for c in counts)
        planned = " / ".join(f"{100 * r:.1f}%" for r in ratios)
        details = {"p": p, "counts": counts, "source": source}
        if p < alpha:
            summary = (f"the arms got {shares} of {source.split(' (')[0]} against a planned {planned}, "
                       "a gap chance almost never produces at this size")
            explained = _explained(explanations, "sample_split")
            if explained:
                return result("sample_split", FLAG, f"{summary}; explained by '{explained}'", **details)
            return result("sample_split", FAIL, summary, **details)
        if health and health["status"] == "FAILED":
            return result("sample_split", FAIL, f"the platform's balance check failed (split {shares})", **details)
        if p < warn_alpha:
            return result("sample_split", FLAG, f"split {shares} against a planned {planned} is borderline; "
                          "check exposure logging before triage", warning=True, **details)
        return result("sample_split", PASS, f"split {shares} matches the planned {planned}", **details)
    if health:
        status = {"PASSED": PASS, "FAILED": FAIL, "WARNING": FLAG}.get(health["status"], COULD_NOT_RUN)
        return result("sample_split", status, f"platform balance check: {health['status']} (no counts to recompute)")
    return result("sample_split", COULD_NOT_RUN, "no exposures, unit counts or platform check to test the split")


def config_checks(config, prereg, as_of):
    """Parameters, unit, design and mid-flight edits."""
    out = []
    if not config:
        return [result("config", COULD_NOT_RUN, "no experiment config given")]
    groups = [g for g in config.get("groups", []) if not g.get("disabled")]
    control = next((g for g in groups if g.get("id") == config.get("control_group")), groups[0] if groups else None)
    intended = prereg.get("intended_params")
    if intended is None:
        out.append(result("config_params", COULD_NOT_RUN,
                          "the pre-registration doesn't list intended parameters, so unintended ones can't be found"))
    elif control:
        stray = {}
        for g in groups:
            if g is control:
                continue
            keys = set(g.get("parameters", {})) | set(control.get("parameters", {}))
            diff = sorted(k for k in keys if g.get("parameters", {}).get(k) != control.get("parameters", {}).get(k)
                          and k not in intended)
            if diff:
                stray[g.get("name")] = diff
        if stray:
            out.append(result("config_params", FAIL, f"parameters changed that the test didn't intend: {stray}", stray=stray))
        else:
            out.append(result("config_params", PASS, f"only the intended parameters differ ({', '.join(intended) or 'none'})"))
    unit = prereg.get("unit")
    if unit and config.get("id_type") and _unit_key(unit) != _unit_key(config["id_type"]):
        out.append(result("config_unit", FAIL, f"randomized on {config['id_type']} but pre-registered on {unit}"))
    edits = [e for e in config.get("edits", []) if e.get("date") and config.get("start")
             and config["start"] <= e["date"] <= (as_of or "9999")]
    order = lambda e: (e["date"], str(e.get("time") or ""), e.get("action") in RESTARTS)  # noqa: E731
    restarts = sorted((e for e in edits if e.get("action") in RESTARTS), key=order)
    window_start = restarts[-1]["date"] if restarts else config.get("start")
    disruptive = [e for e in edits if e.get("action") in DISRUPTIVE_EDITS
                  and (not restarts or order(e) > order(restarts[-1]))]
    if disruptive:
        first = disruptive[0]
        out.append(result("config_edits", FAIL, f"the test was edited mid-flight on {first['date']} "
                          f"({first.get('summary') or first['action']}) and the results include days before and after",
                          edits=disruptive))
    elif restarts:
        out.append(result("config_edits", FLAG, f"results count from the restart on {window_start}; earlier days excluded",
                          window_start=window_start))
    else:
        out.append(result("config_edits", PASS, "no mid-flight edits in the analysis window"))
    for health_check in config.get("health_checks", []):
        name = HEALTH.get(health_check.get("type"))
        if name in ("crossover", "data_freshness", "config_unit") and health_check.get("status") in ("FAILED", "WARNING"):
            status = FAIL if health_check["status"] == "FAILED" else FLAG
            out.append(result(name, status, f"platform check '{health_check.get('name') or health_check['type']}': "
                              f"{health_check['status']}"))
    return out


def pre_period(prereg, config, primary_name):
    """Did the arms differ before exposure? Uses a pre-period interval if given, else the platform check."""
    pre = (prereg.get("pre_period") or {}).get(primary_name)
    if pre and pre.get("rel_ci"):
        low, high = pre["rel_ci"]
        if low > 0 or high < 0:
            explained = _explained(prereg.get("explanations"), "pre_period")
            status = FLAG if explained else FAIL
            return result("pre_period", status, f"before exposure, {primary_name} already differed by "
                          f"{100 * pre.get('rel_delta', (low + high) / 2):+.2f}% ({100 * low:+.2f}% to {100 * high:+.2f}%)"
                          + (f"; explained by '{explained}'" if explained else ""))
        return result("pre_period", PASS, f"no difference before exposure ({100 * low:+.2f}% to {100 * high:+.2f}%)")
    health = _health(config, "pre_period")
    if health:
        status = {"PASSED": PASS, "FAILED": FAIL, "WARNING": FLAG}.get(health["status"], COULD_NOT_RUN)
        return result("pre_period", status, f"platform pre-experiment check: {health['status']}")
    return result("pre_period", COULD_NOT_RUN, "no pre-period interval or platform check")


def novelty(series, effect_sign, z_needed=2.0):
    """Is the effect fading? `series` is [{day, rel_delta, rel_ci, basis}] by days since first exposure."""
    series = [p for p in (series or []) if isinstance(p, dict) and p.get("rel_delta") is not None
              and p.get("rel_ci") and len(p["rel_ci"]) == 2 and p.get("day") is not None]
    if len(series) < 4:
        return result("novelty", COULD_NOT_RUN, "no usable daily effect series (needs 4 or more days)")
    basis = series[0].get("basis", "days_since_exposure")
    days = [p["day"] for p in series]
    effects = [p["rel_delta"] for p in series]
    ses = [max(expstats.se_from_ci(*p["rel_ci"]), 1e-9) for p in series]
    slope, slope_se = expstats.weighted_slope(days, effects, ses)
    third = max(1, len(series) // 3)
    late, late_se = expstats.pooled(effects[-third:], ses[-third:])
    early, _ = expstats.pooled(effects[:third], ses[:third])
    fading = effect_sign * slope < 0 and abs(slope) / slope_se >= z_needed and abs(late) < abs(early)
    summary = (f"effect by {basis.replace('_', ' ')}: first days {100 * early:+.2f}%, last days {100 * late:+.2f}% "
               f"(trend {100 * slope:+.3f}% per day)")
    if fading and basis != "days_since_exposure":
        return result("novelty", FLAG, summary + "; calendar days mix day-of-week and late joiners, so cut by days "
                      "since first exposure before calling it novelty", late=late, late_se=late_se, basis=basis)
    if fading:
        return result("novelty", FLAG, "effect fading: " + summary, late=late, late_se=late_se, basis=basis)
    return result("novelty", PASS, summary, late=late, late_se=late_se, basis=basis)


def _count(value):
    if isinstance(value, dict):
        value = value.get("count", value.get("units", value.get("exposures")))
    return int(value)


def _unit_key(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _health(config, name):
    for h in (config or {}).get("health_checks", []):
        if HEALTH.get(h.get("type")) == name:
            return h
    return None


def _explained(explanations, check):
    for item in explanations or []:
        if item.get("check") == check and item.get("cause") and item.get("evidence"):
            return item["cause"]
    return None


def days_between(start, end):
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    except (TypeError, ValueError):
        return None
