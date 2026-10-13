"""Typed lessons, and where they come from.

A lesson is one bounded change to the knowledge folder:

| type           | changes                                  | evaluation                         |
|----------------|------------------------------------------|------------------------------------|
| metric_mapping | metric_families.json: a family gets a name | deterministic replay, exact diff   |
| metric_alias   | metric_aliases.json: a scorecard name reads as a contract name | deterministic replay |
| pattern        | patterns.json: add or replace a pattern  | deterministic replay, exact diff   |
| cut_template   | cut_templates.json: add a template       | deterministic replay (+ dry run)   |
| readout_rule   | readout_rules.json: a writing rule       | model-facing: 3+ runs, lint counts |
| experiment_card| cards/: a card becomes confirmed         | none: a person writes it           |

No type touches M0 check logic, the grade, or the cut runner; those change only through
engineering review, outside this loop. A lesson records the flights it came from, which are
held out of its evaluation.

Where candidates come from (the only evolution kept from EvoOntology-style loops):
- a review's notes (what the reviewer said was wrong or missing);
- a T1 metric that maps to no family but shares words with one (a mapping proposal);
- a contract metric missing from the scorecard while a renamed one is there (an alias
  proposal: a person checks the definition didn't change before approving);
- a metric that moved with no candidate in three or more flights (a pattern stub for a
  person to fill in).
"""

import copy
import hashlib
import json
import os
import re
import shutil
import time

TYPES = ("metric_mapping", "metric_alias", "pattern", "cut_template", "readout_rule", "experiment_card")
DETERMINISTIC = ("metric_mapping", "metric_alias", "pattern", "cut_template")


def new(kind, change, source_flights, rationale, target_cases=None, author="agent"):
    if kind not in TYPES:
        raise ValueError(f"lesson type must be one of {', '.join(TYPES)}")
    digest = hashlib.sha256(json.dumps(change, sort_keys=True).encode()).hexdigest()[:8]
    return {"id": f"{kind}-{digest}",
            "type": kind, "change": change, "source_flights": sorted(set(source_flights)), "rationale": rationale,
            "target_cases": sorted(set(target_cases or [])), "status": "candidate", "author": author,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _load(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return copy.deepcopy(default)


def _save(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1)


def apply(lesson, knowledge_dir):
    """Apply a deterministic or readout lesson to knowledge_dir in place."""
    kind, change = lesson["type"], lesson["change"]
    os.makedirs(knowledge_dir, exist_ok=True)
    if kind == "metric_mapping":
        path = os.path.join(knowledge_dir, "metric_families.json")
        names = _load(path, {})
        current = names.get(change["family"], [])
        current = current if isinstance(current, list) else [current]
        names[change["family"]] = list(dict.fromkeys([change["metric"]] + current))
        _save(path, names)
    elif kind == "metric_alias":
        path = os.path.join(knowledge_dir, "metric_aliases.json")
        aliases = _load(path, {})
        aliases[change["scorecard_name"]] = change["contract_name"]
        _save(path, aliases)
    elif kind == "pattern":
        path = os.path.join(knowledge_dir, "patterns.json")
        doc = _load(path, {"patterns": [], "families": {}, "ratios": {}})
        doc["patterns"] = [p for p in doc["patterns"] if p["id"] != change["pattern"]["id"]] + [change["pattern"]]
        doc.setdefault("families", {}).update(change.get("families", {}))
        _save(path, doc)
    elif kind == "cut_template":
        path = os.path.join(knowledge_dir, "cut_templates.json")
        doc = _load(path, {})
        doc[change["id"]] = change["template"]
        _save(path, doc)
        if change.get("pattern"):
            apply({"type": "pattern", "change": {"pattern": change["pattern"], "families": change.get("families", {})}},
                  knowledge_dir)
    elif kind == "readout_rule":
        path = os.path.join(knowledge_dir, "readout_rules.json")
        doc = _load(path, {"rules": []})
        doc["rules"].append(change["rule"])
        _save(path, doc)
    else:
        raise ValueError(f"{kind} lessons are not applied to the knowledge folder")


def candidate_knowledge(parent_dir, lesson, out_dir):
    """A copy of the parent knowledge with the lesson applied."""
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    shutil.copytree(parent_dir, out_dir)
    apply(lesson, out_dir)
    return out_dir


# ---------- proposals ----------

def _words(text):
    return set(re.findall(r"[a-z0-9]+", str(text).lower())) - {"rate", "per", "the", "of", "share", "query", "queries"}


def propose_mappings(scorecard, library, flight):
    """T1 metrics no family claims, but whose words overlap one family's name (a person decides)."""
    import explain
    out = []
    claimed = {m["metric"].lower() for f, names in library.get("exact_families", {}).items() for m in
               [{"metric": n} for n in (names if isinstance(names, list) else [names])]}
    for c in scorecard["comparisons"][:1]:
        for m in c["metrics"]:
            if m["metric"].lower() in claimed or " - " in m["metric"] or explain.family_candidates(library, m):
                continue
            vocab = {f: _words(f.replace("_", " ")) | {w for pat in spec["match"] for w in _words(re.sub(r"[\\^$?*+()\[\]|.]", " ", pat))}
                     for f, spec in library["families"].items()}
            best = max(vocab, key=lambda f: len(vocab[f] & _words(m["metric"])))
            if len(vocab[best] & _words(m["metric"])) >= 2:
                out.append(new("metric_mapping", {"family": best, "metric": m["metric"]}, [flight],
                               f"'{m['metric']}' matched no family; its name shares words with '{best}'"))
    return out


def propose_aliases(scorecard, contract, flight, aliases=None):
    """A contract metric missing from the scorecard, and one scorecard name that contains every
    word of it (e.g. 'Search Success Rate v2'): propose reading that name as the contract's."""
    out = []
    specs = ((contract.get("primaries") or {}).get("metrics") or []) + (contract.get("guardrails") or [])
    names = [m["metric"] for m in scorecard["comparisons"][0]["metrics"]] if scorecard.get("comparisons") else []
    present = {n.lower() for n in names} | {v.lower() for v in (aliases or {}).values()}
    for spec in specs:
        want = spec["metric"]
        if want.lower() in present:
            continue
        words = set(re.findall(r"[a-z0-9]+", want.lower()))
        hits = [n for n in names if words <= set(re.findall(r"[a-z0-9]+", n.lower())) and n.lower() != want.lower()]
        if len(hits) > 1:  # several contain it: keep the ones that only add a version word ('v2', 'new', '2026')
            hits = [n for n in hits if all(re.fullmatch(r"v\d+|new|updated|beta|\d+", w)
                                           for w in set(re.findall(r"[a-z0-9]+", n.lower())) - words)]
        if len(hits) == 1:
            out.append(new("metric_alias", {"scorecard_name": hits[0], "contract_name": want}, [flight],
                           f"the contract names '{want}', which the scorecard lacks; it has '{hits[0]}'. Approve only if the "
                           "definition is the same"))
    return out


def propose_patterns(traces, min_flights=3):
    """A metric that moved with no candidate in min_flights or more flights becomes a pattern stub."""
    seen = {}
    for t in traces:
        blocked = {pair for pair, g in t.get("counts", {}).get("grades", {}).items() if g == "not_permitted"}
        for c in t.get("claim_records", []):
            if c.get("pair") in blocked:
                continue  # movements on a blocked pair aren't interpretable, so they teach nothing
            if c["support_level"] == "none" and not c["change"].startswith("didn't move"):
                seen.setdefault(c["metric"], set()).add(t["flight"])
    out = []
    for metric, flights in sorted(seen.items()):
        if len(flights) >= min_flights:
            out.append(new("pattern", {"pattern": {"id": f"stub_{re.sub(r'[^a-z0-9]+', '_', metric.lower()).strip('_')}",
                                                   "name": f"(to fill in) why {metric} moves", "when": {}, "mechanism": "",
                                                   "deep_dives": [], "reading": "neutral", "stub": True}},
                           flights, f"{metric} moved with no candidate in {len(flights)} flights", author="agent"))
    return out


def from_review(review, flight):
    """A reviewer's 'missing' notes, and 'wrong' verdicts with a note, become readout-rule candidates.
    Readout rules are model-facing, so they wait for a model-facing evaluation before any approval."""
    out = []
    for c in review.get("claims") or []:
        if c.get("verdict") == "wrong" and (c.get("note") or "").strip():
            rule = {"rule": f"{c['metric']}: {c['note'].strip()}", "metric": c["metric"]}
            out.append(new("readout_rule", rule, [flight], f"reviewer marked claim {c['claim_id']} wrong",
                           author=review.get("reviewer") or "reviewer"))
    for note in review.get("missing") or []:
        if isinstance(note, dict) and note.get("lesson"):
            kind = note.get("type", "readout_rule")
            out.append(new(kind, note["lesson"], [flight], note.get("why", "from review"), note.get("targets"),
                           author=review.get("reviewer") or "reviewer"))
        elif isinstance(note, str):
            out.append(new("readout_rule", {"rule": note}, [flight], "reviewer note", author=review.get("reviewer") or "reviewer"))
    return out


def review_stats(reviews):
    claims = [c for r in reviews for c in r.get("claims", []) if c.get("verdict")]
    checks = [c for r in reviews for c in r.get("checks", []) if c.get("useful") is not None]
    count = lambda v: sum(1 for c in claims if c["verdict"] == v)  # noqa: E731
    return {"reviews": len(reviews), "claims_graded": len(claims), "correct": count("correct"), "wrong": count("wrong"),
            "missing": count("missing"), "checks_graded": len(checks), "useful_checks": sum(1 for c in checks if c["useful"])}
