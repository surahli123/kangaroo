"""The cut runner. Templates (cut_templates.json, plus the overlay's) are data the owner DS
reviews; this runner is engineering's and stays fixed:

- one aggregate statement per cut, read-only, built from the template;
- arms come from the named assignment source, queries after each unit's first exposure;
- the paying cohort from the tenant table when the overlay names it;
- per user first (the unit of randomization), then per arm: n and the sums for a ratio of
  sums with a delta-method variance, so the result ties to T1;
- a cell with fewer than `min_units_per_cell` users gives no result;
- a table the overlay hasn't approved gives an access gap, never a query.
"""

import json
import math
import os
import re
from statistics import NormalDist

from kangaroo import CUTS
from kangaroo.sources.warehouse import QueryRefused

Z = NormalDist().inv_cdf(0.975)


class AccessGap(Exception):
    pass


def load_templates(overlay_dir=None):
    with open(CUTS, encoding="utf-8") as handle:
        templates = {k: v for k, v in json.load(handle).items() if not k.startswith("_")}
    extra = os.path.join(overlay_dir, "cut_templates.json") if overlay_dir else None
    if extra and os.path.exists(extra):
        with open(extra, encoding="utf-8") as handle:
            templates.update({k: v for k, v in json.load(handle).items() if not k.startswith("_")})
    return templates


def needed_tables(wh_cfg):
    return ["assignment", "t3"] + (["t4"] if wh_cfg.get("cohort") else [])


DAY_INDEX = {"sqlite": "CAST(julianday(t.{day}) - julianday(a.first_exposure) AS INTEGER) + 1",
             "spark": "DATEDIFF(t.{day}, a.first_exposure) + 1"}


def build_sql(tpl, wh_cfg, dialect="sqlite"):
    tables, assign = wh_cfg["tables"], wh_cfg["assignment"]
    t3c = wh_cfg.get("t3_columns") or {}
    col = lambda expr: re.sub(r"\bt\.(\w+)", lambda m: "t." + t3c.get(m.group(1), m.group(1)), expr)  # noqa: E731
    unit, day, tenant = (t3c.get(c, c) for c in ("user_id", "day", "tenant_id"))
    cohort = wh_cfg.get("cohort")
    cohort_join = (f" JOIN {tables['t4']} c ON t.{tenant} = c.{cohort['tenant']} AND c.{cohort['flag']} = 1" if cohort else "")
    y_agg = "MAX" if tpl.get("y_per_user") == "max" else "SUM"
    series = bool(tpl.get("series"))
    k_col = f", {DAY_INDEX[dialect].format(day=day)} AS k" if series else ""
    k_key = ", k" if series else ""
    return (f"WITH a AS (SELECT {assign['unit']} AS unit, {assign['group']} AS arm, {assign['first_exposure']} AS first_exposure "
            f"FROM {tables['assignment']} WHERE {assign['experiment']} = :experiment), "
            f"q AS (SELECT a.arm AS arm, t.{unit} AS unit, {col(tpl['x'])} AS x, {col(tpl['y'])} AS y{k_col} "
            f"FROM {tables['t3']} t JOIN a ON t.{unit} = a.unit{cohort_join} "
            f"WHERE t.{day} >= a.first_exposure AND t.{day} <= :as_of AND {col(tpl['filter'])}), "
            f"u AS (SELECT arm, unit{k_key}, SUM(x) AS x, {y_agg}(y) AS y FROM q GROUP BY arm, unit{k_key}) "
            f"SELECT arm{k_key}, COUNT(*) AS n, SUM(x) AS sx, SUM(y) AS sy, SUM(x * x) AS sxx, SUM(y * y) AS syy, SUM(x * y) AS sxy "
            f"FROM u GROUP BY arm{k_key}")


def arm_ratio(row):
    n, sx, sy = row["n"], row["sx"], row["sy"]
    if not n or n < 2 or not sy:
        return None, None, n
    r = sx / sy
    var_e = (row["sxx"] - 2 * r * row["sxy"] + r * r * row["syy"]) / (n - 1)
    return r, var_e / (n * (sy / n) ** 2), n


def compare(rows, control, test, produces, min_units, direction=None):
    by_arm = {r["arm"]: r for r in rows}
    if control not in by_arm or test not in by_arm:
        return None, f"no rows for {control if control not in by_arm else test}"
    rc, vc, nc = arm_ratio(by_arm[control])
    rt, vt, nt = arm_ratio(by_arm[test])
    if min(nc or 0, nt or 0) < min_units:
        return None, f"fewer than {min_units} users in a cell ({nc} and {nt})"
    if not rc or rt is None:
        return None, "a zero denominator"
    rel = rt / rc - 1
    se = (rt / rc) * math.sqrt((vt / rt ** 2 if rt else 0) + vc / rc ** 2)
    return {"metric": produces, "listed_as": "evidence", "test_group": test, "directionality": direction,
            "control_mean": rc, "test_mean": rt, "abs_delta": rt - rc, "rel_delta": rel,
            "rel_ci": [rel - Z * se, rel + Z * se], "control_units": nc, "test_units": nt,
            "p_value": 2 * (1 - NormalDist().cdf(abs(rel) / se)) if se > 0 else None}, None


def run_cut(warehouse, template_id, templates, wh_cfg, experiment, as_of, control, tests, min_units=100):
    """Run one template; returns {statement, metrics: [evidence per test arm], problems}."""
    missing = [t for t in needed_tables(wh_cfg) if t not in (wh_cfg.get("approved") or [])]
    if missing:
        raise AccessGap(f"not approved for queries: {', '.join(missing)}")
    tpl = templates[template_id]
    sql = build_sql(tpl, wh_cfg, warehouse.dialect)
    try:
        res = warehouse.query(sql, {"experiment": experiment, "as_of": as_of}, label=f"cut {template_id}")
    except (QueryRefused, TimeoutError) as err:
        raise AccessGap(str(err)) from err
    if tpl.get("series"):
        return {"template": template_id, "statement": {k: res[k] for k in ("statement_id", "label", "row_count", "sql_sha")},
                "series": {test: series_points(res["rows"], control, test, min_units) for test in tests}, "metrics": [],
                "problems": []}
    metrics, problems = [], []
    for test in tests:
        m, problem = compare(res["rows"], control, test, tpl["produces"], min_units, tpl.get("direction"))
        if m:
            m["source"] = res["statement_id"]
            metrics.append(m)
        else:
            problems.append(f"{test}: {problem}")
    statement = {k: res[k] for k in ("statement_id", "label", "row_count", "sql_sha")}
    return {"template": template_id, "statement": statement, "metrics": metrics, "problems": problems}


def series_points(rows, control, test, min_units):
    """Effect by days since first exposure, in the shape decide.py's novelty check reads."""
    points = []
    for k in sorted({r["k"] for r in rows if r.get("k") is not None}):
        day_rows = [r for r in rows if r.get("k") == k]
        m, _ = compare(day_rows, control, test, "series", min_units)
        if m:
            points.append({"day": int(k), "rel_delta": m["rel_delta"], "rel_ci": m["rel_ci"], "basis": "days_since_exposure"})
    return points


IDENTIFIERS = ("user_id", "query_id", "tenant_id", "unit")
ALLOWED_KEYS = {"produces", "filter", "x", "y", "y_per_user", "full_page", "surface_shift_is_the_question", "series", "note"}


def template_problem(tpl, known_columns=None):
    """Why a proposed template can't be used, or None. A template is data a lesson may add, so
    it may only name query-table columns the shipped templates already use (or the overlay
    lists), never an identifier column (a filter on one is a membership probe), and no
    statement keyword besides the expression itself."""
    if not isinstance(tpl, dict) or not tpl.get("produces") or "x" not in tpl or "y" not in tpl:
        return "it needs produces, x and y"
    extra = set(tpl) - ALLOWED_KEYS
    if extra:
        return f"unknown fields: {', '.join(sorted(extra))}"
    known = set(known_columns or [])
    if not known:
        for t in load_templates().values():
            for field in ("filter", "x", "y"):
                known |= set(re.findall(r"\bt\.(\w+)", str(t.get(field, ""))))
    for field in ("filter", "x", "y"):
        expr = str(tpl.get(field, ""))
        cols = set(re.findall(r"\bt\.(\w+)", expr))
        bad = [c for c in cols if c in IDENTIFIERS or c not in known]
        if bad:
            return f"{field} names {', '.join(sorted(bad))}, which a template may not use"
        if re.search(r"\b(select|from|join|union|insert|update|delete|drop|with)\b|;|--", expr, re.I):
            return f"{field} holds a statement keyword"
        if re.search(r"\b(?:" + "|".join(IDENTIFIERS) + r")\b", expr, re.I):
            return f"{field} names an identifier column"
    return None


def dry_run_sql(tpl, wh_cfg, dialect="spark"):
    """The statement with LIMIT 0, to check a template compiles on the work warehouse before it is used."""
    return build_sql(tpl, wh_cfg, dialect) + " LIMIT 0"
