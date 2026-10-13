"""Paired evaluation of one lesson, and the approval gate.

- Cases are frozen runs (learning/cases/<flight>/): the recorded aggregate results, the
  config and the contract. A replay answers from the recording, so a case doesn't drift as
  T1 is rewritten or expires; only a statement the recording lacks (a new cut template) goes
  to the live warehouse, if one is given.
- The flights a lesson came from are held out: they are reported as fit cases and never
  count towards a pass.
- Parent and candidate knowledge replay the same cases with the same code. Deterministic
  lessons need one replay each, since the pipeline has no randomness.
- A held-out case that changes (grade, call, candidate set or plan) has improved only when
  its planted mechanism (synthetic cases with gold) moves up from unexplained to likely to
  confirmed, or, at work, when a reviewer marked a claim on it wrong or missing and that claim
  changed while every claim the reviewer marked correct stayed. Any other change is a
  regression, whether or not the lesson named the case as a target. Fewer unexplained
  movements is not an improvement on its own: a vacuous pattern achieves that.
- A lesson passes with no regression, at least one improved case, and every named target
  improved. Then a person approves it; approval re-runs the evaluation, applies the lesson and
  records the new knowledge version. Nothing is applied without both.
"""

import hashlib
import json
import os
import shutil
import tempfile
import time

from learning import knowledge, lessons
from kangaroo import run as runner
from kangaroo.m1 import cuts as cutlib
from kangaroo.sources.warehouse import FrozenWarehouse


class FrozenConfig:
    command = "the case's frozen config (none was recorded)"

    def __init__(self, cfg):
        self.cfg = cfg

    def get(self, config_id):
        return self.cfg if self.cfg and self.cfg.get("id") == config_id else None

    def find(self, name):
        return [self.cfg] if self.cfg and name in (self.cfg.get("experiment_name"), self.cfg.get("name")) else []


def freeze(run_dir, cases_root, gold=None):
    """Copy a run's case inputs into the case library."""
    with open(os.path.join(run_dir, "case", "meta.json"), encoding="utf-8") as handle:
        meta = json.load(handle)
    dest = os.path.join(cases_root, meta["experiment"])
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(os.path.join(run_dir, "case"), dest)
    review = _load(os.path.join(run_dir, "review.json"))
    if review and review.get("reviewer"):
        shutil.copy(os.path.join(run_dir, "review.json"), os.path.join(dest, "review.json"))
    meta["frozen"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if gold:
        meta["gold"] = gold
    with open(os.path.join(dest, "meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=1)
    return dest


def _case(case_dir):
    out = []
    for name in ("meta.json", "config.json"):
        with open(os.path.join(case_dir, name), encoding="utf-8") as handle:
            out.append(json.load(handle))
    return tuple(out)


def _load(path, default=None):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return default


def replay(case_dir, overlay, knowledge_dir, live=None):
    """Re-run a frozen case: its recorded results, config and contract (not the live overlay's)."""
    meta, cfg = _case(case_dir)
    out = tempfile.mkdtemp(prefix="replay-")
    try:
        result = runner.run(meta["experiment"], overlay, FrozenWarehouse(os.path.join(case_dir, "queries.json"), live),
                            FrozenConfig(cfg), out, knowledge_dir, contract=_load(os.path.join(case_dir, "contract.json")))
    finally:
        shutil.rmtree(out, ignore_errors=True)
    return outcome(result)


def outcome(result):
    pairs = {}
    for p in result["pairs"]:
        e = p["explained"]
        pairs[p["test"]] = {
            "grade": p["grade"]["grade"], "call": p["decision"]["call"],
            "candidates": sorted(f"{i['id']}:{i['status']}" for i in e["patterns"]),
            "plan": [d["cut"] for d in e["deep_dives"]],
            "manual_checks": sum(1 for d in e["deep_dives"] if not d.get("sql")),
            "confirmed": sorted({c["candidate_id"] for c in p["claims"] if c["support_level"] == "confirmed"}),
            "likely": sorted({c["candidate_id"] for c in p["claims"] if c["support_level"] in ("inferred", "consistent")}),
            "claims": {c["metric"].lower(): [c["candidate_id"], c["support_level"]] for c in p["claims"]},
            "unexplained": sorted(c["metric"] for c in p["claims"]
                                  if not c["candidate_id"] and not c["change"].startswith("didn't move"))}
    return pairs


def gold_score(o, gold):
    """2 if the planted mechanism is confirmed in every pair, 1 if likely, 0 otherwise; a pair
    that also confirms a mechanism that wasn't planted scores 0 (a confirmation that only means
    "consistent with" is the failure this guards against)."""
    want = gold.get("mechanism")
    also = set(gold.get("also") or [])  # mechanisms planted alongside, e.g. a second change
    grades = gold.get("grades") or {}
    scores = []
    for name, pair in o.items():
        if grades.get(name) == "not_permitted":  # an arm that should be blocked scores on the block alone
            scores.append(2 if pair["grade"] == "not_permitted" else 0)
        elif want in (None, "none"):
            scores.append(2 if not pair["confirmed"] else 0)
        elif want == "artifact":
            scores.append(2 if pair["grade"] == "not_permitted" else 0)
        else:
            extra = set(pair["confirmed"]) - {want, "as_hypothesized", "novelty"} - also
            scores.append(0 if extra else 2 if want in pair["confirmed"] else 1 if want in pair["likely"] else 0)
    return min(scores) if scores else 0


def grade_score(o, gold):
    """How many pairs get the grade the flight should get (gold 'grades' per arm, else 'grade')."""
    want = gold.get("grades") or {}
    return sum(1 for pair, p in o.items() if p["grade"] == want.get(pair, gold.get("grade")))


def extra_confirmed(o, gold):
    want = gold.get("mechanism")
    return sorted({c for p in o.values() for c in p["confirmed"]} - {want, "as_hypothesized", "novelty"}
                  - set(gold.get("also") or []))


def review_verdict(before, after, review):
    """From a reviewer's verdicts on the case: 'improved' if a claim marked wrong or missing
    changed and none marked correct did; 'regressed' if a correct one changed; else None."""
    if not review:
        return None
    marks = [(c["metric"].lower(), c["verdict"]) for c in review.get("claims", []) if c.get("verdict")]
    claim = lambda o, m: [p["claims"].get(m) for p in o.values()]  # noqa: E731
    changed = {m for m, _ in marks if claim(before, m) != claim(after, m)}
    if any(v == "correct" and m in changed for m, v in marks):
        return "regressed"
    if any(v in ("wrong", "missing") and m in changed for m, v in marks):
        return "improved"
    return None


def _same(a, b):
    keys = ("grade", "call", "candidates", "plan")
    return all(a.get(t, {}).get(k) == b.get(t, {}).get(k) for t in set(a) | set(b) for k in keys)


def evaluate(lesson, cases_root, overlay, parent_knowledge, live=None):
    if not lesson.get("source_flights"):
        return {"lesson_id": lesson["id"], "pass": False, "status": "it names no source flight; say which runs it came from, "
                "so they are held out of its evaluation"}
    if lesson["type"] == "cut_template":
        problem = cutlib.template_problem(lesson["change"]["template"])
        if problem:
            return {"lesson_id": lesson["id"], "pass": False, "status": f"the cut template is refused: {problem}"}
    if lesson["type"] == "pattern" and lesson["change"]["pattern"].get("stub"):
        return {"lesson_id": lesson["id"], "pass": False, "status": "a stub: a person writes its trigger, mechanism and cuts first"}
    if lesson["type"] not in lessons.DETERMINISTIC:
        return {"lesson_id": lesson["id"], "pass": False, "status": "needs a model-facing evaluation (3+ runs per case, "
                "lint counts and grade stability), which this replay doesn't do"}
    work = tempfile.mkdtemp(prefix="lesson-")
    try:
        cand = lessons.candidate_knowledge(parent_knowledge, lesson, os.path.join(work, "candidate"))
        cases = sorted(d for d in os.listdir(cases_root) if os.path.isdir(os.path.join(cases_root, d)))
        rows, regressions, fit = [], [], []
        for case in cases:
            path = os.path.join(cases_root, case)
            meta, _ = _case(path)
            before = replay(path, overlay, parent_knowledge, live)
            after = replay(path, overlay, cand, live)
            row = {"case": case, "held_out": case not in lesson["source_flights"], "changed": not _same(before, after)}
            if meta.get("gold"):
                row["gold_before"], row["gold_after"] = gold_score(before, meta["gold"]), gold_score(after, meta["gold"])
                row["grades_before"], row["grades_after"] = grade_score(before, meta["gold"]), grade_score(after, meta["gold"])
                row["extra_confirmed_after"] = extra_confirmed(after, meta["gold"])
            row["unexplained_before"] = sum(len(p["unexplained"]) for p in before.values())
            row["unexplained_after"] = sum(len(p["unexplained"]) for p in after.values())
            if not row["held_out"]:
                fit.append(row)
                continue
            verdict = review_verdict(before, after, _load(os.path.join(path, "review.json")))
            ups = [row.get(f"{k}_after", 0) - row.get(f"{k}_before", 0) for k in ("gold", "grades")]
            gold_up, gold_down = any(u > 0 for u in ups), any(u < 0 for u in ups)
            row["improved"] = row["changed"] and not gold_down and verdict != "regressed" and (gold_up or verdict == "improved")
            if row["changed"] and not row["improved"]:
                regressions.append(case)
            rows.append(row)
        targets = [r for r in rows if r["case"] in lesson.get("target_cases", [])]
        improved = [r for r in rows if r["improved"]]
        passed = not regressions and bool(improved) and all(r["improved"] for r in targets)
        report = {"lesson_id": lesson["id"], "type": lesson["type"], "parent_version": knowledge.version_hash(parent_knowledge),
                  "candidate_version": knowledge.version_hash(cand), "cases": rows, "fit_cases": fit,
                  "regressions": regressions, "improved": [r["case"] for r in improved], "pass": passed,
                  "status": "passed; waiting for a person's approval" if passed else
                  ("regressions on " + ", ".join(regressions) if regressions else
                   "a named target didn't improve" if improved else "no held-out case improved"),
                  "evaluated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return report


def approve(lesson, report, approver, knowledge_dir, lessons_dir, cases_root=None, overlay=None, live=None):
    """Apply a passed lesson after a person approves it; returns the new knowledge version.
    With the cases, the evaluation runs again here, so an edited report can't pass a lesson."""
    if not approver or not str(approver).strip():
        raise ValueError("approval needs the approving person's name")
    if not report.get("pass") or report.get("lesson_id") != lesson["id"]:
        raise ValueError("only a lesson whose own evaluation passed can be approved")
    if report["parent_version"] != knowledge.version_hash(knowledge_dir):
        raise ValueError("the knowledge changed since this lesson was evaluated; evaluate it again")
    if cases_root:
        again = evaluate(lesson, cases_root, overlay, knowledge_dir, live)
        if not again.get("pass") or again["improved"] != report.get("improved") or again["regressions"] != report.get("regressions"):
            raise ValueError("re-running the evaluation doesn't reproduce this report; evaluate it again")
        report = again
    lessons.apply(lesson, knowledge_dir)
    new_version = knowledge.version_hash(knowledge_dir)
    stamp = {"approver": approver, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "report_sha": hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(),
             "parent_version": report["parent_version"], "new_version": new_version}
    record = {**lesson, "status": "approved", "approval": stamp, "report": report}
    os.makedirs(os.path.join(lessons_dir, "approved"), exist_ok=True)
    with open(os.path.join(lessons_dir, "approved", f"{lesson['id']}.json"), "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=1)
    return new_version


def stamp_readout(run_dir, approver, warns_acknowledged):
    """A person's approval of a decision-grade readout: it names the readout hash and every
    WARN, and only then does the readout say "approved"."""
    with open(os.path.join(run_dir, "trace.json"), encoding="utf-8") as handle:
        trace = json.load(handle)
    final = {}
    for g in trace["grades"]:
        if g.get("after") == "M0":
            final[g["pair"]] = g
    decision_grade = {p: g for p, g in final.items() if g["grade"] == "decision_grade"}
    if not decision_grade:
        raise ValueError("no pair in this run is decision grade; there is nothing to approve")
    acknowledged = " ".join(warns_acknowledged).lower()
    missing = sorted({w.split(":")[0] for g in decision_grade.values() for w in g.get("warns", [])
                      if w.split(":")[0].lower() not in acknowledged})
    if missing:
        raise ValueError(f"the approval must name every warning: {', '.join(missing)}")
    stamp = {"approver": approver, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "readout_sha": trace["readout_sha"], "warns_acknowledged": list(warns_acknowledged),
             "pairs": sorted(decision_grade)}
    with open(os.path.join(run_dir, "approval.json"), "w", encoding="utf-8") as handle:
        json.dump(stamp, handle, indent=1)
    path = os.path.join(run_dir, "readout.md")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    text = text.replace("Decision grade, not approved yet", f"Decision grade, approved by {approver} on {stamp['time'][:10]}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return stamp
