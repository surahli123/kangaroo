"""Turn a scorecard export and an experiment config into the canonical schema.

The canonical files are what every other tool reads, so a new scorecard source
only needs a new adapter here (or a column map for a CSV export).

    scorecard.json
      experiment_id, control_group, analysis {ci_level, cuped, sequential, mc_correction, as_of}
      comparisons[]: {test_group, metrics[]}
        metric, metric_type, listed_as (primary|secondary|monitoring), directionality,
        control_mean, test_mean, control_se, test_se, control_units, test_units,
        abs_delta, abs_ci [lo, hi], rel_delta, rel_ci [lo, hi] (fractions, not percent),
        p_value, seq_abs_ci, seq_rel_ci

    config.json
      experiment_id, name, hypothesis, id_type, layer_id, allocation_pct, control_group,
      groups[] {id, name, size_pct, parameters, disabled}, targeting_gate, inline_targeting,
      holdouts, start, end, decision_time, status, target_days, target_exposures,
      settings {ci_level, sequential, bonferroni, benjamini_hochberg, analytics_type},
      primary_metrics[], secondary_metrics[], health_checks[], edits[]

Statsig field names come from its Console API OpenAPI spec (Dec 2025 copy in
statsig-io/terraform-provider-statsig). The spec's own examples use snake_case
and different keys, so every lookup accepts both spellings.
"""

import csv
import json
import re
from datetime import datetime, timezone


class AdapterError(ValueError):
    """The export doesn't have what the canonical schema needs."""


def _snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _get(record, *names, default=None):
    """First present key among names, trying camelCase and snake_case of each."""
    for name in names:
        for key in (name, _snake(name)):
            if key in record and record[key] is not None:
                return record[key]
    return default


MISSING = {"", "n/a", "na", "-", "--", "none", "null", "nan"}


def _num(value):
    """A number from an export cell: accepts '1,234.5', '41.2%', '<0.001' (read as 0.001) and blanks."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace(",", "").replace("\u2212", "-")
    if text.lower() in MISSING:
        return None
    text = text.rstrip("%").lstrip("<>≤≥=~ ")
    try:
        return float(text)
    except ValueError:
        raise AdapterError(f"expected a number, got {value!r}")


def _bool(value):
    """True/False from a bool or a string like 'true', 'false', '1', '0'; None when absent."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise AdapterError(f"expected true or false, got {value!r}")


def level(value):
    """A confidence level as a fraction from '95', 95, '95%' or 0.95; None when absent."""
    number = _num(value)
    if number is None:
        return None
    number = number / 100 if number > 1 else number
    if not 0.5 < number < 1:
        raise AdapterError(f"confidence level {value!r} is not between 50% and 100%")
    return number


def _date(value):
    """Epoch milliseconds, epoch seconds or an ISO string -> YYYY-MM-DD."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
    return str(value)[:10]


def _interval(record, bound_names, half_names, centre):
    """An interval from {lower, upper} objects, or from centre +/- a half-width."""
    for name in bound_names:
        bounds = _get(record, name)
        if isinstance(bounds, dict) and "lower" in bounds and "upper" in bounds:
            return [_num(bounds["lower"]), _num(bounds["upper"])]
        if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
            return [_num(bounds[0]), _num(bounds[1])]
    half = _num(_get(record, *half_names))
    if half is None or centre is None:
        return None
    half = abs(half)
    return [centre - half, centre + half]


def statsig_metric(record, listed_as):
    metric_id = _get(record, "metricID", "metricId", "metric_id", default="")
    name, _, kind = str(metric_id).partition("::")
    name = _get(record, "metricName", default=name) or name
    control_mean = _num(_get(record, "controlMean"))
    test_mean = _num(_get(record, "testMean"))
    abs_delta = _num(_get(record, "absoluteChange", "value"))
    if abs_delta is None and None not in (control_mean, test_mean):
        abs_delta = test_mean - control_mean
    pct = _num(_get(record, "percentChange"))
    rel_delta = pct / 100 if pct is not None else (
        abs_delta / control_mean if abs_delta is not None and control_mean else None)
    if name == "":
        raise AdapterError("a metric record has no metricID or metricName")
    percent_ci = _interval(record, ["percentConfidenceInterval"], ["percentConfidenceIntervalDelta"], pct)
    seq_percent_ci = _interval(record, [], ["percentSequentialTestingConfidenceIntervalDelta"], pct)
    return {
        "metric": name, "metric_type": kind or None, "listed_as": listed_as,
        "directionality": _get(record, "directionality"),
        "control_mean": control_mean, "test_mean": test_mean,
        "control_se": _num(_get(record, "controlStd")), "test_se": _num(_get(record, "testStd")),
        "control_units": _num(_get(record, "controlUnits")), "test_units": _num(_get(record, "testUnits")),
        "abs_delta": abs_delta,
        "abs_ci": _interval(record, ["confidenceInterval"],
                            ["absoluteConfidenceIntervalDelta", "confidenceIntervalDelta"], abs_delta),
        "rel_delta": rel_delta,
        "rel_ci": _percent_to_fraction(percent_ci),
        "p_value": _num(_get(record, "pValue")),
        "seq_abs_ci": _interval(record, ["sequentialTestingConfidenceInterval"],
                                ["sequentialTestingConfidenceIntervalDelta"], abs_delta),
        "seq_rel_ci": _percent_to_fraction(seq_percent_ci),
    }


def _percent_to_fraction(interval):
    return [b / 100 for b in interval] if interval else None


def statsig_pulse(responses, request=None, test_groups=None):
    """Statsig `pulse_results` responses (one per test group) -> canonical scorecard.

    `request` is what the caller asked for: {"control", "test", "cuped", "confidence",
    "sequential", "applyBonferroniPerMetric", ...}. The response doesn't echo these, so
    they come from here, and missing ones are recorded as unknown (None), never guessed.
    Each response's test group comes from the response itself, else from `test_groups`
    (one per response), else from request["test"] when there is a single response.
    """
    request = request or {}
    if test_groups and len(test_groups) != len(responses):
        raise AdapterError("give one test group per pulse file")
    comparisons, as_of = [], None
    for i, response in enumerate(responses):
        if not isinstance(response, dict):
            raise AdapterError("a pulse response must be a JSON object")
        data = response.get("data", response)
        if not isinstance(data, dict):
            raise AdapterError("the pulse response's data is not an object; save one response per test group")
        as_of = as_of or data.get("ds")
        metrics = [statsig_metric(m, "primary") for m in data.get("primaryMetrics", data.get("primary_metrics", []))]
        metrics += [statsig_metric(m, "secondary") for m in data.get("secondaryMetrics", data.get("secondary_metrics", []))]
        metrics += [statsig_metric(m, "monitoring") for m in data.get("monitoringMetrics", [])]
        if not metrics:
            raise AdapterError("a pulse response has no primary, secondary or monitoring metrics")
        group = response.get("test") or data.get("test") or (test_groups[i] if test_groups else None)
        if group is None:
            if len(responses) > 1:
                raise AdapterError("several pulse files but no test group for each; pass --pulse FILE:GROUP")
            group = request.get("test")
        comparisons.append({"test_group": group, "metrics": metrics})
    groups = [c["test_group"] for c in comparisons]
    if len(set(groups)) < len(groups):
        raise AdapterError(f"two pulse files have the same test group {groups}; pass --pulse FILE:GROUP")
    corrections = [k for k in ("applyBonferroniPerMetric", "applyBonferroniPerVariant",
                               "applyBenjaminiHochbergPerMetric", "applyBenjaminiHochbergPerVariant")
                   if _bool(request.get(k))]
    return {
        "source": "statsig_pulse",
        "experiment_id": request.get("experiment_id"),
        "control_group": request.get("control"),
        "analysis": {
            "ci_level": level(request.get("confidence", request.get("ci_level"))),
            "cuped": _bool(request.get("cuped")),
            "sequential": _bool(request.get("sequential")),
            "mc_correction": ",".join(corrections) or ("none" if request else None),
            "as_of": request.get("as_of") or as_of,
        },
        "comparisons": comparisons,
    }


def statsig_config(experiment, audit_logs=None):
    """Statsig Get Experiment (+ optional audit log entries) -> canonical config."""
    exp = experiment.get("data", experiment)
    groups = [{"id": g.get("id"), "name": g.get("name"), "size_pct": _num(g.get("size")),
               "parameters": _get(g, "parameterValues", default={}) or {}, "disabled": bool(g.get("disabled"))}
              for g in exp.get("groups", [])]
    if not groups:
        raise AdapterError("the experiment config has no groups")
    metric_list = lambda key: [{"metric": m.get("name"), "type": m.get("type"), "direction": m.get("direction"),  # noqa: E731
                                "hypothesized": m.get("hypothesizedValue")} for m in _get(exp, key, default=[]) or []]
    entries = audit_logs.get("data", []) if isinstance(audit_logs, dict) else (audit_logs or [])
    edits = []
    for entry in entries:
        edits.append({"action": _get(entry, "actionType"), "date": _date(_get(entry, "date", "time")),
                      "time": _get(entry, "time"), "summary": _get(entry, "changeLog"),
                      "changes": _get(entry, "changes", default={})})
    ci = _get(exp, "defaultConfidenceInterval")
    return {
        "experiment_id": exp.get("id"), "name": exp.get("name"), "hypothesis": exp.get("hypothesis"),
        "id_type": _get(exp, "idType"), "layer_id": _get(exp, "layerID", "layerId"),
        "allocation_pct": _num(_get(exp, "allocation")), "control_group": _get(exp, "controlGroupID", "controlGroupId"),
        "groups": groups,
        "targeting_gate": _get(exp, "targetingGateID", "targetingGateId"),
        "inline_targeting": _get(exp, "inlineTargetingRulesJSON", "inline_targeting_rules_json"),
        "holdouts": _get(exp, "holdoutIDs", "holdoutIds", default=[]) or [],
        "start": _date(_get(exp, "startTime")), "end": _date(_get(exp, "endTime")),
        "decision_time": _date(_get(exp, "decisionTime")), "status": exp.get("status"),
        "target_days": _num(_get(exp, "duration")), "target_exposures": _num(_get(exp, "targetExposures")),
        "settings": {"ci_level": level(ci),
                     "sequential": _bool(_get(exp, "sequentialTesting")),
                     "bonferroni": bool(_get(exp, "bonferroniCorrection") or _get(exp, "bonferroniCorrectionPerMetric")),
                     "benjamini_hochberg": bool(_get(exp, "benjaminiHochbergPerMetric")
                                                or _get(exp, "benjaminiHochbergPerVariant")),
                     "analytics_type": _get(exp, "analyticsType")},
        "primary_metrics": metric_list("primaryMetrics"), "secondary_metrics": metric_list("secondaryMetrics"),
        "health_checks": [{"type": h.get("type"), "name": h.get("name"), "status": h.get("status"),
                           "metadata": h.get("metadata", {})} for h in exp.get("healthChecks", [])],
        "edits": edits,
        "exposures": exp.get("exposures") or {},
    }


def mapped_csv(path, column_map, request=None):
    """A CSV scorecard export -> canonical scorecard, using a column map.

    column_map: {"canonical field": "export column", ...}. Interval columns map as
    rel_ci_lo/rel_ci_hi, abs_ci_lo/abs_ci_hi, seq_rel_ci_lo/seq_rel_ci_hi. Options:
    "_percent_fields": [...] for columns in percent, and "_control_label": the variant
    value of control rows in a long export (those rows are skipped). Needs a metric
    and an abs_delta or rel_delta on every test row. request.json takes the same keys
    as for Pulse ("confidence", "cuped", "sequential", "as_of").
    """
    percent = set(column_map.get("_percent_fields", []))
    control_label = column_map.get("_control_label")
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise AdapterError(f"{path} has no rows")
    missing = [c for k, c in column_map.items() if not k.startswith("_") and c not in rows[0]]
    if missing:
        raise AdapterError(f"columns not in the export: {missing}; the export has {sorted(rows[0])}")
    by_group = {}
    for row in rows:
        metric = {}
        for field, column in column_map.items():
            if field.startswith("_"):
                continue
            value = row[column]
            if field in ("metric", "test_group", "listed_as", "directionality", "metric_type"):
                metric[field] = (value or "").strip() or None
            else:
                number = _num(value)
                metric[field] = number / 100 if number is not None and field in percent else number
        if control_label and str(metric.get("test_group", "")).lower() == str(control_label).lower():
            continue
        for name in ("abs_ci", "rel_ci", "seq_abs_ci", "seq_rel_ci"):
            lo, hi = metric.pop(name + "_lo", None), metric.pop(name + "_hi", None)
            metric[name] = [lo, hi] if lo is not None and hi is not None else None
        if not metric.get("metric") or metric.get("abs_delta") is None and metric.get("rel_delta") is None:
            raise AdapterError("each test row needs a metric and an abs_delta or rel_delta "
                               "(for a long export, set _control_label so control rows are skipped)")
        metric.setdefault("listed_as", "secondary")
        group = metric.pop("test_group", None) or "test"
        if any(m["metric"] == metric["metric"] for m in by_group.get(group, [])):
            raise AdapterError(f"metric '{metric['metric']}' appears twice for group '{group}'; map the variant column")
        by_group.setdefault(group, []).append(metric)
    request = request or {}
    return {"source": "mapped_csv", "experiment_id": request.get("experiment_id"),
            "control_group": request.get("control"),
            "analysis": {"ci_level": level(request.get("confidence", request.get("ci_level"))),
                         "cuped": _bool(request.get("cuped")), "sequential": _bool(request.get("sequential")),
                         "mc_correction": request.get("mc_correction"), "as_of": request.get("as_of")},
            "comparisons": [{"test_group": g, "metrics": m} for g, m in by_group.items()]}


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
