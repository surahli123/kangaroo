#!/usr/bin/env python3
"""Explain the movements triage will ask about, propose where to dig next, and say
whether the experiment is ready for triage.

    python3 explain.py --decision decision.json --scorecard scorecard.json --prereg prereg.json \
        [--config config.json] [--evidence evidence.json] [--overlay DIR] [--out explain.json]

Patterns come from references/search_patterns.json, plus patterns.json and
metric_families.json in the overlay. evidence.json carries what the deep-dive
cuts found, so a cut's result can confirm or refute a candidate by script:

    {"metrics":  [{"metric": "SAT Click Rate", "rel_delta": 0.001, "rel_ci": [-0.004, 0.006],
                   "control_units": 998000, "test_units": 1001000, "source": "query q17"}],
     "segments": {"Abandonment Rate": {"by": "answer on the page", "parts": [
                   {"segment": "With answer", "rel_delta": 0.003, "rel_ci": [-0.005, 0.011], "weight": 0.3,
                    "control_units": 301000, "test_units": 299000}]}},
     "cuts":     [{"pattern": "attractive_not_relevant", "cut": 4, "outcome": "refutes",
                   "evidence": "long clicks rose at every position"}]}

weight is the segment's share of the control metric total. New metrics join the
scorecard for family matching (so "SAT Click Rate" can answer the SAT-click cut);
segment parts with per-arm units get a sample-split test and are dropped if it fails.

Rules that keep it honest:
- A family is "flat" only when its interval sits inside the family's practical band,
  and "unknown" when the interval is too wide to tell; unknown neither supports nor
  contradicts anything.
- A pattern whose trigger metrics are neither registered nor surviving the
  false-discovery correction is exploratory: listed, never "likely", never in the plan.
- Segment findings are exploratory unless the pre-registration lists the dimension
  or segment (prereg "segments").
- A candidate is confirmed only by a cut whose result matches its confirms_when (or an
  explicit outcome), and dropped when a cut refutes it.

Writes explain.json next to the decision. Exit 0, or 2 on bad input.
"""

import argparse
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import decompose  # noqa: E402
import expstats  # noqa: E402
from decide import describe, moved  # noqa: E402

PATTERNS = os.path.join(os.path.dirname(HERE), "references", "search_patterns.json")
STATE_OK = {"up": {"up"}, "down": {"down"}, "flat": {"flat"}, "flat_or_down": {"flat", "down"},
            "up_or_flat": {"up", "flat"}, "moved": {"up", "down"}, "any": {"up", "down", "flat"}}
ACTIVITY = {"queries_per_user", "clicks_per_user", "sessions_per_user", "ctr"}
DEFAULT_BAND = 0.01


# ---------- library and families ----------

def load_library(overlay, base=None):
    """The pattern library: `base` (default references/search_patterns.json) plus the
    overlay's patterns.json and metric_families.json."""
    with open(base or PATTERNS, encoding="utf-8") as handle:
        library = json.load(handle)
    if overlay:
        extra = os.path.join(overlay, "patterns.json")
        if os.path.exists(extra):
            with open(extra, encoding="utf-8") as handle:
                more = json.load(handle)
            ids = {p["id"]: i for i, p in enumerate(library["patterns"])}
            for p in more.get("patterns", []):
                if p["id"] in ids:
                    library["patterns"][ids[p["id"]]] = p
                else:
                    library["patterns"].append(p)
            library["families"].update(more.get("families", {}))
            library["ratios"].update(more.get("ratios", {}))
        names = os.path.join(overlay, "metric_families.json")
        if os.path.exists(names):
            with open(names, encoding="utf-8") as handle:
                library["exact_families"] = json.load(handle)
    for name, spec in list(library["families"].items()):
        if isinstance(spec, list):
            library["families"][name] = {"match": spec, "exclude": [], "good": None, "practical": DEFAULT_BAND}
    return library


def _direction(m):
    d = str(m.get("directionality") or "").lower()
    return "increase" if d in ("increase", "higher", "up") else "decrease" if d in ("decrease", "lower", "down") else None


def family_candidates(library, m):
    name = m["metric"].lower()
    out = []
    for family, spec in library["families"].items():
        if not any(re.search(p, name) for p in spec["match"]):
            continue
        if any(re.search(p, name) for p in spec.get("exclude", [])):
            continue
        good, said = spec.get("good"), _direction(m)
        if good and said and good != said:
            continue  # a "decrease is good" metric is not a success metric, whatever its name says
        out.append(family)
    return out


def map_families(library, metrics, registered):
    """family -> metric record, plus notes on names the map refused."""
    fam, notes = {}, []
    by_name = {m["metric"].lower(): m for m in metrics}
    for family, names in library.get("exact_families", {}).items():
        for name in names if isinstance(names, list) else [names]:
            if name.lower() in by_name:
                fam[family] = by_name[name.lower()]
                break
    taken = {id(m) for m in fam.values()}
    claims = {}
    for m in metrics:
        if id(m) in taken or " - " in m["metric"]:
            continue
        found = family_candidates(library, m)
        if len(found) > 1:
            notes.append(f"'{m['metric']}' looks like {' and '.join(found)}; not used for patterns (set it in metric_families.json)")
            continue
        if found:
            claims.setdefault(found[0], []).append(m)
    for family, ms in claims.items():
        if family in fam:
            continue
        ms.sort(key=lambda m: (m["metric"].lower() not in registered, len(m["metric"])))
        fam[family] = ms[0]
    return fam, notes


# ---------- states ----------

def rel_interval(m):
    ci, point = m.get("rel_ci"), m.get("rel_delta")
    if (ci is None or point is None) and m.get("abs_ci") and m.get("control_mean"):
        ci = [b / m["control_mean"] for b in m["abs_ci"]]
        point = (m.get("abs_delta") or 0) / m["control_mean"]
    return (point, ci) if ci and point is not None else (None, None)


def significant(m):
    point, ci = rel_interval(m)
    if ci is None:
        ci = m.get("abs_ci")
    if not ci:
        return None
    return "up" if ci[0] > 0 else "down" if ci[1] < 0 else "flat"


def state(m, band):
    """up / down / flat / unknown against a practical band (an equivalence-style reading)."""
    point, ci = rel_interval(m)
    if ci is None:
        return None
    lo, hi = min(ci), max(ci)
    inside = -band <= lo and hi <= band
    if lo > 0 or hi < 0:
        if abs(point) >= band:
            return "up" if point > 0 else "down"
        return "flat" if inside else "unknown"
    return "flat" if inside else "unknown"


def change_text(m):
    point, ci = rel_interval(m)
    if ci is None:
        return "has no interval"
    entry = {"point": point, "low": ci[0], "high": ci[1], "unit": "relative"}
    return moved(entry) if significant(m) != "flat" else f"didn't move clearly ({describe(entry)})"


# ---------- co-movement patterns ----------

def _holds(conds, states):
    return all(f in states and states[f] in STATE_OK[s] for f, s in conds.items())


def _supports(m, want, band):
    """A supporting metric: a clear move in the stated direction counts even if it is small;
    'flat' still needs the interval inside the practical band."""
    sig, st = significant(m), state(m, band)
    if want in ("up", "down"):
        return sig == want
    if want == "flat_or_down":
        return st == "flat" or sig == "down"
    if want == "up_or_flat":
        return st == "flat" or sig == "up"
    return st in STATE_OK.get(want, set())


def _when_ok(m, want, band):
    """Triggers and contradictions: an up/down needs a move of practical size; the
    flat_or_down / up_or_flat forms accept a clear move the right way or an interval inside the band."""
    if want in ("up", "down"):
        return state(m, band) == want
    return _supports(m, want, band)


def match_comovement(library, fam, states, trusted, band_of=lambda f: DEFAULT_BAND):
    found, ruled_out = [], []
    for p in library["patterns"]:
        if p.get("kind") or not (p.get("when") or p.get("any_of")):
            continue
        need = p.get("when") or {}
        if not all(f in fam and _when_ok(fam[f], s, band_of(f)) for f, s in need.items()):
            continue
        any_of = {f: s for f, s in (p.get("any_of") or {}).items() if f in fam and _when_ok(fam[f], s, band_of(f))}
        if p.get("any_of") and not any_of:
            continue
        contra = [f for f, s in (p.get("contradicted_by") or {}).items() if f in fam and _when_ok(fam[f], s, band_of(f))]
        support = [f for f, s in (p.get("strengthened_by") or {}).items()
                   if f in fam and s != "any" and f not in need and f not in any_of and _supports(fam[f], s, band_of(f))]
        families = list(dict.fromkeys(list(need) + list(any_of) + support))
        triggers = list(need) + list(any_of)
        exploratory = not any(fam[f]["metric"].lower() in trusted for f in triggers if states.get(f) in ("up", "down")) \
            and not any(fam[f]["metric"].lower() in trusted for f in triggers)
        instance = {"id": p["id"], "name": p["name"], "label": "inferred", "status": "untested", "mechanism": p["mechanism"],
                    "reading": p.get("reading", "neutral"), "source": p.get("source"), "families": families,
                    "metrics": [fam[f]["metric"] for f in families],
                    "evidence": [f"{fam[f]['metric']} {change_text(fam[f])}" for f in families],
                    "score": len(support), "deep_dives": p.get("deep_dives", []), "exploratory": exploratory}
        if contra:
            instance["ruled_out_by"] = [f"{fam[f]['metric']} {change_text(fam[f])}" for f in contra]
            ruled_out.append(instance)
        else:
            found.append(instance)
    return found, ruled_out


def fill(template, values):
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template)


def ratio_patterns(library, fam, level):
    out, notes = [], []
    spec = next((p for p in library["patterns"] if p.get("kind") == "ratio"), None)
    for ratio_family, parts in library.get("ratios", {}).items():
        r, n, d = fam.get(ratio_family), fam.get(parts["numerator"]), fam.get(parts["denominator"])
        if not (spec and r and n and d) or significant(r) not in ("up", "down"):
            continue
        if None in (n.get("rel_delta"), d.get("rel_delta"), r.get("rel_delta")) or not (n.get("rel_ci") and d.get("rel_ci") and r.get("rel_ci")):
            continue
        split = decompose.ratio({"metric": n["metric"], "rel_delta": n["rel_delta"], "rel_ci": n["rel_ci"]},
                                {"metric": d["metric"], "rel_delta": d["rel_delta"], "rel_ci": d["rel_ci"]}, level)
        if split["denominator_share"] < 0.5 or not split["denominator"].get("clear_of_zero"):
            continue
        se_r = expstats.se_from_ci(r["rel_ci"][0], r["rel_ci"][1], level)
        se_parts = math.sqrt(sum(expstats.se_from_ci(x["rel_ci"][0], x["rel_ci"][1], level) ** 2 for x in (n, d)))
        consistent = abs(split["ratio_rel_delta"] - r["rel_delta"]) <= 2 * math.sqrt(se_r ** 2 + se_parts ** 2)
        values = {"ratio": r["metric"], "numerator": n["metric"], "denominator": d["metric"], "denominator_change": change_text(d)}
        evidence = [f"{r['metric']} {change_text(r)}", f"{n['metric']} {change_text(n)}", f"{d['metric']} {change_text(d)}",
                    f"{round(100 * split['denominator_share'])}% of the split comes from {d['metric']}"]
        if not consistent:
            notes.append(f"the per-user counts imply {r['metric']} {100 * split['ratio_rel_delta']:+.2f}%, not "
                         f"{100 * r['rel_delta']:+.2f}%; {r['metric']} is probably on a different population or definition")
            evidence.append(notes[-1])
        out.append({"id": spec["id"], "name": spec["name"], "label": "measured" if consistent else "inferred",
                    "status": "confirmed by the scorecard" if consistent else "untested",
                    "mechanism": fill(spec["mechanism"], values), "reading": spec.get("reading", "neutral"),
                    "source": spec.get("source"), "families": [ratio_family, parts["numerator"], parts["denominator"]],
                    "metrics": [r["metric"], n["metric"], d["metric"]], "evidence": evidence, "score": 3 if consistent else 1,
                    "deep_dives": [{k: fill(v, values) if isinstance(v, str) else v for k, v in dd.items()} for dd in spec["deep_dives"]],
                    "exploratory": False})
    return out, notes


# ---------- segments ----------

def segment_groups(comparison, library, evidence):
    """[(metric, dimension, parts, measured)] from scorecard cuts and evidence segments."""
    dims = library.get("segment_dimensions", {})
    grouped = {}
    for m in comparison["metrics"]:
        if " - " not in m["metric"]:
            continue
        base, segment = m["metric"].rsplit(" - ", 1)
        dim = next((d for d, names in dims.items() if segment.lower() in names), "cut")
        grouped.setdefault((base, dim), []).append({"segment": segment, "rel_delta": m.get("rel_delta"), "rel_ci": m.get("rel_ci"),
                                                    "control_units": m.get("control_units"), "test_units": m.get("test_units")})
    out = [(metric, dim, parts, False) for (metric, dim), parts in grouped.items()]
    for metric, cuts in ((evidence or {}).get("segments") or {}).items():
        for cut in cuts if isinstance(cuts, list) else [cuts]:
            out.append((metric, cut.get("by", "segment"), [dict(p) for p in cut["parts"]], True))
    return out


def _segment_srm(parts, overall_ratio):
    from checks import chi_square_p
    kept, dropped = [], []
    for p in parts:
        c, t = p.get("control_units"), p.get("test_units")
        if c and t and overall_ratio:
            _, pval = chi_square_p([int(c), int(t)], [overall_ratio, 1 - overall_ratio])
            if pval < 0.001:
                dropped.append(p["segment"])
                continue
        kept.append(p)
    return kept, dropped


def analyse_segments(library, comparison, evidence, prereg, level, planned_text=""):
    """Concentration, segments that don't add up, and per-segment split problems."""
    spec = next((p for p in library["patterns"] if p.get("kind") == "segment"), None)
    base = {m["metric"]: m for m in comparison["metrics"] if " - " not in m["metric"]}
    registered_segments = {str(s).lower() for s in prereg.get("segments") or []}
    groups = segment_groups(comparison, library, evidence)
    tests = max(1, sum(1 for metric, *_ in groups if metric in base))
    instances, results = [], []
    for metric, dim, parts, measured in groups:
        overall = base.get(metric)
        if not overall or not overall.get("rel_ci") or overall.get("rel_delta") is None:
            continue
        cu, tu = overall.get("control_units"), overall.get("test_units")
        ratio = cu / (cu + tu) if cu and tu else None
        units = [p.get("control_units") for p in parts]
        parts, split_off = _segment_srm(parts, ratio) if len(set(units)) > 1 else (parts, [])
        for p in parts:
            if p.get("weight") is None:
                p["weight"] = p.get("control_units") if len(set(units)) > 1 else None
        try:
            res = decompose.segments(overall, parts, level)
        except ValueError:
            continue
        res.update(metric=metric, dimension=dim, measured=measured, split_off=split_off)
        results.append(res)
        planned_by = {str((d.get("needs") or {}).get("by", "")).lower() for pat in library["patterns"] for d in pat.get("deep_dives", [])}
        planned = measured and (dim.lower() in planned_text or any(b and b in dim.lower() for b in planned_by))
        exploratory = not (dim.lower() in registered_segments or any(p["segment"].lower() in registered_segments for p in parts)
                           or planned)
        weighted = all(s["contribution"] is not None for s in res["segments"])
        overall_se = expstats.se_from_ci(overall["rel_ci"][0], overall["rel_ci"][1], level)
        if weighted and abs(res["sum_of_contributions"] - overall["rel_delta"]) > 2 * max(overall_se, 1e-9) + 0.002:
            instances.append({"id": "segment_mix", "name": "Segments don't add up to the total", "label": "measured" if measured else "inferred",
                              "status": "untested", "reading": "neutral", "source": "Dmitriev et al., A Dirty Dozen, KDD 2017 (5.8)",
                              "mechanism": (f"{metric}'s segments by {dim} imply {100 * res['sum_of_contributions']:+.2f}% but the total is "
                                            f"{100 * overall['rel_delta']:+.2f}%: the traffic mix across {dim} probably differs between arms "
                                            "(a mix shift or a segment-level split problem), so read the segments before the total."),
                              "families": [], "metrics": [metric], "evidence": [f"{metric} by {dim}: " + "; ".join(
                                  f"{s['segment']} {100 * s['rel_delta']:+.2f}%" for s in res["segments"])],
                              "score": 3, "exploratory": False, "dimension": dim, "blocking": True,
                              "deep_dives": [{"cut": f"units per {dim} segment by arm (a split test per segment)",
                                              "confirms": "a segment's share differs between arms", "refutes": "every segment is split as planned"},
                                             {"cut": f"mix_shift on {metric} by {dim}", "confirms": "the mix effect carries the total",
                                              "refutes": "rates explain the total"}]})
        threshold = 0.01 / tests
        standout = [s for s in res["segments"] if s["differs_from_pooled"] and s["clear_of_zero"]]
        if not spec or res["heterogeneity_p"] >= threshold or not standout:
            continue
        top = max(res["segments"], key=lambda s: abs(s["contribution"])) if weighted else max(standout, key=lambda s: abs(s["rel_delta"]))
        overall_flat = overall["rel_ci"][0] <= 0 <= overall["rel_ci"][1]
        if overall_flat:
            share = "while the overall change is about zero"
        elif top.get("share_of_overall") is None:
            share = "an unknown share (segment sizes not given)"
        elif top["share_of_overall"] > 1:
            share = "more than all of the overall change (other segments offset it)"
        else:
            share = f"{round(100 * top['share_of_overall'])}% of the overall change"
        values = {"metric": metric, "segment": top["segment"], "share": share}
        instances.append({"id": spec["id"], "name": spec["name"], "label": "measured" if measured else "inferred",
                          "status": "untested", "reading": spec.get("reading", "neutral"), "source": spec.get("source"),
                          "mechanism": fill(spec["mechanism"], values), "families": [], "metrics": [metric],
                          "segment": top["segment"], "dimension": dim, "exploratory": exploratory,
                          "evidence": [f"{metric} by {dim}: " + "; ".join(f"{s['segment']} {100 * s['rel_delta']:+.2f}%" for s in res["segments"])]
                          + ([f"segments dropped for a split problem: {', '.join(split_off)}"] if split_off else []),
                          "score": 2, "deep_dives": [{k: fill(v, values) if isinstance(v, str) else v for k, v in dd.items()}
                                                     for dd in spec["deep_dives"]]})
    return instances, results


# ---------- checks, hypothesis ----------

def check_patterns(library, decision):
    out = []
    explained = {c["check"] for c in decision["checks"] if "explained by" in c.get("summary", "")}
    for spec in (p for p in library["patterns"] if p.get("kind") == "check"):
        wanted = set(spec["check"].split("|"))
        if spec["id"] == "novelty":
            hits = [c for c in decision["checks"] if c["check"] == "novelty" and c["status"] == "flag"
                    and c["details"].get("basis") == "days_since_exposure"]
        else:
            hits = [c for c in decision["checks"] if c["check"] in wanted and c["check"] not in explained and
                    (c["status"] == "fail" or c["status"] == "flag" and c["check"] in ("sample_split", "pre_period"))]
            hits += [{"check": "invariant", "status": "fail", "summary": f"{v['metric']} moved"} for v in decision["verdicts"]
                     if "invariant" in wanted and v["role"] == "invariant" and v["verdict"] == "moved"]
        if hits:
            out.append({"id": spec["id"], "name": spec["name"], "label": "measured", "status": "confirmed by the checks",
                        "mechanism": spec["mechanism"], "reading": spec.get("reading", "bad"), "source": spec.get("source"),
                        "families": [], "metrics": [v["metric"] for v in decision["verdicts"] if v["role"] == "primary"][:1]
                        if spec["id"] == "novelty" else [], "evidence": [h["summary"] for h in hits], "score": 5,
                        "deep_dives": spec["deep_dives"], "exploratory": False,
                        "blocking": spec["id"] == "artifact" and any(h.get("status") == "fail" for h in hits)})
    return out


def hypothesis_candidate(decision, prereg, fam, states, comparison):
    """The registered hypothesis, for a primary that moved the registered way. If the
    hypothesis names success and success didn't rise, it stays a weak candidate."""
    text = prereg.get("hypothesis")
    by_name = {m["metric"].lower(): m for m in comparison["metrics"]}
    named = prereg.get("hypothesis_metric")
    success = by_name.get(str(named).lower()) if named else fam.get("success")
    out = []
    for v in decision["verdicts"]:
        m = by_name.get(v["metric"].lower())
        want = {"increase": "up", "decrease": "down"}.get(v.get("direction"))
        if v["role"] != "primary" or not m or significant(m) != want:
            continue
        evidence = [f"{v['metric']} {change_text(m)}"]
        reading, score = "good", 1
        if success and success is not m:
            evidence.append(f"{success['metric']} {change_text(success)}")
            if significant(success) != "up":
                reading, score = "neutral", 0
        out.append({"id": "as_hypothesized", "name": "The registered hypothesis", "label": "inferred", "status": "untested",
                    "reading": reading, "source": None, "exploratory": False,
                    "mechanism": (text or "The change works the way the experiment was designed to; no hypothesis text was registered.")
                    + (f" But {success['metric']} didn't rise, so the hypothesis isn't supported yet." if reading == "neutral" else ""),
                    "families": [], "metrics": [v["metric"]], "evidence": evidence, "score": score,
                    "deep_dives": [{"cut": f"{v['metric']} on triggered queries (where the ranking changed) vs untriggered",
                                    "confirms": "a larger effect where the ranking changed", "refutes": "the same effect on untouched queries"}]})
    return out


# ---------- cuts as evidence ----------

def config_diff(config, prereg, library):
    if not config:
        return None
    groups = [g for g in config.get("groups", []) if not g.get("disabled")]
    control = next((g for g in groups if g.get("id") == config.get("control_group")), groups[0] if groups else None)
    if not control:
        return None
    diffs = {}
    for g in groups:
        if g is control:
            continue
        for k in set(g.get("parameters", {})) | set(control.get("parameters", {})):
            a, b = control.get("parameters", {}).get(k), g.get("parameters", {}).get(k)
            if a != b:
                diffs[k] = (a, b)
    pattern = library.get("presentation_params", "")
    return {"diffs": diffs, "presentation_changed": any(re.search(pattern, k.lower()) for k in diffs) if pattern else None}


def evaluate_cut(dd, fam, band_of, segments, config_info):
    """Return 'confirms', 'refutes', 'inconclusive' or None (no result available) for a library cut."""
    needs = dd.get("needs")
    if not needs:
        return None, None
    facts = {}
    if "metric" in needs:
        family = needs["metric"]
        if family not in fam:
            return None, None
        facts[family] = state(fam[family], band_of(family))
        detail = f"{fam[family]['metric']} {change_text(fam[family])}"
    elif "segment_of" in needs:
        family = needs["segment_of"]
        metric = fam.get(family, {}).get("metric")
        res = next((r for r in segments if r["metric"] == metric and needs["by"].lower() in r["dimension"].lower()), None)
        if not res:
            return None, None
        facts["even"] = not res["heterogeneous"]
        if res["heterogeneous"]:
            top = max(res["segments"], key=lambda s: abs(s["contribution"] if s["contribution"] is not None else s["rel_delta"]))
            facts["carries_most"] = top["segment"].lower()
        detail = f"{metric} by {res['dimension']}: " + "; ".join(f"{s['segment']} {100 * s['rel_delta']:+.2f}%" for s in res["segments"])
    elif needs.get("config") == "presentation":
        if not config_info or config_info.get("presentation_changed") is None:
            return None, None
        facts["presentation_changed"] = config_info["presentation_changed"]
        diffs = config_info["diffs"]
        detail = "config differs in " + (", ".join(f"{k} ({a} -> {b})" for k, (a, b) in diffs.items()) or "nothing")
    else:
        return None, None

    def holds(conds):
        for key, want in (conds or {}).items():
            have = facts.get(key)
            if key == "carries_most":
                if have is None or not have.startswith(str(want).lower()):
                    return False
            elif isinstance(want, bool):
                if have is not want:
                    return False
            elif have not in STATE_OK.get(want, {want}):
                return False
        return bool(conds)

    if holds(dd.get("refutes_when")):
        return "refutes", detail
    rel = _relative(dd, fam)
    if rel:
        return rel, detail
    if holds(dd.get("confirms_when")):
        return "confirms", detail
    return "inconclusive", detail


def _relative(dd, fam):
    """A control cut read against a reference effect ("relative_to": family, share, expect).
    expect "absent": it confirms when its interval rules out `share` of the reference change
    (the effect sits where the mechanism acts, not here). expect "present": it refutes when its
    interval rules out `share` of the reference change (the effect isn't here, so it isn't
    everywhere). Without the reference metric, the cut's own conditions decide."""
    spec = dd.get("relative_to")
    family = (dd.get("needs") or {}).get("metric")
    if not spec or spec.get("family") not in fam or family not in fam:
        return None
    ref, _ = rel_interval(fam[spec["family"]])
    _, ci = rel_interval(fam[family])
    if ref is None or ci is None or ref == 0:
        return None
    edge = spec.get("share", 0.5) * ref
    lo, hi = min(ci), max(ci)
    rules_out = lo > edge if ref < 0 else hi < edge
    if spec.get("expect") == "absent":
        return "confirms" if rules_out else None
    return "refutes" if rules_out else None


def _explicit_match(e, inst, n, dd):
    if e.get("pattern") and e["pattern"] != inst["id"]:
        return False
    if e.get("sql"):
        return e["sql"] == dd.get("sql")
    return e.get("cut") == n or (isinstance(e.get("cut"), str) and e["cut"].lower() in dd["cut"].lower())


def apply_evidence(instances, fam, band_of, segments, config_info, evidence, needs_control=False):
    """Score each candidate's cuts. A refuting cut rules it out. A confirming cut makes it
    "confirmed by a cut", except: a result with a caveat (it compares different query sets)
    can't confirm, and with needs_control the candidate also needs its control cut (the one
    whose result separates it from the usual look-alikes, role "control") to confirm. Short of
    that, a confirming result makes it "fits a cut", and `awaiting` names what's missing."""
    explicit = (evidence or {}).get("cuts") or []
    kept, refuted, run = [], [], []
    for inst in instances:
        results = []
        for n, dd in enumerate(inst["deep_dives"], 1):
            outcome, detail = evaluate_cut(dd, fam, band_of, segments, config_info)
            caveat = None
            for e in explicit:
                if _explicit_match(e, inst, n, dd):
                    if e.get("outcome"):
                        outcome, detail = e["outcome"], e.get("evidence", "explicit outcome")
                    caveat = e.get("caveat") or caveat
            if outcome:
                results.append({"cut": dd["cut"], "outcome": outcome, "detail": detail, "role": dd.get("role", "where"),
                                **({"caveat": caveat} if caveat else {})})
                run.append(dd["cut"].lower())
        inst["cut_results"] = results
        if any(r["outcome"] == "refutes" for r in results):
            inst["status"] = "refuted by a cut"
            inst["ruled_out_by"] = [f"{r['cut']}: {r['detail']}" for r in results if r["outcome"] == "refutes"]
            refuted.append(inst)
            continue
        agree = [r for r in results if r["outcome"] == "confirms"]
        where = [r for r in agree if r["role"] != "control" and not r.get("caveat")]
        controls = [dd for dd in inst["deep_dives"] if dd.get("role") == "control"]
        control_ok = all(any(r["cut"] == dd["cut"] and r["outcome"] == "confirms" and not r.get("caveat") for r in results)
                         for dd in controls)
        if not needs_control:
            control_ok, where = True, [r for r in agree if not r.get("caveat")]
        if where and control_ok and (controls or not needs_control):
            inst["status"], inst["label"], inst["exploratory"] = "confirmed by a cut", "measured", False
        elif agree:
            inst["status"], inst["label"], inst["exploratory"] = "fits a cut", "measured", False
            missing = [dd["cut"] for dd in controls if not any(r["cut"] == dd["cut"] and r["outcome"] == "confirms" for r in results)]
            caveats = sorted({r["caveat"] for r in agree if r.get("caveat")})
            inst["awaiting"] = ([f"a check that compares the same query sets ({c})" for c in caveats]
                                + (missing if controls else ["a control cut (none in the library)"] if needs_control else []))
        if agree:
            inst["evidence"] = inst["evidence"] + [f"{r['cut']}: {r['detail']}" for r in agree]
        kept.append(inst)
    return kept, refuted, set(run)


# ---------- questions, readiness, plan ----------

def build_questions(decision, comparison, instances, trusted, segment_instances):
    by_name = {m["metric"].lower(): m for m in comparison["metrics"]}
    questions, seen = [], set()

    def add(metric, text, kind):
        key = metric.lower()
        if key in seen or key not in by_name:
            return
        seen.add(key)
        related = sorted((i for i in instances if metric in i["metrics"] and not i.get("exploratory")),
                         key=lambda i: (not i["status"].startswith("confirmed"), -i["score"]))
        if any(i["status"].startswith("confirmed") for i in related):
            status = "confirmed by a cut" if any(i["status"] == "confirmed by a cut" for i in related) else "confirmed by the scorecard"
        elif any(i["status"] == "fits a cut" for i in related):
            status = "fits a cut, not yet confirmed"
        elif related:
            status = "untested"
        else:
            status = "no candidate in the library" if key in trusted else "possibly chance"
        questions.append({"metric": metric, "question": text, "kind": kind, "change": change_text(by_name[key]),
                          "candidates": [i["id"] for i in related], "status": status})

    verbs = {"up": "rise", "down": "fall"}
    decider = decision["decider"].get("metric")
    order = sorted(decision["verdicts"], key=lambda v: (v["metric"] != decider, v["role"] != "primary"))
    for v in order:
        m = by_name.get(v["metric"].lower())
        if not m:
            continue
        sig = significant(m)
        want = {"increase": "up", "decrease": "down"}.get(v.get("direction"))
        if v["role"] == "primary":
            if sig == want:
                add(v["metric"], f"Why did {v['metric']} {verbs[sig]}?", "primary")
            elif sig == "flat":
                add(v["metric"], f"{v['metric']} was expected to {verbs.get(want, 'move')} but didn't move. Why?", "primary")
            elif sig:
                add(v["metric"], f"{v['metric']} was expected to {verbs.get(want, 'move')} but {'rose' if sig == 'up' else 'fell'}. Why?", "primary")
        elif v["role"] == "guardrail" and sig in ("up", "down") and sig != want:
            add(v["metric"], f"Why did {v['metric']} {verbs[sig]}?", "guardrail")
    segmented = {i["metrics"][0].lower() for i in segment_instances}
    for s in decision["secondaries"]["survivors"]:
        m = by_name.get(s["metric"].lower())
        if m and not (" - " in s["metric"] and s["metric"].rsplit(" - ", 1)[0].lower() in segmented) and significant(m) in verbs:
            add(s["metric"], f"Why did {s['metric']} {verbs[significant(m)]}?", "secondary")
    for v in decision["verdicts"]:
        m = by_name.get(v["metric"].lower())
        want = {"increase": "up", "decrease": "down"}.get(v.get("direction"))
        if v["role"] == "guardrail" and m and significant(m) == want and v["metric"].lower() not in seen:
            add(v["metric"], f"{v['metric']} improved. Is that expected?", "improvement")
            if questions and questions[-1]["metric"] == v["metric"] and not questions[-1]["candidates"]:
                questions[-1]["status"] = "a good surprise to mention"
    if decision["call"] == "no_effect":
        for q in questions:
            if q["kind"] == "primary" and "didn't move" in q["question"] and not q["candidates"]:
                q["status"] = "answered by the call: the interval rules out the bar"
    for inst in segment_instances:
        if inst["id"] != "concentrated_segment" or inst.get("exploratory"):
            continue
        key = f"{inst['metrics'][0]} [{inst['segment']}]"
        if key not in seen:
            seen.add(key)
            questions.append({"metric": inst["metrics"][0], "question": f"Why is {inst['metrics'][0]}'s change concentrated in {inst['segment']}?",
                              "kind": "segment", "change": inst["evidence"][0], "candidates": [inst["id"]], "status": inst["status"]})
    return questions


def chance_count(comparison, level):
    tested = [m for m in comparison["metrics"] if m.get("rel_ci") or m.get("abs_ci")]
    moved_ = [m for m in tested if significant(m) in ("up", "down")]
    return {"tested": len(tested), "moved": len(moved_), "expected_by_chance": round(len(tested) * (1 - level), 1)}


def standard_questions(decision, prereg, comparison, chance, config_info, fam):
    checks = {c["check"]: c for c in decision["checks"]}
    problems = [c for c in decision["checks"] if c["status"] == "fail" or
                (c["status"] == "flag" and c["check"] in ("sample_split", "pre_period") and "explained" not in c["summary"])]
    notes = [c for c in decision["checks"] if c["status"] == "flag" and c not in problems and c["check"] != "novelty"]
    unrun = [c["check"].replace("_", " ") for c in decision["checks"] if c["status"] == "could_not_run" and c["check"] != "novelty"]
    if problems:
        trust = "No: " + "; ".join(c["summary"] for c in problems[:2])
    else:
        trust = "Yes, as far as the checks reach"
        if notes:
            trust += "; note: " + "; ".join(c["summary"] for c in notes[:2])
        if unrun:
            trust += f"; not checked: {', '.join(unrun)}"
    decider = decision["decider"]
    judged = decider if decider.get("se") else next((v for v in decision["verdicts"] if v["role"] == "primary" and v.get("se")), None)
    borderline = False
    if judged:
        p = expstats.two_sided_p(judged["point"], judged["se"])
        borderline = 0.01 < p < 0.05
        clear = judged["low"] > 0 or judged["high"] < 0
        real = (f"{judged['metric']} {describe(judged)}; its interval {'excludes' if clear else 'includes'} zero"
                + (f", but p is about {p:.2f}, and results that borderline often fail a rerun" if borderline else "")
                + f". Across the scorecard {chance['moved']} of {chance['tested']} metrics moved clearly; about "
                  f"{chance['expected_by_chance']:g} would by chance alone")
    else:
        real = "No interval for the deciding metric"
    novelty = checks.get("novelty")
    basis = (novelty or {}).get("details", {}).get("basis")
    if novelty and novelty["status"] == "pass":
        last = "Probably: " + novelty["summary"]
    elif novelty and novelty["status"] == "flag" and basis == "days_since_exposure":
        last = "Maybe not: " + novelty["summary"]
    elif novelty and novelty["status"] == "flag":
        last = ("Unknown: only a calendar-day series, and it fades, but calendar days mix day of week with late joiners. "
                "Cut the primary by days since first exposure before triage")
    else:
        last = "Unknown: there is no effect series by days since first exposure (4 days or more). Export it before triage"
    if config_info and config_info["diffs"]:
        changed = "; ".join(f"{k}: {a} -> {b}" for k, (a, b) in config_info["diffs"].items())
        intended = set(prereg.get("intended_params") or [])
        extra = [k for k in config_info["diffs"] if intended and k not in intended]
        what = changed + (f". Not in the pre-registration: {', '.join(extra)}" if extra else "; nothing else differs between arms")
    else:
        what = "Not shown: no experiment config was given"
    features = prereg.get("feature_metrics") or []
    by_name = {m["metric"].lower(): m for m in comparison["metrics"]}
    shown = [(f, by_name.get(str(f).lower())) for f in features]
    if not features:
        did = ("Not shown. If the scorecard or a cut has a coverage metric (the share of queries whose top results changed), "
               "register it as feature_metrics")
        did_ok = None
    else:
        did = "; ".join(f"{f} {change_text(m)}" if m else f"{f} is not in the scorecard" for f, m in shown)
        did_ok = all(m and significant(m) in ("up", "down") for f, m in shown) if all(m for f, m in shown) else None
    family_line = ", ".join(f"{f} = {m['metric']}" for f, m in sorted(fam.items()))
    return [{"question": "What did you change?", "answer": what, "ok": True},
            {"question": "Is the data trustworthy?", "answer": trust, "ok": not problems},
            {"question": "Is the effect real, or chance?", "answer": real, "ok": bool(judged) and not borderline, "borderline": borderline},
            {"question": "Will it last?", "answer": last, "ok": bool(novelty and novelty["status"] == "pass")},
            {"question": "Did the change do what it was built to do?", "answer": did, "ok": did_ok},
            {"question": "Metric families used:", "answer": family_line or "no metric matched a family", "ok": True, "note": True}]


def readiness(decision, instances, questions, standard, notes):
    by_id = {}
    for inst in instances:
        by_id.setdefault(inst["id"], inst)
    reasons, first = [], None
    if decision["call"] in ("cant_tell", "not_ab"):
        reasons.append(f"the call is {decision['call_text']}: {decision['reason']}")
    if decision["call"] == "keep_running":
        reasons.append(f"the test needs more data: {decision['reason']}")
    blocking = [i for i in instances if i.get("blocking")]
    if blocking:
        reasons.append("a data problem is unresolved: " + "; ".join(blocking[0]["evidence"][:2]))
        first = first or (blocking[0]["deep_dives"][0] if blocking[0]["deep_dives"] else None)
    if any(i["id"] == "novelty" for i in instances) and decision["call"] != "keep_running":
        reasons.append("the effect is fading by days since exposure, so the scorecard average overstates the long-run effect")
    did = next((q for q in standard if q["question"].startswith("Did the change")), None)
    if did and did["ok"] is False:
        reasons.append("the change may not have reached users: " + did["answer"])
    if decision["call"] == "ship":
        for q in (q for q in questions if q["kind"] == "primary"):
            live = [by_id[c] for c in q["candidates"] if c in by_id]
            if live and live[0]["reading"] == "bad" and not any(i["reading"] == "good" and i["score"] > 0 for i in live):
                reasons.append(f"the scripted call is Ship, but the best explanation of {q['metric']} is bad news: "
                               f"'{live[0]['name']}'")
                scriptable = [d for d in live[0]["deep_dives"] if d.get("needs")]
                first = first or (scriptable or live[0]["deep_dives"] or [None])[0]
                break
    registered = [q for q in questions if q["kind"] in ("primary", "guardrail")]
    for q in registered:
        live = [by_id[c] for c in q["candidates"] if c in by_id]
        good = [i for i in live if i["reading"] == "good"]
        bad = [i for i in live if i["reading"] == "bad"]
        if good and bad:
            if any(i["status"].startswith("confirmed") for i in bad) and not any(i["status"].startswith("confirmed") for i in good):
                continue
            if any(i["status"].startswith("confirmed") for i in good) and not any(i["status"].startswith("confirmed") for i in bad):
                continue
            text = (f"{q['metric']} has two opposite explanations, '{good[0]['name']}' and '{bad[0]['name']}'")
            if decision["call"] in ("ship", "escalate"):
                reasons.append(text + ", and they lead to different decisions")
                scriptable = [d for d in bad[0]["deep_dives"] if d.get("needs")]
                first = first or (scriptable or bad[0]["deep_dives"] or [None])[0]
            else:
                decision.setdefault("_open", []).append(text + "; it doesn't change the call, but triage will ask")
            break
    if reasons:
        return {"verdict": "not_ready", "text": "Not ready", "reasons": reasons, "first": first}
    parts = []
    real = next((q for q in standard if q["question"].startswith("Is the effect real")), None)
    if real and real.get("borderline"):
        parts.append("the deciding result is borderline (p between 0.01 and 0.05); expect triage to ask for a rerun")
    last = next((q for q in standard if q["question"] == "Will it last?"), None)
    if last and not last["ok"]:
        parts.append("durability is unknown: " + last["answer"].split(":", 1)[-1].strip())
    if did and did["ok"] is None and (decision.get("_features_declared")):
        parts.append("the coverage metric isn't in the scorecard")
    unexplained = [q for q in questions if q["status"] == "no candidate in the library"]
    if unexplained:
        parts.append(f"{len(unexplained)} of {len(questions)} movements have no candidate in the library: "
                     + ", ".join(q["metric"] for q in unexplained[:3]))
    noisy = [v for v in decision["verdicts"] if v["role"] == "primary" and v.get("se") and v.get("threshold")
             and (v["high"] - v["low"]) / 2 > 2 * abs(v["threshold"])]
    if noisy:
        parts.append(f"{noisy[0]['metric']} is too noisy to read ({describe(noisy[0])}); a deep dive can't settle what it can't measure")
    weak = [q for q in registered if q["candidates"] and all(by_id[c]["score"] == 0 for c in q["candidates"] if c in by_id)]
    if weak:
        parts.append(f"{', '.join(q['metric'] for q in weak)} rest on a weak explanation")
    parts += [n for n in notes if "different population" in n]
    parts += decision.get("_open", [])
    registered_names = {q["metric"] for q in registered}
    exploratory = [i for i in instances if i.get("exploratory") and set(i["metrics"]) & registered_names]
    if exploratory:
        parts.append("exploratory only, not for the decision: " + "; ".join(
            f"{i['name']}" + (f" ({i['segment']})" if i.get("segment") else "") for i in exploratory[:2])
            + "; confirm on a rerun or a pre-registered cut")
    if parts:
        return {"verdict": "open_questions", "text": "Ready, with open questions", "reasons": parts, "first": None}
    confirmed = all(any(by_id[c]["status"].startswith("confirmed") for c in q["candidates"] if c in by_id) for q in registered)
    note = "" if confirmed else "; the explanations are inferred from co-movements, so the cuts below would confirm them"
    return {"verdict": "ready", "text": "Ready", "first": None,
            "reasons": [f"every movement triage will ask about has a candidate explanation and none conflict{note}"]}


def plan(instances, run, limit=5):
    live = [i for i in instances if not i.get("exploratory")]
    order = sorted(live, key=lambda i: (not i.get("blocking"), i["id"] not in ("artifact", "segment_mix"),
                                        i["status"].startswith("confirmed"), -i["score"]))
    cuts, seen = [], set(run)
    for depth in range(5):
        for inst in order:
            dives = [d for d in inst["deep_dives"] if d["cut"].lower() not in seen]
            dives.sort(key=lambda d: not d.get("needs"))
            if len(dives) > depth - (len(inst["deep_dives"]) - len(dives)) and dives:
                d = dives[0]
                seen.add(d["cut"].lower())
                cuts.append({**d, "tests": inst["name"], "pattern": inst["id"], "scriptable": bool(d.get("needs"))})
            if len(cuts) >= limit:
                return cuts
    return cuts


def explain(decision, scorecard, prereg, library, evidence=None, config=None):
    group = decision["experiment"].get("test_group")
    comparison = next((c for c in scorecard["comparisons"] if c.get("test_group") == group), scorecard["comparisons"][0])
    comparison = {**comparison, "metrics": list(comparison["metrics"]) + [
        {**m, "listed_as": "evidence"} for m in (evidence or {}).get("metrics", [])]}
    level = decision["settings"].get("scorecard_ci_level") or decision["settings"]["ci_level"]
    registered = {v["metric"].lower() for v in decision["verdicts"]}
    survivors = {s["metric"].lower() for s in decision["secondaries"]["survivors"]}
    trusted = registered | survivors | {m["metric"].lower() for m in (evidence or {}).get("metrics", [])}
    fam, notes = map_families(library, comparison["metrics"], registered)
    band_of = lambda f: library["families"].get(f, {}).get("practical", DEFAULT_BAND)  # noqa: E731
    states = {f: state(m, band_of(f)) for f, m in fam.items()}
    states = {f: s for f, s in states.items() if s}
    found, ruled_out = match_comovement(library, fam, states, trusted, band_of)
    ratio_found, ratio_notes = ratio_patterns(library, fam, level)
    notes += ratio_notes
    found += ratio_found
    planned_text = " ".join(d["cut"].lower() for i in found for d in i.get("deep_dives", []))
    planned_text += " " + " ".join(d["cut"].lower() for p in library["patterns"] for d in p.get("deep_dives", []))
    seg_found, seg_results = analyse_segments(library, comparison, evidence, prereg, level, planned_text)
    found += seg_found
    found += check_patterns(library, decision)
    found += hypothesis_candidate(decision, prereg, fam, states, comparison)
    config_info = config_diff(config, prereg, library)
    found, refuted, run = apply_evidence(found, fam, band_of, seg_results, config_info, evidence,
                                         library.get("confirm_needs_control", False))
    ruled_out += refuted
    questions = build_questions(decision, comparison, found, trusted, seg_found)
    chance = chance_count(comparison, level)
    standard = standard_questions(decision, prereg, comparison, chance, config_info, fam)
    decision = {**decision, "_features_declared": bool(prereg.get("feature_metrics"))}
    for inst in seg_found:
        if inst["id"] == "segment_mix":
            notes.append(inst["mechanism"].split(":")[0] + ": segments don't add up")
    ready = readiness(decision, found, questions, standard, notes)
    cut_results = [dict(r, pattern=i["id"], tests=i["name"]) for i in found + refuted for r in i.get("cut_results", [])]
    return {"readiness": ready, "standard": standard, "chance": chance, "questions": questions,
            "patterns": sorted([i for i in found if not i.get("exploratory")], key=lambda i: -i["score"]),
            "exploratory": [i for i in found if i.get("exploratory")], "ruled_out": ruled_out,
            "cut_results": cut_results, "deep_dives": plan(found, run), "families": {f: m["metric"] for f, m in fam.items()},
            "notes": notes}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--decision", required=True)
    parser.add_argument("--scorecard", required=True)
    parser.add_argument("--prereg", required=True)
    parser.add_argument("--config")
    parser.add_argument("--evidence", help="evidence.json from deep-dive cuts")
    parser.add_argument("--segments", help="older form: segments only (same as evidence.segments)")
    parser.add_argument("--overlay", default=os.environ.get("DS_OVERLAY_DIR"))
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        load = lambda p: json.load(open(p, encoding="utf-8")) if p else None  # noqa: E731
        evidence = load(args.evidence) or {}
        if args.segments:
            evidence["segments"] = {**(evidence.get("segments") or {}), **load(args.segments)}
        result = explain(load(args.decision), load(args.scorecard), load(args.prereg), load_library(args.overlay),
                         evidence, load(args.config))
    except (OSError, ValueError, KeyError, TypeError) as err:
        print(f"explain: {type(err).__name__}: {err}", file=sys.stderr)
        return 2
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.decision)), "explain.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    r = result["readiness"]
    print(f"triage readiness: {r['text']} - {'; '.join(r['reasons'])}")
    for q in result["questions"]:
        print(f"question: {q['question']} [{q['status']}: {', '.join(q['candidates']) or '-'}]")
    for c in result["cut_results"]:
        print(f"cut result: {c['outcome']} '{c['tests']}' - {c['cut']}")
    for d in result["deep_dives"]:
        print(f"deep dive: {d['cut']} (tests: {d['tests']})")
    for n in result["notes"]:
        print(f"note: {n}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
