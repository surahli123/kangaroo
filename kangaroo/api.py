"""The functions a person reaches through a chat session (MCP tools) or the command line.

Everything lives in one workspace folder (KANGAROO_HOME, default ~/.kangaroo):

    overlay/    warehouse.json, standards.json, contracts/<experiment>.json
    knowledge/  what approved lessons wrote (patterns, metric families, cut templates)
    cards/      experiment cards, drafted by runs and confirmed by people
    cases/      frozen runs, which lessons are evaluated on
    lessons/    candidate/ and approved/
    runs/       one folder per run: readout.md, readout.html, trace, review

Every function returns a dict with a short `text` for a person, plus paths and counts.
None of them writes to the warehouse. The only functions that change the knowledge are
`approve_lesson` and `confirm_card`, and both need a person's name.
"""

import contextlib
import glob
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

from learning import cards as cardlib  # noqa: E402
from learning import evaluate, knowledge, lessons  # noqa: E402
from kangaroo import LIBRARY  # noqa: E402
from kangaroo import run as runner  # noqa: E402
from kangaroo.sources import config as cfgsrc  # noqa: E402
from kangaroo.sources import t1 as t1src  # noqa: E402
from kangaroo.sources.warehouse import HandoffWarehouse, Pending, SplitWarehouse, bind  # noqa: E402

EXAMPLE_OVERLAY = os.path.join(ROOT, "overlay.example")


def home(path=None):
    return os.path.abspath(os.path.expanduser(path or os.environ.get("KANGAROO_HOME") or "~/.kangaroo"))


def _dirs(h):
    return {k: os.path.join(h, k) for k in ("overlay", "knowledge", "cards", "cases", "lessons", "runs")}


def _load(path, default=None):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return default


def _save(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1)


def init(path=None, overlay=None):
    """Create the workspace, starting the overlay from overlay.example (or a given overlay)."""
    h = home(path)
    d = _dirs(h)
    if not os.path.exists(d["overlay"]):
        shutil.copytree(overlay or EXAMPLE_OVERLAY, d["overlay"])
    for key in ("knowledge", "cards", "cases", "runs"):
        os.makedirs(d[key], exist_ok=True)
    os.makedirs(os.path.join(d["lessons"], "candidate"), exist_ok=True)
    return {"home": h, "text": f"Workspace ready at {h}. Put contracts in {d['overlay']}/contracts and the warehouse "
                               f"settings in {d['overlay']}/warehouse.json."}


def _quiet(fn, *args, **kwargs):
    """Run with stdout sent to stderr, so an MCP session's stdout stays clean."""
    with contextlib.redirect_stdout(sys.stderr):
        return fn(*args, **kwargs)


def _run_dir(h, run):
    """A run folder from a path, 'latest', or an experiment name (its latest run)."""
    if run and os.path.isdir(run):
        return run
    runs = sorted(glob.glob(os.path.join(_dirs(h)["runs"], "*", "trace.json")), key=os.path.getmtime)
    if run and run != "latest":
        runs = [r for r in runs if _load(r, {}).get("flight") == run or os.path.basename(os.path.dirname(r)).startswith(run)]
    if not runs:
        raise FileNotFoundError(f"no run found for {run or 'latest'}")
    return os.path.dirname(runs[-1])


def minute(text):
    """The title and the Summary section of a report."""
    from kangaroo.render.readout import summary_text
    return summary_text(text)


def _new_run_dir(runs, experiment, stamp):
    """A run folder that no earlier run uses, also when two runs start in the same second."""
    out, n = os.path.join(runs, f"{experiment}-{stamp}"), 2
    while os.path.exists(out):
        out, n = os.path.join(runs, f"{experiment}-{stamp}-{n}"), n + 1
    return out


def analyze(experiment=None, scorecard=None, contract=None, config=None, warehouse=None, configs=None, path=None):
    """Run the agent on one experiment. The scorecard comes from a file (an export of T1) or
    from the warehouse; the deep-dive cuts need a warehouse. Both together: T1 from the file,
    the cuts from the warehouse."""
    h = home(path)
    init(h)
    d = _dirs(h)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    wh_cfg = _load(os.path.join(d["overlay"], "warehouse.json"))
    overrides, cut_wh = None, None
    if warehouse:
        spec = f"sqlite:{warehouse}" if ":" not in warehouse and os.path.exists(warehouse) else warehouse
        cut_wh = runner.warehouse_from(spec, wh_cfg.get("dialect", "spark"))  # the one warehouse seam
    if scorecard:
        rows = runner.scorecard_rows(scorecard)
        runner.check_scorecard_columns(rows, t1src.columns(wh_cfg))
        names = sorted({r[t1src.columns(wh_cfg)["experiment"]] for r in rows})
        experiment = experiment or (names[0] if len(names) == 1 else None)
        if not experiment:
            raise ValueError(f"the scorecard holds {len(names)} experiments ({', '.join(names[:5])}); say which one")
        out = _new_run_dir(d["runs"], experiment, stamp)
        local = "scorecard_t1"  # the file's rows, under a local name (the overlay's T1 name can be catalog.schema.view)
        wh, _ = runner.scorecard_warehouse(scorecard, out, local, t1src.columns(wh_cfg))
        overrides = {"tables": {**wh_cfg["tables"], "t1": local}}
        if cut_wh:
            wh = SplitWarehouse(wh, cut_wh, local)
        else:
            overrides["approved"] = ["t1"]
    elif cut_wh:
        if not experiment:
            raise ValueError("say which experiment to analyze")
        out, wh = _new_run_dir(d["runs"], experiment, stamp), cut_wh
    else:
        raise ValueError("give a scorecard file, or a warehouse (sqlite:PATH at home)")
    cfg_source = (cfgsrc.OneConfig(_load(config)) if config else cfgsrc.JsonDirConfig(configs) if configs
                  else cfgsrc.OneConfig(None))
    waiting = ""
    try:
        result = _quiet(runner.run, experiment, d["overlay"], wh, cfg_source, out, d["knowledge"], d["cards"],
                        _load(contract) if contract else None, overrides)
    except Pending:  # the scorecard itself waits for the warehouse
        return {"run_dir": out, "counts": None, "pending": cut_wh.pending(), "text": _handoff_text(cut_wh)}
    if isinstance(cut_wh, HandoffWarehouse) and cut_wh.pending():
        waiting = "\n\n" + _handoff_text(cut_wh)
    _quiet(evaluate.freeze, out, d["cases"])
    c = result["counts"]
    return {"run_dir": out, "readout": os.path.join(out, "readout.md"), "html": os.path.join(out, "readout.html"),
            "counts": c, "pending": cut_wh.pending() if isinstance(cut_wh, HandoffWarehouse) else [],
            "text": minute(result["readout"]) + f"\n\nFull report: {os.path.join(out, 'readout.html')}" + waiting}


def _handoff_text(wh):
    n = len(wh.pending())
    return (f"{n} statement{'s' if n != 1 else ''} wait for the warehouse in {wh.folder}/pending. Run each one with the "
            f"warehouse tool. Save the tool output, without changes, as {wh.folder}/results/<same name>.json. "
            "Then run analyze again.")


def dry_run(out, path=None):
    """Write each cut template's SQL with LIMIT 0, built from the overlay, for a compile check on the
    work warehouse. Run each file with the warehouse tool; a template that fails stays unused."""
    from kangaroo.m1 import cuts as cutlib
    h = home(path)
    d = _dirs(h)
    init(h)
    wh_cfg = _load(os.path.join(d["overlay"], "warehouse.json"))
    os.makedirs(out, exist_ok=True)
    written = []
    for tid, tpl in sorted(cutlib.load_templates(d["knowledge"]).items()):
        target = os.path.join(out, f"{tid}.sql")
        with open(target, "w", encoding="utf-8") as handle:
            sql = cutlib.dry_run_sql(tpl, wh_cfg, wh_cfg.get("dialect", "spark"))
            finished = bind(sql, {"experiment": "__dry_run__", "as_of": "1970-01-01"}, "spark")[0]  # LIMIT 0: no rows
            handle.write(finished + "\n")
        written.append(target)
    return {"files": written, "text": f"{len(written)} dry-run statements in {out}. Run each one with the warehouse tool. "
                                      "Each must compile and return no rows."}


def check_report(run="latest", path=None):
    """The writing-rule check (render/ste.py) on a run's report. Team names are not checked."""
    from kangaroo.render import ste
    rd = _run_dir(home(path), run)
    with open(os.path.join(rd, "readout.md"), encoding="utf-8") as handle:
        text = handle.read()
    trace = _load(os.path.join(rd, "trace.json"))
    scorecard = _load(os.path.join(rd, "case", "scorecard.json"), {})
    names = {m["metric"] for c in scorecard.get("comparisons", []) for m in c["metrics"]}
    names |= {c.get("test_group") for c in scorecard.get("comparisons", [])} | {scorecard.get("control_group") or ""}
    names |= {a.get("produces") or "" for a in trace["action_records"]} | {a["exact_next_task"] for a in trace["action_records"]}
    problems = ste.check(text, {n for n in names if n})
    lines = [f"{len(problems)} problems with the writing rules in {rd}/readout.md."] + [f"- {r}: {t}" for r, t in problems[:20]]
    return {"problems": problems, "text": "\n".join(lines)}


def contract_template(scorecard, out, path=None):
    """Write a contract skeleton that lists the scorecard's metrics, for the owner to fill in."""
    d = _dirs(home(path))
    init(home(path))
    cols = t1src.columns(_load(os.path.join(d["overlay"], "warehouse.json")))
    rows = runner.scorecard_rows(scorecard)
    runner.check_scorecard_columns(rows, cols)
    _save(out, runner.contract_template(rows, cols))
    return {"path": out, "text": f"Contract template written to {out}. Fill in the primary metrics, their bars and the "
                                 "guardrails. Then run analyze again with --contract."}


def report(run="latest", part="summary", path=None):
    """The report, or one part: summary (what analyze returns), details (the tables), all."""
    rd = _run_dir(home(path), run)
    with open(os.path.join(rd, "readout.md"), encoding="utf-8") as handle:
        text = handle.read()
    if part in ("summary", "minute"):
        out = minute(text)
    elif part == "details":
        out = text[text.find("## Metrics"):] if "## Metrics" in text else text
    else:
        out = text
    return {"run_dir": rd, "text": out}


def _ste(text):
    """A tool's change text in the report's words: 'fell 4.70% (-6.36% to -3.03%)' -> 'decreased 4.70% (95% CI ...)'."""
    text = re.sub(r"didn't move clearly \(([^()]*) \(([^()]*)\)\)", r"did not change clearly: \1 (95% CI \2)", text)
    text = re.sub(r"\b(fell|rose) ([\d.]+%) \(([^()]*)\)",
                  lambda m: f"{'decreased' if m.group(1) == 'fell' else 'increased'} {m.group(2)} (95% CI {m.group(3)})", text)
    return text.replace("didn't", "did not").replace("doesn't", "does not")


def why(metric, run="latest", path=None):
    """What the run says about one metric: its change, the best explanation, its support, the cuts that ran."""
    rd = _run_dir(home(path), run)
    trace = _load(os.path.join(rd, "trace.json"))
    key = metric.lower()
    claims = ([c for c in trace["claim_records"] if c["metric"].lower() == key]
              or [c for c in trace["claim_records"] if key in c["metric"].lower()])
    if not claims:
        moved = sorted({c["metric"] for c in trace["claim_records"]})
        return {"run_dir": rd, "text": f"The run has no result for '{metric}'. It has results for: {', '.join(moved)}."}
    support = {"confirmed": "Confirmed", "consistent": "Consistent", "inferred": "Likely", "exploratory": "Exploratory"}
    outcome = {"confirms": "This agrees with the cause.", "refutes": "This rules out the cause."}
    blocked = {g["pair"] for g in trace.get("grades", []) if g.get("grade") == "not_permitted"}
    lines = []
    for c in claims:
        if c["pair"] in blocked:
            lines.append(f"{c['metric']} ({c['pair']}): not readable. Fix the data problem of this arm first.")
            continue
        lines.append(f"{c['metric']} ({c['pair']}): {_ste(c['change'])}.")
        if c["candidate"]:
            lines.append(f"Best explanation: {c['candidate']} ({support.get(c['support_level'], 'Possible')}). {c['mechanism']}")
            ran = [a for a in trace["action_records"] if a.get("pair") == c["pair"] and a.get("target_pattern") == c["candidate_id"]
                   and a["status"] in ("ran", "access gap")]
            for a in ran:
                role = "Control cut" if a.get("role") == "control" else "Cut"
                lines.append(f"- {role}: {a['exact_next_task']}: " + (
                    f"{_ste(a.get('detail') or '')}. {outcome.get(a.get('outcome'), 'This does not confirm the cause and does not rule it out.')}"
                    if a["status"] == "ran" else f"not run ({a['source_or_access_gap']})."))
            if c.get("awaiting"):
                lines.append(f"Not confirmed until this agrees: {'; '.join(c['awaiting'][:2])}.")
        elif c.get("status") == "possibly chance":
            lines.append("It is not a registered metric, and no cause explains it. Some metrics change by chance.")
        elif c["change"].startswith("didn't move"):
            lines.append("No cause in the library explains why it did not change as the contract expected.")
        else:
            lines.append("No cause in the library explains this clear change. Say that it is not explained. Do not guess.")
        if c["statement_ids"]:
            lines.append(f"Data: {', '.join(c['statement_ids'])}.")
        lines.append(f"To mark this claim as right or wrong, review claim {c['claim_id']} of this run.")
    return {"run_dir": rd, "text": "\n".join(lines)}


def review(verdict, claim=None, metric=None, note="", reviewer=None, run="latest", path=None):
    """Record a person's verdict on a claim (correct, wrong, missing) in the run's review form."""
    if verdict not in ("correct", "wrong", "missing"):
        raise ValueError("verdict must be correct, wrong or missing")
    rd = _run_dir(home(path), run)
    form_path = os.path.join(rd, "review.json")
    form = _load(form_path)
    hits = ([c for c in form["claims"] if claim and c["claim_id"] == claim]
            or [c for c in form["claims"] if metric and c["metric"].lower() == metric.lower()]
            or [c for c in form["claims"] if metric and metric.lower() in c["metric"].lower()])
    if verdict == "missing" and not hits:
        form.setdefault("missing", []).append(note)
    elif not hits:
        raise ValueError(f"no claim {claim or metric} in this run")
    for c in hits[:1]:
        c["verdict"], c["note"] = verdict, note
    form["reviewer"] = reviewer or form.get("reviewer") or "reviewer"
    form["date"] = time.strftime("%Y-%m-%d", time.gmtime())
    _save(form_path, form)
    case = os.path.join(_dirs(home(path))["cases"], _load(os.path.join(rd, "trace.json"))["flight"])
    if os.path.isdir(case) and _load(os.path.join(case, "meta.json"), {}).get("run_id") == form.get("run_id"):
        _save(os.path.join(case, "review.json"), form)  # the evaluation reads verdicts from the frozen case
    done = sum(1 for c in form["claims"] if c.get("verdict"))
    return {"run_dir": rd, "text": f"Recorded: {verdict}" + (f" on {hits[0]['metric']}" if hits else "") +
            f". {done} of {len(form['claims'])} claims in this run are reviewed. Lessons come from reviews: ask to propose them."}


def propose(path=None):
    """Candidate lessons from reviews, unmapped metric names and movements unexplained across flights."""
    import explain
    h = home(path)
    d = _dirs(h)
    library = explain.load_library(d["knowledge"], base=LIBRARY)
    found = []
    for case in sorted(glob.glob(os.path.join(d["cases"], "*", "scorecard.json"))):
        flight = os.path.basename(os.path.dirname(case))
        found += lessons.propose_mappings(_load(case), library, flight)
        contract = _load(os.path.join(os.path.dirname(case), "contract.json"))
        if contract:
            found += lessons.propose_aliases(_load(case), contract, flight, _load(os.path.join(d["knowledge"], "metric_aliases.json"), {}))
    traces = [_load(t) for t in glob.glob(os.path.join(d["runs"], "*", "trace.json"))]
    found += lessons.propose_patterns(traces)
    for form in glob.glob(os.path.join(d["runs"], "*", "review.json")):
        r = _load(form)
        if r.get("reviewer"):
            found += lessons.from_review(r, _load(os.path.join(os.path.dirname(form), "trace.json"))["flight"])
    approved = {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(d["lessons"], "approved", "*.json"))}
    new = {}
    for lesson in found:
        if lesson["id"] in approved:
            continue
        if lesson["id"] in new:
            new[lesson["id"]]["source_flights"] = sorted(set(new[lesson["id"]]["source_flights"]) | set(lesson["source_flights"]))
        else:
            new[lesson["id"]] = lesson
    for lesson in new.values():
        _save(os.path.join(d["lessons"], "candidate", f"{lesson['id']}.json"), lesson)
    return {"lessons": list(new), "text": list_lessons(h)["text"]}


def list_lessons(path=None):
    d = _dirs(home(path))
    lines = []
    for p in sorted(glob.glob(os.path.join(d["lessons"], "candidate", "*.json"))):
        if p.endswith(".report.json"):
            continue
        lesson = _load(p)
        rep = _load(p.replace(".json", ".report.json"))
        state = f"evaluated: {rep['status']}" if rep else "not evaluated"
        lines.append(f"- {lesson['id']} ({lesson['type']}): {lesson['rationale']}; from {', '.join(lesson['source_flights'])}; {state}")
    approved = len(glob.glob(os.path.join(d["lessons"], "approved", "*.json")))
    return {"text": ("Candidate lessons:\n" + "\n".join(lines) if lines else "No candidate lessons.")
            + f"\n{approved} approved so far. Knowledge version {knowledge.version_hash(d['knowledge'])[:12]}."}


def evaluate_lesson(lesson_id, warehouse=None, path=None):
    """Replay the frozen cases with and without the lesson, its source flights held out."""
    h = home(path)
    d = _dirs(h)
    p = os.path.join(d["lessons"], "candidate", f"{lesson_id}.json")
    lesson = _load(p)
    if not lesson:
        raise FileNotFoundError(f"no candidate lesson {lesson_id}")
    live = runner.warehouse_from(warehouse) if warehouse else None
    rep = _quiet(evaluate.evaluate, lesson, d["cases"], d["overlay"], d["knowledge"], live)
    _save(p.replace(".json", ".report.json"), rep)
    lines = [f"{lesson_id}: {rep['status']}."]
    if rep.get("cases") is not None:
        lines.append(f"{len(rep['cases'])} held-out cases, improved: {', '.join(rep['improved']) or 'none'}, "
                     f"regressions: {', '.join(rep['regressions']) or 'none'}; source flights held out: "
                     f"{', '.join(c['case'] for c in rep.get('fit_cases', [])) or 'none'}.")
    if rep.get("pass"):
        lines.append("It changes the knowledge only if a person approves it, by name.")
    return {"report": rep, "text": "\n".join(lines)}


def approve_lesson(lesson_id, approver, path=None):
    """A person approves a passed lesson. The caller must have the person's explicit approval."""
    if not approver or not str(approver).strip():
        raise ValueError("approval needs the approving person's name")
    d = _dirs(home(path))
    p = os.path.join(d["lessons"], "candidate", f"{lesson_id}.json")
    version = _quiet(evaluate.approve, _load(p), _load(p.replace(".json", ".report.json"), {}), approver, d["knowledge"],
                     d["lessons"], d["cases"], d["overlay"])
    os.remove(p)
    return {"version": version, "text": f"Approved by {approver}. Knowledge version is now {version[:12]}; the next runs use it."}


def approve_report(approver, warns=(), run="latest", path=None):
    """A person approves a decision-grade report, naming every warning it carries."""
    if not approver or not str(approver).strip():
        raise ValueError("approval needs the approving person's name")
    rd = _run_dir(home(path), run)
    stamp = evaluate.stamp_readout(rd, approver, list(warns))
    return {"run_dir": rd, "text": f"Report approved by {approver} for {', '.join(stamp['pairs'])}."}


def list_cards(path=None):
    cs = cardlib.load(_dirs(home(path))["cards"])
    lines = [f"- {c['experiment_name']} ({c['status']}): {c['name']}" + (f". Lesson: {c['lesson']}" if c.get("lesson") else "")
             for c in cs]
    return {"text": "\n".join(lines) or "No experiment cards yet."}


def confirm_card(experiment, cause, lesson, approver, path=None):
    """A person confirms what a past experiment showed; only confirmed cards are cited."""
    if not approver or not str(approver).strip():
        raise ValueError("confirming a card needs the person's name")
    card = cardlib.confirm(_dirs(home(path))["cards"], experiment, cause, lesson, approver)
    return {"text": f"Card for {card['experiment_name']} confirmed by {approver}; similar experiments will cite it."}


def list_runs(path=None):
    rows = []
    for t in sorted(glob.glob(os.path.join(_dirs(home(path))["runs"], "*", "trace.json")), key=os.path.getmtime)[-10:]:
        tr = _load(t)
        rows.append(f"- {os.path.basename(os.path.dirname(t))}: {tr['flight']}, grades {tr['counts']['grades']}")
    return {"text": "\n".join(rows) or "No runs yet."}
