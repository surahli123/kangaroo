#!/usr/bin/env python3
"""Build a synthetic enterprise-search warehouse for the agent, in the shapes the work
warehouse has (aliases T1 to T4), with mechanisms planted in the query rows.

    python3 make_warehouse.py --out synth_out/ [--scale 1.0] [--seed 7]

Writes synth_out/warehouse.sqlite and synth_out/configs/<uuid>.json, plus
synth_out/contracts/<experiment>.json (the pre-registration) and synth_out/gold.json
(what was planted, for the evaluation only; the agent never reads it).

Tables:
- t1_scorecard: one row per experiment, metric, control arm, test arm and daily snapshot (ds),
  cumulative from the start. relative_* columns are percents, as at work. Computed from t3, so
  a deep-dive cut on t3 ties to t1.
- exposures: the current assignment source (unit, experiment, group, first exposure date).
- t2_first_exposure: the retired assignment table; it holds nothing for flights after May 2026.
- t3_queries: one row per query: user, tenant, surface (experience), and click and success flags.
- t4_tenants: tenant enablement, with the paying cohort.

Only the standard library is used, so it runs on the work laptop's Python 3.14 too.
"""

import argparse
import json
import math
import os
import random
import sqlite3
import uuid
from datetime import date, timedelta
from statistics import NormalDist

NORMAL = NormalDist()
START = date(2026, 9, 21)

BASE = {
    "nav_share": 0.40,        # known-item intents
    "ta_hit_nav": 0.60,       # typeahead resolves a known-item intent
    "ta_hit_info": 0.05,
    "eligible_given_auth": 0.60,  # a 3P candidate exists (before treatment)
    "top1_3p_eligible": 0.15,
    "p3_click_top": 0.35, "p3_click_low": 0.08, "p3_long_given_click": 0.60,
    "p1_long_nav": 0.55, "p1_long_info": 0.40, "p1_long_displaced_factor": 0.45,
    "p1_short": 0.18, "short_success": 0.30,
    "answer_shown_info": 0.25, "answer_shown_nav": 0.05, "answer_success": 0.45,
    "answer_suppresses_1p": 0.0,
    "latency_ms": 400.0,
    "requery_auth": 0.0,      # extra low-success full-page queries for authorized users
    "exposure_loss": 0.0,     # share of low-activity users whose exposure is never logged
}

# Flights: arms are {name: overrides}. The first arm is control.
FLIGHTS = [
    {"name": "synth-3p-boost", "display": "3P boost, three offsets", "layer": "l2-ranking", "tags": ["3p", "interleaver"],
     "days": 14, "as_of_day": 14, "users_per_arm": 5000,
     "arms": {"control": {}, "boost_10": {"top1_3p_eligible": 0.35}, "boost_15": {"top1_3p_eligible": 0.45},
              "boost_20": {"top1_3p_eligible": 0.55}},
     "params": {"ranker": "l2_v7", "p3_offset": {"control": 0.0, "boost_10": 0.10, "boost_15": 0.15, "boost_20": 0.20}},
     "intended": ["p3_offset"], "hypothesis": "A 3P score offset puts more 3P results on top and raises 3P clicks.",
     "feature_metrics": ["3P Top1 Share"],
     "gold": {"mechanism": "first_party_displaced", "metric": "1P Long Click Rate", "grade": "decision_grade"}},
    {"name": "synth-3p-boost-mid", "display": "3P boost, mid-flight", "layer": "l2-ranking", "tags": ["3p", "interleaver"],
     "days": 14, "as_of_day": 7, "users_per_arm": 5000, "drop_metrics": ["Search Success Rate, Position Weighted"],
     "arms": {"control": {}, "boost_15": {"top1_3p_eligible": 0.45}},
     "params": {"ranker": "l2_v7", "p3_offset": {"control": 0.0, "boost_15": 0.15}},
     "intended": ["p3_offset"], "hypothesis": "A 3P score offset puts more 3P results on top and raises 3P clicks.",
     "feature_metrics": ["3P Top1 Share"],
     "gold": {"mechanism": "first_party_displaced", "metric": "1P Long Click Rate", "grade": "directional_only"}},
    {"name": "synth-3p-boost-v2", "display": "3P boost, second ranker", "layer": "l2-ranking", "tags": ["3p", "interleaver"],
     "days": 14, "as_of_day": 14, "users_per_arm": 5000, "rename": {"3P Top1 Share": "3P Top-1 Placement Rate"},
     "arms": {"control": {}, "boost": {"top1_3p_eligible": 0.50}},
     "params": {"ranker": "l2_v8", "p3_offset": {"control": 0.0, "boost": 0.18}},
     "intended": ["p3_offset"], "hypothesis": "A 3P offset on the new ranker raises 3P clicks.",
     "feature_metrics": ["3P Top-1 Placement Rate"],
     "gold": {"mechanism": "first_party_displaced", "metric": "1P Long Click Rate", "grade": "decision_grade"}},
    {"name": "synth-3p-pin", "display": "Pin connector results for people queries", "layer": "l2-ranking",
     "tags": ["3p", "interleaver"], "days": 14, "as_of_day": 14, "users_per_arm": 5000,
     "rename": {"3P Top1 Share": "3P Top-1 Placement Rate"},
     "arms": {"control": {}, "pin": {"top1_3p_eligible": 0.55}},
     "params": {"ranker": "l2_v8", "pin_3p": {"control": False, "pin": True}},
     "intended": ["pin_3p"], "hypothesis": "Pinning connector results raises 3P clicks.",
     "feature_metrics": ["3P Top-1 Placement Rate"],
     "gold": {"mechanism": "first_party_displaced", "metric": "1P Long Click Rate", "grade": "decision_grade"}},
    {"name": "synth-3p-carousel", "display": "Connector results in a top carousel", "layer": "l2-ranking",
     "tags": ["3p", "interleaver"], "days": 14, "as_of_day": 14, "users_per_arm": 5000,
     "rename": {"3P Top1 Share": "3P Top-1 Placement Rate"},
     "arms": {"control": {}, "carousel": {"top1_3p_eligible": 0.48}},
     "params": {"ranker": "l2_v8", "p3_carousel": {"control": "off", "carousel": "top"}},
     "intended": ["p3_carousel"], "hypothesis": "A carousel of connector results at the top raises 3P clicks.",
     "feature_metrics": ["3P Top-1 Placement Rate"],
     "gold": {"mechanism": "first_party_displaced", "metric": "1P Long Click Rate", "grade": "decision_grade"}},
    {"name": "synth-typeahead-regress", "display": "Typeahead ranker v3", "layer": "typeahead", "tags": ["typeahead"],
     "days": 14, "as_of_day": 14, "users_per_arm": 5000,
     "arms": {"control": {}, "test": {"ta_hit_nav": 0.48}},
     "params": {"typeahead_model": {"control": "ta_v2", "test": "ta_v3"}},
     "intended": ["typeahead_model"], "hypothesis": "A new typeahead model finds known items faster.",
     "gold": {"mechanism": "typeahead_funnel", "metric": "Typeahead CTR", "grade": "decision_grade"}},
    {"name": "synth-auth-requery", "display": "Connector result blending", "layer": "l3-blend", "tags": ["3p"],
     "days": 14, "as_of_day": 14, "users_per_arm": 5000,
     "arms": {"control": {}, "test": {"requery_auth": 0.35}},
     "params": {"blend": {"control": "b1", "test": "b2"}},
     "intended": ["blend"], "hypothesis": "Blending connector results by freshness helps authorized users.",
     "gold": {"mechanism": "authorized_struggle", "metric": "Search Success Rate", "grade": "decision_grade"}},
    {"name": "synth-answer-shift", "display": "Answer panel on more queries", "layer": "answer", "tags": ["answer"],
     "days": 14, "as_of_day": 14, "users_per_arm": 5000, "no_config": True,
     "arms": {"control": {}, "test": {"answer_shown_info": 0.40, "answer_suppresses_1p": 0.45}},
     "params": {"answer_threshold": {"control": 0.8, "test": 0.6}},
     "intended": ["answer_threshold"], "hypothesis": "Showing answers on more queries raises answer success.",
     "feature_metrics": ["Answer Shown Rate"],
     "gold": {"mechanism": "answer_substitution", "metric": "1P Binary CTR", "grade": "directional_only"}},
    {"name": "synth-null", "display": "Ranking feature cleanup", "layer": "l2-ranking", "tags": [],
     "days": 14, "as_of_day": 14, "users_per_arm": 5000,
     "arms": {"control": {}, "test": {}}, "params": {"features": {"control": "f41", "test": "f40"}},
     "intended": ["features"], "hypothesis": "Removing an unused feature changes nothing.",
     "gold": {"mechanism": "none", "metric": None, "grade": "decision_grade"}},
    {"name": "synth-split-one-arm", "display": "3P boost, two rankers", "layer": "l2-ranking", "tags": ["3p", "interleaver"],
     "days": 14, "as_of_day": 14, "users_per_arm": 5000,
     "arms": {"control": {}, "good": {"top1_3p_eligible": 0.45}, "bad": {"top1_3p_eligible": 0.45, "exposure_loss": 0.15}},
     "params": {"ranker": {"control": "l2_v7", "good": "l2_v8", "bad": "l2_v8b"}, "p3_offset": {"control": 0.0, "good": 0.15, "bad": 0.15}},
     "intended": ["ranker", "p3_offset"], "hypothesis": "A 3P offset on either new ranker raises 3P clicks.",
     "feature_metrics": ["3P Top1 Share"],
     "gold": {"mechanism": "first_party_displaced", "metric": "1P Long Click Rate", "grade": "decision_grade",
              "grades": {"good": "decision_grade", "bad": "not_permitted"}}},
    {"name": "synth-renamed-primary", "display": "3P boost, success metric renamed", "layer": "l2-ranking",
     "tags": ["3p", "interleaver"], "days": 14, "as_of_day": 14, "users_per_arm": 5000,
     "rename": {"Search Success Rate": "Search Success Rate v2"},
     "arms": {"control": {}, "boost": {"top1_3p_eligible": 0.45}},
     "params": {"ranker": "l2_v7", "p3_offset": {"control": 0.0, "boost": 0.15}},
     "intended": ["p3_offset"], "hypothesis": "A 3P score offset puts more 3P results on top and raises 3P clicks.",
     "feature_metrics": ["3P Top1 Share"],
     "gold": {"mechanism": "first_party_displaced", "metric": "1P Long Click Rate", "grade": "decision_grade"}},
    {"name": "synth-renamed-primary-b", "display": "3P pin, success metric renamed", "layer": "l2-ranking",
     "tags": ["3p", "interleaver"], "days": 14, "as_of_day": 14, "users_per_arm": 5000,
     "rename": {"Search Success Rate": "Search Success Rate v2"},
     "arms": {"control": {}, "pin": {"top1_3p_eligible": 0.5}},
     "params": {"ranker": "l2_v7", "pin_3p": {"control": False, "pin": True}},
     "intended": ["pin_3p"], "hypothesis": "Pinning connector results raises 3P clicks.",
     "feature_metrics": ["3P Top1 Share"],
     "gold": {"mechanism": "first_party_displaced", "metric": "1P Long Click Rate", "grade": "decision_grade"}},
    {"name": "synth-split-loss", "display": "Search page logging migration", "layer": "l2-ranking", "tags": [],
     "days": 14, "as_of_day": 14, "users_per_arm": 5000,
     "arms": {"control": {}, "test": {"exposure_loss": 0.15}}, "params": {"logger": {"control": "v1", "test": "v2"}},
     "intended": ["logger"], "hypothesis": "The new logger is neutral.",
     "gold": {"mechanism": "artifact", "metric": None, "grade": "not_permitted"}},
]

METRICS = [
    # name, type, directionality, numerator key, denominator key
    ("Exposed Users", "count", None, "exposed", None),
    ("Search Success Rate", "ratio", "increase", "success", "fp"),
    ("Search Success Rate, Position Weighted", "ratio", "increase", "success_w", "fp"),
    ("3P Clicks Per Authorized Query", "ratio", "increase", "click_3p", "fp_auth"),
    ("Answer Success Rate", "ratio", "increase", "answer_success", "answer_shown"),
    ("3P Top1 Share", "ratio", None, "top1_3p", "fp"),
    ("3P Clicks Per Query", "ratio", "increase", "click_3p", "fp"),
    ("Authorized Query Share", "ratio", None, "fp_auth", "fp"),
    ("1P Binary CTR", "ratio", "increase", "any_1p", "fp"),
    ("1P Long Click Rate", "ratio", "increase", "long_1p", "fp"),
    ("Typeahead CTR", "ratio", "increase", "ta_click", "queries"),
    ("Full-Page Queries Per User", "mean", None, "fp", "one"),
    ("Answer Shown Rate", "ratio", None, "answer_shown", "fp"),
    ("Latency Mean (ms)", "ratio", "decrease", "latency", "fp"),
]
KEYS = ["exposed", "one", "queries", "fp", "fp_auth", "success", "success_w", "click_3p", "answer_success", "answer_shown",
        "top1_3p", "any_1p", "long_1p", "ta_click", "latency"]


def simulate_query(rng, user, p):
    """One search intent -> a t3 row (dict) and the per-user counter increments."""
    nav = rng.random() < p["nav_share"]
    ta_hit = rng.random() < (p["ta_hit_nav"] if nav else p["ta_hit_info"])
    row = {"experience": "typeahead" if ta_hit else "surface_full", "typeahead_suggested": int(nav),
           "authorized_3p": user["authorized"], "eligible_3p": 0, "top1_3p": 0, "click_3p": 0, "long_click_3p": 0,
           "long_click_1p": 0, "any_click_1p": 0, "answer_shown": 0, "answer_success": 0, "success_flag_1": 0,
           "success_flag_2": 0.0, "max_long_click_position": None, "latency_ms": None, "ta_click": int(ta_hit)}
    if ta_hit:
        return row
    eligible = user["authorized"] and rng.random() < p["eligible_given_auth"]
    top1 = eligible and rng.random() < p["top1_3p_eligible"]
    shown = rng.random() < (p["answer_shown_nav"] if nav else p["answer_shown_info"])
    answered = shown and rng.random() < p["answer_success"]
    p1_long = p["p1_long_nav"] if nav else p["p1_long_info"]
    if top1:
        p1_long *= p["p1_long_displaced_factor"]
    if answered:
        p1_long *= 1 - p["answer_suppresses_1p"]
    long_1p = rng.random() < p1_long
    short_1p = not long_1p and rng.random() < p["p1_short"] * (1 - (p["answer_suppresses_1p"] if answered else 0))
    click_3p = eligible and rng.random() < (p["p3_click_top"] if top1 else p["p3_click_low"])
    long_3p = click_3p and rng.random() < p["p3_long_given_click"]
    success = long_1p or long_3p or answered or (short_1p and rng.random() < p["short_success"])
    position = (2 if top1 else 1) if long_1p else (1 if long_3p else None)
    row.update(eligible_3p=int(eligible), top1_3p=int(top1), click_3p=int(click_3p), long_click_3p=int(long_3p),
               long_click_1p=int(long_1p), any_click_1p=int(long_1p or short_1p), answer_shown=int(shown),
               answer_success=int(answered), success_flag_1=int(success),
               success_flag_2=(1.0 / position) if success and position else (1.0 if success else 0.0),
               max_long_click_position=position, latency_ms=round(rng.gauss(p["latency_ms"], 60), 1))
    return row


def add_counts(c, row):
    c["queries"] += 1
    c["ta_click"] += row["ta_click"]
    if row["experience"] != "surface_full":
        return
    c["fp"] += 1
    c["fp_auth"] += row["authorized_3p"]
    for key, col in (("success", "success_flag_1"), ("success_w", "success_flag_2"), ("click_3p", "click_3p"),
                     ("answer_success", "answer_success"), ("answer_shown", "answer_shown"), ("top1_3p", "top1_3p"),
                     ("any_1p", "any_click_1p"), ("long_1p", "long_click_1p"), ("latency", "latency_ms")):
        c[key] += row[col]


def ratio_stats(xs, ys):
    """Ratio of sums with a delta-method variance (users are the unit)."""
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    if n < 2 or sy <= 0:
        return None, None
    r = sx / sy
    my = sy / n
    resid = [x - r * y for x, y in zip(xs, ys)]
    mean_resid = sum(resid) / n
    var = sum((e - mean_resid) ** 2 for e in resid) / (n - 1)
    return r, var / (n * my * my)


def t1_rows_for_day(flight, users, running, ds_day):
    """Cumulative scorecard rows for each test arm against control, through day ds_day."""
    arms = list(flight["arms"])
    control = arms[0]
    per_arm = {arm: [] for arm in arms}
    for i, u in enumerate(users):
        if u["exposure_day"] <= ds_day and not u["exposure_lost"]:
            per_arm[u["arm"]].append(running[i])
    ds = (START + timedelta(days=ds_day - 1)).isoformat()
    rows = []
    idx = {k: n for n, k in enumerate(KEYS)}
    for name, kind, direction, num, den in METRICS:
        if name in flight.get("drop_metrics", []):
            continue
        stats = {}
        for arm in arms:
            totals = per_arm[arm]
            if kind == "count":
                stats[arm] = (len(totals), None, len(totals))
                continue
            ni, di = idx[num], idx[den]
            pairs = [(c[ni], c[di]) for c in totals if den == "one" or c[di] > 0]
            r, var = ratio_stats([a for a, _ in pairs], [b for _, b in pairs])
            stats[arm] = (r, var, len(pairs))
        shown = flight.get("rename", {}).get(name, name)
        for arm in arms[1:]:
            rc, vc, nc = stats[control]
            rt, vt, nt = stats[arm]
            row = {"experiment_name": flight["name"], "metric_name": shown, "metric_type": kind, "directionality": direction,
                   "control_group_name": control, "test_group_name": arm, "ds": ds, "use_cov": 0, "use_sequential": 0,
                   "control_mean": rc, "test_mean": rt, "control_units": nc, "test_units": nt,
                   "absolute_delta": None, "relative_delta": None, "relative_ci_lower": None, "relative_ci_upper": None,
                   "p_value": None}
            if kind != "count" and rc and rt:
                rel = rt / rc - 1
                se = (rt / rc) * math.sqrt(vt / rt ** 2 + vc / rc ** 2)
                if se > 0:
                    row.update(absolute_delta=rt - rc, relative_delta=100 * rel, relative_ci_lower=100 * (rel - 1.959964 * se),
                               relative_ci_upper=100 * (rel + 1.959964 * se),
                               p_value=2 * (1 - NORMAL.cdf(abs(rel) / se)))
            rows.append(row)
    return rows


T3_COLS = ["query_id", "day", "user_id", "tenant_id", "experience", "typeahead_suggested", "ta_click", "authorized_3p",
           "eligible_3p", "top1_3p", "click_3p", "long_click_3p", "long_click_1p", "any_click_1p", "answer_shown",
           "answer_success", "success_flag_1", "success_flag_2", "max_long_click_position", "latency_ms"]


def build_flight(rng, flight, scale, tenants, db):
    """Simulate the flight, stream its t3 rows into db, and return its t1 rows and exposures."""
    arms = list(flight["arms"])
    n = max(200, int(flight["users_per_arm"] * scale))
    users, days = [], flight["as_of_day"]
    weights = [t["weight"] for t in tenants]
    daily = []  # per user: {day: counts list}
    insert = f"INSERT INTO t3_queries VALUES ({','.join('?' * len(T3_COLS))})"
    for arm in arms:
        p = {**BASE, **flight["arms"][arm]}
        for i in range(n):
            tenant = rng.choices(tenants, weights)[0]
            activity = rng.gammavariate(2.0, 0.4)
            u = {"id": f"{flight['name']}-{arm}-u{i}", "arm": arm, "tenant": tenant["id"],
                 "authorized": int(rng.random() < tenant["auth_rate"]), "activity": activity,
                 "exposure_day": 1 + min(int(rng.expovariate(0.5)), 4)}
            u["exposure_lost"] = bool(p["exposure_loss"] and activity < 0.5 and rng.random() < p["exposure_loss"] / 0.3)
            users.append(u)
            per_day, batch = {}, []
            for d in range(u["exposure_day"], days + 1):
                for _ in range(_poisson(rng, activity)):
                    rows = [simulate_query(rng, u, p)]
                    if rows[0]["experience"] == "surface_full" and u["authorized"] and rng.random() < p["requery_auth"]:
                        rows.append(simulate_query(rng, u, {**p, "ta_hit_nav": 0, "ta_hit_info": 0, "p1_long_nav": 0.2,
                                                            "p1_long_info": 0.15, "answer_shown_info": 0.1}))
                    for r in rows:
                        c = per_day.setdefault(d, {k: 0 for k in KEYS})
                        add_counts(c, r)
                        r.update(query_id=uuid.UUID(int=rng.getrandbits(128)).hex, user_id=u["id"], tenant_id=u["tenant"],
                                 day=(START + timedelta(days=d - 1)).isoformat())
                        batch.append([r[col] for col in T3_COLS])
            if batch:
                db.executemany(insert, batch)
            daily.append(per_day)
    running = [[0] * len(KEYS) for _ in users]
    idx = {k: n for n, k in enumerate(KEYS)}
    for i, u in enumerate(users):
        running[i][idx["exposed"]] = 1
        running[i][idx["one"]] = 1
    t1 = []
    for ds_day in range(1, days + 1):
        for i in range(len(users)):
            counts = daily[i].get(ds_day)
            if counts:
                row = running[i]
                for k, v in counts.items():
                    row[idx[k]] += v
        t1.extend(t1_rows_for_day(flight, users, running, ds_day))
    exposures = [(u["id"], flight["name"], u["arm"], (START + timedelta(days=u["exposure_day"] - 1)).isoformat())
                 for u in users if not u["exposure_lost"]]
    return t1, exposures


def _poisson(rng, lam):
    if lam <= 0:
        return 0
    k, p, limit = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def config_for(flight, rng):
    arms = list(flight["arms"])
    groups = []
    for arm in arms:
        params = {k: (v[arm] if isinstance(v, dict) else v) for k, v in flight["params"].items()}
        groups.append({"id": f"{flight['name']}:{arm}", "name": arm, "size": round(100 / len(arms), 4), "parameters": params})
    end = START + timedelta(days=flight["days"] - 1)
    return {"id": str(uuid.UUID(int=rng.getrandbits(128))), "name": flight["display"], "experiment_name": flight["name"],
            "layer": flight["layer"], "tags": flight["tags"], "status": "active", "start": START.isoformat(),
            "end": end.isoformat(), "target_days": flight["days"], "control_group": f"{flight['name']}:{arms[0]}",
            "groups": groups, "hypothesis": flight["hypothesis"],
            "health_checks": [{"name": "Exposures are balanced", "status": "PASSED" if not any(
                a.get("exposure_loss") for a in flight["arms"].values()) else "FAILED"}]}


FOUR_KEYS = [{"metric": "Search Success Rate", "direction": "increase", "bar": 0.005},
             {"metric": "Search Success Rate, Position Weighted", "direction": "increase", "bar": 0.005},
             {"metric": "3P Clicks Per Authorized Query", "direction": "increase", "bar": 0.01},
             {"metric": "Answer Success Rate", "direction": "increase", "bar": 0.01}]
PRIMARIES = {"synth-typeahead-regress": [{"metric": "Typeahead CTR", "direction": "increase", "bar": 0.01},
                                         {"metric": "Search Success Rate", "direction": "increase", "bar": 0.005}],
             "synth-answer-shift": [{"metric": "Answer Success Rate", "direction": "increase", "bar": 0.01},
                                    {"metric": "Search Success Rate", "direction": "increase", "bar": 0.005}],
             "synth-auth-requery": [{"metric": "3P Clicks Per Authorized Query", "direction": "increase", "bar": 0.01},
                                    {"metric": "Search Success Rate", "direction": "increase", "bar": 0.005}]}


def contract_for(flight):
    return {"experiment_id": flight["name"], "name": flight["display"], "design": "ab", "confirmed": True,
            "ci_level": 0.95, "hypothesis": flight["hypothesis"],
            "primaries": {"rule": "any", "metrics": PRIMARIES.get(flight["name"], FOUR_KEYS)},
            "guardrails": [{"metric": "Latency Mean (ms)", "direction": "decrease", "margin": 0.02},
                           {"metric": "1P Long Click Rate", "direction": "increase", "margin": 0.02}],
            "intended_params": flight["intended"], "feature_metrics": flight.get("feature_metrics", []),
            "explanations": [], "pre_period": {}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--flights", nargs="*", help="only these flight names")
    args = parser.parse_args(argv)
    rng = random.Random(args.seed)
    for sub in ("configs", "contracts"):
        folder = os.path.join(args.out, sub)
        os.makedirs(folder, exist_ok=True)
        for old in os.listdir(folder):
            os.remove(os.path.join(folder, old))
    path = os.path.join(args.out, "warehouse.sqlite")
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE t1_scorecard (experiment_name TEXT, metric_name TEXT, metric_type TEXT, directionality TEXT,
            control_group_name TEXT, test_group_name TEXT, ds TEXT, use_cov INTEGER, use_sequential INTEGER,
            control_mean REAL, test_mean REAL, absolute_delta REAL, relative_delta REAL, relative_ci_lower REAL,
            relative_ci_upper REAL, p_value REAL, control_units INTEGER, test_units INTEGER);
        CREATE TABLE exposures (unit_id TEXT, experiment_name TEXT, group_name TEXT, first_exposure_date TEXT);
        CREATE TABLE t2_first_exposure (unit_id TEXT, name TEXT, experiment_group TEXT, assigned_date TEXT);
        CREATE TABLE t3_queries (query_id TEXT, day TEXT, user_id TEXT, tenant_id TEXT, experience TEXT,
            typeahead_suggested INTEGER, ta_click INTEGER, authorized_3p INTEGER, eligible_3p INTEGER, top1_3p INTEGER,
            click_3p INTEGER, long_click_3p INTEGER, long_click_1p INTEGER, any_click_1p INTEGER, answer_shown INTEGER,
            answer_success INTEGER, success_flag_1 INTEGER, success_flag_2 REAL, max_long_click_position INTEGER,
            latency_ms REAL);
        CREATE TABLE t4_tenants (tenant_id TEXT, paying INTEGER, as_of TEXT);
    """)
    tenants = []
    for i in range(120):
        tenants.append({"id": f"tenant{i:03d}", "weight": 1 / (i + 1) ** 1.1, "auth_rate": rng.uniform(0.15, 0.65),
                        "paying": int(rng.random() < 0.85)})
    db.executemany("INSERT INTO t4_tenants VALUES (?,?,?)", [(t["id"], t["paying"], START.isoformat()) for t in tenants])
    gold = {}
    t1_cols = ["experiment_name", "metric_name", "metric_type", "directionality", "control_group_name", "test_group_name",
               "ds", "use_cov", "use_sequential", "control_mean", "test_mean", "absolute_delta", "relative_delta",
               "relative_ci_lower", "relative_ci_upper", "p_value", "control_units", "test_units"]
    for flight in FLIGHTS:
        if args.flights and flight["name"] not in args.flights:
            continue
        t1, exposures = build_flight(rng, flight, args.scale, tenants, db)
        db.executemany(f"INSERT INTO t1_scorecard VALUES ({','.join('?' * len(t1_cols))})", [[r[c] for c in t1_cols] for r in t1])
        db.executemany("INSERT INTO exposures VALUES (?,?,?,?)", exposures)
        config = config_for(flight, rng)
        if not flight.get("no_config"):
            with open(os.path.join(args.out, "configs", f"{config['id']}.json"), "w") as handle:
                json.dump(config, handle, indent=1)
        with open(os.path.join(args.out, "contracts", f"{flight['name']}.json"), "w") as handle:
            json.dump(contract_for(flight), handle, indent=1)
        gold[flight["name"]] = {**flight["gold"], "config_id": None if flight.get("no_config") else config["id"],
                                "as_of_day": flight["as_of_day"], "days": flight["days"]}
    db.commit()
    db.close()
    with open(os.path.join(args.out, "gold.json"), "w") as handle:
        json.dump(gold, handle, indent=1)
    print(f"make_warehouse: {len(gold)} flights -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
