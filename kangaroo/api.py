"""The functions a person reaches through a chat session (MCP tools) or the command line.

Everything lives in one workspace folder (KANGAROO_HOME, default ~/.kangaroo):

    overlay/    warehouse.json, standards.json, contracts/<experiment>.json
    knowledge/  what approved lessons wrote (patterns, metric families, cut templates)
    cards/      experiment cards, drafted by runs and confirmed by people
    cases/      frozen runs, which lessons are evaluated on
    lessons/    candidate/ and approved/
    runs/       one folder per run: readout.md, readout.html, triage prep, trace, review

Every function returns a dict with a short `text` for a person, plus paths and counts.
None of them writes to the warehouse. The only functions that change the knowledge are
`approve_lesson` and `confirm_card`, and both need a person's name.
"""

import contextlib
import glob
import json
import os
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
from kangaroo.sources.warehouse import SqliteWarehouse  # noqa: E402

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
    start = text.find("## In one minute")
    end = text.find("# Details")
    return text[start:end].strip() if start >= 0 else text.split("\n## ", 2)[0]


def analyze(experiment=None, scorecard=None, contract=None, config=None, warehouse=None, configs=None, path=None):
    """Run the agent on one experiment: from a scorecard file, or from a warehouse."""
    h = home(path)
    init(h)
    d = _dirs(h)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    overrides = None
    if scorecard:
        wh_cfg = _load(os.path.join(d["overlay"], "warehouse.json"))
        names = sorted({r[t1src.columns(wh_cfg)["experiment"]] for r in runner.scorecard_rows(scorecard)})
        experiment = experiment or (names[0] if len(names) == 1 else None)
        if not experiment:
            raise ValueError(f"the scorecard holds {len(names)} experiments ({', '.join(names[:5])}); say which one")
        out = os.path.join(d["runs"], f"{experiment}-{stamp}")
        wh, _ = runner.scorecard_warehouse(scorecard, out, wh_cfg["tables"]["t1"])
        overrides = {"approved": ["t1"]}
    elif warehouse:
        wh = SqliteWarehouse(warehouse.partition(":")[2] if warehouse.startswith("sqlite:") else warehouse)
        if not experiment:
            raise ValueError("say which experiment to analyze")
        out = os.path.join(d["runs"], f"{experiment}-{stamp}")
    else:
        raise ValueError("give a scorecard file, or a warehouse (sqlite:PATH at home)")
    cfg_source = cfgsrc.OneConfig(_load(config)) if config else cfgsrc.JsonDirConfig(configs)
    result = _quiet(runner.run, experiment, d["overlay"], wh, cfg_source, out, d["knowledge"], d["cards"],
                    _load(contract) if contract else None, overrides)
    _quiet(evaluate.freeze, out, d["cases"])
    c = result["counts"]
    return {"run_dir": out, "readout": os.path.join(out, "readout.md"), "html": os.path.join(out, "readout.html"),
            "counts": c, "text": minute(result["readout"]) + f"\n\nFull report: {os.path.join(out, 'readout.html')}"}


def report(run="latest", part="minute", path=None):
    """The report, or one part: minute, details, triage_prep, all."""
    rd = _run_dir(home(path), run)
    with open(os.path.join(rd, "readout.md"), encoding="utf-8") as handle:
        text = handle.read()
    if part == "minute":
        out = minute(text)
    elif part == "details":
        out = text[text.find("# Details"):] if "# Details" in text else text
    elif part == "triage_prep":
        parts = []
        for prep in sorted(glob.glob(os.path.join(rd, "triage_prep_*.md"))):
            with open(prep, encoding="utf-8") as handle:
                parts.append(handle.read())
        out = "\n\n".join(parts)
    else:
        out = text
    return {"run_dir": rd, "text": out}


def why(metric, run="latest", path=None):
    """What the run says about one metric: its change, the best explanation, its support, the cuts that ran."""
    rd = _run_dir(home(path), run)
    trace = _load(os.path.join(rd, "trace.json"))
    key = metric.lower()
    claims = ([c for c in trace["claim_records"] if c["metric"].lower() == key]
              or [c for c in trace["claim_records"] if key in c["metric"].lower()])
    if not claims:
        moved = sorted({c["metric"] for c in trace["claim_records"]})
        return {"run_dir": rd, "text": f"The run has no question about '{metric}'. It asked about: {', '.join(moved)}."}
    lines = []
    for c in claims:
        lines.append(f"{c['metric']} ({c['pair']}): {c['change']}.")
        if c["candidate"]:
            support = {"confirmed": "confirmed by data", "consistent": "fits the data, not yet confirmed",
                       "inferred": "likely, not yet checked", "exploratory": "exploratory only"}.get(c["support_level"],
                                                                                                    c["support_level"])
            lines.append(f"Best explanation ({support}): {c['candidate']}. {c['mechanism']}")
            ran = [a for a in trace["action_records"] if a.get("pair") == c["pair"] and a.get("target_pattern") == c["candidate_id"]
                   and a["status"] in ("ran", "access gap")]
            for a in ran:
                role = "Control check" if a.get("role") == "control" else "Check"
                lines.append(f"- {role}: {a['exact_next_task']}: " + (f"{a.get('outcome')} ({a.get('detail')})" if a["status"] == "ran"
                                                                      else f"not run ({a['source_or_access_gap']})"))
            if c.get("awaiting"):
                lines.append(f"Not confirmed until: {'; '.join(c['awaiting'][:2])}.")
        elif c.get("status") == "possibly chance":
            lines.append("It isn't a registered metric and nothing explains it; with many metrics some move by chance.")
        elif c["change"].startswith("didn't move"):
            lines.append("No explanation in the library for why it didn't move as expected.")
        else:
            lines.append("No explanation in the library yet. The move is clear, so it needs a cause; 'unexplained' is the "
                         "honest answer until a check finds one.")
        if c["statement_ids"]:
            lines.append(f"Data behind it: {', '.join(c['statement_ids'])}.")
        lines.append(f"To mark this right or wrong: review claim {c['claim_id']} of this run.")
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
    live = SqliteWarehouse(warehouse.partition(":")[2]) if warehouse and warehouse.startswith("sqlite:") else None
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
