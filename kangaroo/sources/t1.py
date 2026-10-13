"""T1, the vendor scorecard, read from the warehouse into ds-experiment's canonical scorecard.

T1 has one row per experiment, metric, control arm, test arm, snapshot and mode, written
daily. The overlay's warehouse.json maps its column names (`t1_columns`); the defaults are
the synthetic warehouse's. Relative fields named in `_percent_fields` are percents.
"""

DEFAULT_COLUMNS = {
    "experiment": "experiment_name", "metric": "metric_name", "type": "metric_type", "direction": "directionality",
    "control_group": "control_group_name", "test_group": "test_group_name", "snapshot": "ds",
    "cuped": "use_cov", "sequential": "use_sequential", "control_mean": "control_mean", "test_mean": "test_mean",
    "abs_delta": "absolute_delta", "rel_delta": "relative_delta", "rel_ci_lo": "relative_ci_lower",
    "rel_ci_hi": "relative_ci_upper", "p_value": "p_value", "control_units": "control_units", "test_units": "test_units",
    "_percent_fields": ["rel_delta", "rel_ci_lo", "rel_ci_hi"],
}
FIELDS = [k for k in DEFAULT_COLUMNS if not k.startswith("_")]


def columns(overlay_cfg):
    return {**DEFAULT_COLUMNS, **(overlay_cfg.get("t1_columns") or {})}


def select_list(cols):
    return ", ".join(f"{cols[f]} AS {f}" for f in FIELDS)


def latest_sql(table, cols):
    return (f"SELECT {select_list(cols)} FROM {table} WHERE {cols['experiment']} = :experiment "
            f"AND {cols['snapshot']} = (SELECT MAX({cols['snapshot']}) FROM {table} WHERE {cols['experiment']} = :experiment) "
            f"ORDER BY {cols['test_group']}, {cols['metric']}")


def series_sql(table, cols):
    return (f"SELECT {cols['snapshot']} AS snapshot, {cols['metric']} AS metric, {cols['test_group']} AS test_group, "
            f"{cols['control_units']} AS control_units, {cols['test_units']} AS test_units, {cols['rel_delta']} AS rel_delta, "
            f"{cols['cuped']} AS cuped FROM {table} WHERE {cols['experiment']} = :experiment AND {cols['metric']} IN :metrics "
            f"ORDER BY {cols['snapshot']}")


def names_sql(table, cols):
    return f"SELECT DISTINCT {cols['experiment']} AS experiment FROM {table} WHERE {cols['experiment']} = :name"


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _flag(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "t", "yes")
    return bool(v) if v is not None else None


def scorecard_from_rows(rows, cols, prefer_cuped=True, ci_level=0.95):
    """Rows (with the canonical field names from select_list) -> canonical scorecard."""
    if not rows:
        return None
    percent = set(cols.get("_percent_fields") or [])
    modes = {_flag(r["cuped"]) for r in rows}
    mode = prefer_cuped if prefer_cuped in modes else next(iter(modes))
    rows = [r for r in rows if _flag(r["cuped"]) == mode]
    comparisons, control = {}, None
    for r in rows:
        control = control or r["control_group"]
        scale = lambda f: (_num(r[f]) / 100 if f in percent and _num(r[f]) is not None else _num(r[f]))  # noqa: E731
        lo, hi = scale("rel_ci_lo"), scale("rel_ci_hi")
        metric = {"metric": r["metric"], "metric_type": r.get("type"), "directionality": r.get("direction"),
                  "listed_as": "scorecard", "control_mean": _num(r["control_mean"]), "test_mean": _num(r["test_mean"]),
                  "abs_delta": _num(r["abs_delta"]), "rel_delta": scale("rel_delta"),
                  "rel_ci": [lo, hi] if lo is not None and hi is not None else None, "p_value": _num(r["p_value"]),
                  "control_units": _num(r["control_units"]), "test_units": _num(r["test_units"])}
        if metric["abs_delta"] is not None and metric["rel_ci"] and metric["control_mean"]:
            metric["abs_ci"] = [b * metric["control_mean"] for b in metric["rel_ci"]]
        comparisons.setdefault(r["test_group"], []).append(metric)
    snapshot = str(rows[0]["snapshot"])[:10]
    out = []
    for g, ms in comparisons.items():
        # One row per metric: decide.py reads the first and explain.py the last, so two rows for one
        # name would put two different numbers in one readout. Keep the row with a p-value (else the
        # first) and list the name, so the consistency check warns about it.
        kept, dupes = {}, []
        for m in ms:
            key = m["metric"].lower()
            if key in kept:
                dupes.append(key)
                if kept[key]["p_value"] is None and m["p_value"] is not None:
                    kept[key] = m
            else:
                kept[key] = m
        out.append({"test_group": g, "metrics": list(kept.values()), "duplicates": sorted(set(dupes))})
    return {"source": "t1", "experiment_id": rows[0].get("experiment"), "control_group": control,
            "analysis": {"ci_level": ci_level, "cuped": mode, "sequential": _flag(rows[0].get("sequential")),
                         "mc_correction": None, "as_of": snapshot},
            "comparisons": out}
