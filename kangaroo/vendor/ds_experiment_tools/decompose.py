"""Deterministic decompositions that explain where a metric movement comes from.
Standard library only; they read summary numbers, never raw rows.

ratio(numerator, denominator)
    A ratio metric's relative change splits into its parts:
    log(1 + r_ratio) = log(1 + r_num) - log(1 + r_den).
    Tells whether CTR rose because clicks rose or because searches fell.

segments(overall, parts)
    Each segment's contribution to the overall change is its share of the
    control metric total times its own relative change. Cochran's Q tests
    whether the segment effects differ more than noise allows, and each
    segment is compared with the pooled effect at a Bonferroni level.
"""

import math
from statistics import NormalDist

import expstats

NORMAL = NormalDist()


def _log_part(rel, ci, level):
    """log(1 + r) and its standard error from a relative change and its interval."""
    if rel is None or rel <= -1:
        raise ValueError("a relative change must be above -100%")
    se = expstats.se_from_ci(ci[0], ci[1], level) / (1 + rel) if ci else None
    return math.log1p(rel), se


def ratio(numerator, denominator, level=0.95):
    """numerator/denominator: {"metric", "rel_delta", "rel_ci"} for the per-unit parts of a ratio."""
    log_num, se_num = _log_part(numerator["rel_delta"], numerator.get("rel_ci"), level)
    log_den, se_den = _log_part(denominator["rel_delta"], denominator.get("rel_ci"), level)
    log_ratio = log_num - log_den
    total = abs(log_num) + abs(log_den)
    out = {"ratio_rel_delta": math.expm1(log_ratio),
           "numerator": {"metric": numerator["metric"], "rel_delta": numerator["rel_delta"], "log_part": log_num},
           "denominator": {"metric": denominator["metric"], "rel_delta": denominator["rel_delta"], "log_part": -log_den},
           "denominator_share": abs(log_den) / total if total else 0.0}
    for part, se in (("numerator", se_num), ("denominator", se_den)):
        if se:
            out[part]["clear_of_zero"] = abs(out[part]["log_part"]) > expstats.z_for(level) * se
    num_moved = out["numerator"].get("clear_of_zero", False)
    den_moved = out["denominator"].get("clear_of_zero", False)
    if den_moved and not num_moved:
        out["driver"] = "denominator"
    elif num_moved and not den_moved:
        out["driver"] = "numerator"
    elif num_moved and den_moved:
        out["driver"] = "both"
    else:
        out["driver"] = "neither"
    return out


def segments(overall, parts, level=0.95):
    """overall: {"rel_delta", "rel_ci"}; parts: [{"segment", "rel_delta", "rel_ci", "weight"}] where weight is
    the segment's share of the control metric total (or of units when that is all there is)."""
    usable = [p for p in parts if p.get("rel_ci") and p.get("rel_delta") is not None]
    if len(usable) < 2:
        raise ValueError("need at least two segments with intervals")
    ses = [max(expstats.se_from_ci(p["rel_ci"][0], p["rel_ci"][1], level), 1e-12) for p in usable]
    pooled, pooled_se = expstats.pooled([p["rel_delta"] for p in usable], ses)
    q = sum(((p["rel_delta"] - pooled) / se) ** 2 for p, se in zip(usable, ses))
    df = len(usable) - 1
    p_q = _chi2_sf(q, df)
    total_weight = sum(p.get("weight") or 0 for p in usable)
    z_seg = NORMAL.inv_cdf(1 - (1 - level) / (2 * len(usable)))
    rows, explained = [], 0.0
    for p, se in zip(usable, ses):
        weight = (p.get("weight") or 0) / total_weight if total_weight else None
        contribution = weight * p["rel_delta"] if weight is not None else None
        explained += contribution or 0
        differs = abs(p["rel_delta"] - pooled) > z_seg * math.sqrt(se ** 2 + pooled_se ** 2)
        rows.append({"segment": p["segment"], "rel_delta": p["rel_delta"], "rel_ci": p["rel_ci"], "weight": weight,
                     "contribution": contribution, "differs_from_pooled": differs,
                     "clear_of_zero": p["rel_ci"][0] > 0 or p["rel_ci"][1] < 0})
    overall_rel = overall.get("rel_delta")
    for r in rows:
        r["share_of_overall"] = (r["contribution"] / overall_rel if r["contribution"] is not None and overall_rel
                                 else None)
    rows.sort(key=lambda r: -abs(r["contribution"] or 0))
    return {"pooled": pooled, "heterogeneity_q": q, "heterogeneity_df": df, "heterogeneity_p": p_q,
            "heterogeneous": p_q < 0.01, "sum_of_contributions": explained, "segments": rows}


def _chi2_sf(x, df):
    """Upper tail of the chi-square distribution (regularized gamma Q), via the srm check's helper."""
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                    "ds-mode", "checks"))
    from srm import _gamma_q
    return _gamma_q(df / 2, x / 2)
