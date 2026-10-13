#!/usr/bin/env python3
"""Render the launch-review readout from decide.py's decision and the model's notes.

    python3 render.py --decision decision.json --scorecard scorecard.json --notes notes.json [--out-dir DIR]

notes.json is the only part the model writes:

    {"why_it_moved": [{"text": "...", "label": "measured|inferred|guess"}],   # at most 3
     "would_change": ["...", "..."],                                          # at most 3
     "analyst_view": null or {"call": "<one of the calls>", "reason": "..."}}

The notes are checked before anything is written: bullet and word caps, no
stat-speak, no call words in "Why it moved", and every number must be one the
scorecard or the decision contains. On any problem it prints them and exits 1.
Writes readout.md, all_metrics.md and spine.json (the cold-read answer key,
built from the decision, never from the readout). Exit 0 on success, 2 on bad input.
"""

import argparse
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from decide import CALL_TEXT, _key, describe, moved, threshold_text  # noqa: E402

STAT_SPEAK = re.compile(r"p[- ]?values?|\bp\s*(?:[<=>]|of\b|was\b|is\b)|statistical|significan|\balpha\b|"
                        r"z[- ]?score|t[- ]?stat|\bpower(ed)?\b|variance|cuped|bonferroni|sequential|\bsrm\b|"
                        r"null hypothesis|confidence|\bci\b|\bintervals?\b|standard errors?|margin of error|\bnoise\b|"
                        r"\bby chance\b|\brobust\b|\bsigma\b|percent level|\bdegrees of freedom\b", re.I)
CALL_WORDS = re.compile(r"\bship(ping|ped)?\b|don'?t ship|escalat|keep running|no effect found|can'?t tell|"
                        r"\blaunch(ing|ed)?\b|roll(ing)?[ -]?out|go/no[- ]go", re.I)
NUMBER_WORDS = re.compile(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|a half|half|a third|a quarter|"
                          r"twice|double|triple)\b\s*(percent|percentage|pp|%|times|x\b)?", re.I)
NUMBER = re.compile(r"(?<![\w.])([-+−]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(%|pp\b|x\b)?")
DOWN = re.compile(r"\b(fell|fall|falls|dropped|drops|decreased|declined|down|lower|shrank|lost)\b", re.I)
UP = re.compile(r"\b(rose|rise|rises|increased|grew|up|higher|gained|climbed)\b", re.I)
LABELS = ("measured", "inferred", "guess")
MAX_BULLETS, MAX_WORDS, MAX_VIEW_WORDS = 3, 35, 45
GLOSSARY = ("Calls: Can't tell means the test is broken. Keep running means not enough data yet. "
            "No effect found means enough data and nothing as big as the bar. Escalate means a trade-off for triage.")


def _metric_numbers(m):
    percents, plain = set(), set()
    for key in ("rel_delta",):
        if m.get(key) is not None:
            percents.add(100 * m[key])
    for ci in (m.get("rel_ci"), m.get("seq_rel_ci")):
        for b in ci or []:
            percents.add(100 * b)
    for key in ("control_mean", "test_mean", "abs_delta", "control_units", "test_units"):
        if m.get(key) is not None:
            plain.add(m[key])
            if key in ("control_mean", "test_mean") and 0 < abs(m[key]) <= 1:
                percents.add(100 * m[key])
    if m.get("abs_delta") is not None and m.get("control_mean") is not None and abs(m["control_mean"]) <= 1:
        percents.add(100 * m["abs_delta"])  # a rate's change in percentage points
    for b in m.get("abs_ci") or []:
        plain.add(b)
    return percents, plain


def allowed_numbers(decision, scorecard, text=""):
    """Numbers a bullet may use: those of the metrics it names, the decider and the
    primaries, plus the decision's own (thresholds, status, daily series, shares)."""
    percents, plain = set(), set()
    words = set(re.findall(r"[a-z0-9]+", text.lower()))

    def named_in_text(name):
        first = re.findall(r"[a-z0-9]+", name.lower())[:1]
        return (_key(name) and _key(name) in _key(text)) or (first and len(first[0]) >= 5 and first[0] in words)

    named = [m for c in scorecard.get("comparisons", []) if c.get("test_group") == decision["experiment"].get("test_group")
             for m in c["metrics"] if named_in_text(m["metric"])]
    anchors = {_key(decision["decider"].get("metric", ""))} | {_key(v["metric"]) for v in decision["verdicts"] if v["role"] == "primary"}
    for c in scorecard.get("comparisons", []):
        for m in c["metrics"]:
            if m in named or _key(m["metric"]) in anchors:
                p, q = _metric_numbers(m)
                percents |= p
                plain |= q
    for v in decision["verdicts"]:
        for key in ("point", "low", "high", "threshold"):
            if v.get(key) is not None:
                if v.get("unit") == "relative":
                    percents.add(100 * v[key])
                else:
                    plain.add(v[key])
        for key in ("point", "low", "high"):
            if (v.get("triggered") or {}).get(key) is not None:
                percents.add(100 * v["triggered"][key])
        if v.get("scaled_from_triggered"):
            percents.add(100 * v["scaled_from_triggered"])
    for s in decision["secondaries"]["survivors"]:
        if s.get("rel_delta") is not None:
            percents.add(100 * s["rel_delta"])
    for value in decision.get("daily") or []:
        percents.add(100 * value)
    for key, value in decision["status"].items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            plain.add(value)
    plain.add(100 * decision["settings"]["ci_level"])
    return percents, plain


def number_ok(text_value, unit, percents, plain):
    raw = text_value.replace(",", "").replace("−", "-")
    value = abs(float(raw))
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    tolerance = 0.5 * 10 ** -decimals + 1e-9
    if unit in ("%", "pp"):
        return any(abs(value - abs(p)) <= tolerance for p in percents)
    if unit == "x":
        return False
    if value <= 10 and float(value).is_integer() or decimals == 0 and 1990 <= value <= 2100:
        return True
    return any(abs(value - abs(p)) <= max(tolerance, 0.005 * abs(p)) for p in plain | percents)


def _normal(text):
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"[\u2010-\u2015\u2212]", "-", text)


def _sign_problems(where, text, decision, scorecard):
    """A direction word next to a metric name that contradicts the scorecard's sign."""
    problems = []
    lowered = text.lower()
    for c in scorecard.get("comparisons", []):
        if c.get("test_group") != decision["experiment"].get("test_group"):
            continue
        for m in sorted(c["metrics"], key=lambda m: -len(m["metric"])):
            name = m["metric"].lower()
            at = lowered.find(name)
            point = m.get("rel_delta") if m.get("rel_delta") is not None else m.get("abs_delta")
            ci = m.get("rel_ci") or m.get("abs_ci")
            if at < 0 or point is None or not ci or ci[0] <= 0 <= ci[1]:
                continue
            window = " ".join(lowered[at + len(name):].split()[:6])
            if point > 0 and DOWN.search(window) or point < 0 and UP.search(window):
                problems.append(f"{where}: says {m['metric']} moved the other way from the scorecard "
                                f"({100 * m['rel_delta']:+.2f}%)" if m.get("rel_delta") is not None else
                                f"{where}: says {m['metric']} moved the other way from the scorecard")
            lowered = lowered.replace(name, " " * len(name))
    return problems


def check_notes(notes, decision, scorecard):
    if not isinstance(notes, dict):
        return ["notes.json must be an object with why_it_moved, would_change and analyst_view"]
    problems = []
    why = notes.get("why_it_moved") or []
    change = notes.get("would_change") or []
    view = notes.get("analyst_view")
    if not isinstance(why, list) or not isinstance(change, list):
        return ["why_it_moved and would_change must be lists"]
    if len(why) > MAX_BULLETS:
        problems.append(f"why_it_moved has {len(why)} bullets; at most {MAX_BULLETS}")
    if len(change) > MAX_BULLETS:
        problems.append(f"would_change has {len(change)} bullets; at most {MAX_BULLETS}")
    texts = []
    for i, item in enumerate(why, 1):
        if not isinstance(item, dict) or item.get("label") not in LABELS:
            problems.append(f"why_it_moved #{i} needs a label: measured, inferred or guess")
            continue
        texts.append((f"why_it_moved #{i}", item.get("text", ""), MAX_WORDS, True))
    texts += [(f"would_change #{i}", t, MAX_WORDS, True) for i, t in enumerate(change, 1)]
    if view:
        if not isinstance(view, dict) or view.get("call") not in CALL_TEXT or view.get("call") == decision["call"]:
            problems.append(f"analyst_view.call must be one of {sorted(CALL_TEXT)} and differ from the scripted call")
        else:
            texts.append(("analyst_view", view.get("reason", ""), MAX_VIEW_WORDS, False))
    for where, text, cap, no_calls in texts:
        if not isinstance(text, str) or not text.strip():
            problems.append(f"{where} is empty")
            continue
        text = _normal(text)
        if "\n" in text or text.lstrip().startswith(("#", "|", ">")):
            problems.append(f"{where} must be one plain sentence or two, with no line breaks, headings or tables")
        words = len(text.split())
        if words > cap:
            problems.append(f"{where} has {words} words; at most {cap}")
        speak = STAT_SPEAK.search(text)
        if speak:
            problems.append(f"{where} uses stat-speak ('{speak.group(0)}'); say what it means for the decision instead")
        if no_calls and CALL_WORDS.search(text):
            problems.append(f"{where} uses a call word ('{CALL_WORDS.search(text).group(0)}'); the call is made once, "
                            "in the title, and the analyst's view is the place to disagree")
        word_number = NUMBER_WORDS.search(text)
        if word_number and (word_number.group(2) or word_number.group(1).lower() in ("twice", "double", "triple", "half", "a half", "a third", "a quarter")):
            problems.append(f"{where}: write '{word_number.group(0).strip()}' as the scorecard's number so it can be checked")
        percents, plain = allowed_numbers(decision, scorecard, text)
        for verb, number in re.findall(r"\b(\w+)\s+(?:by\s+)?(?:about\s+|nearly\s+|roughly\s+)?([-+]?\d+(?:\.\d+)?)\s*%", text):
            value = float(number)
            decimals = len(number.split(".")[1]) if "." in number else 0
            matches = [p for p in percents if abs(abs(value) - abs(p)) <= 0.5 * 10 ** -decimals + 1e-9]
            if matches and (DOWN.fullmatch(verb) and all(p > 0 for p in matches)
                            or UP.fullmatch(verb) and all(p < 0 for p in matches)):
                problems.append(f"{where}: '{verb} {number}%' has the opposite sign from the scorecard's change")
        for match in NUMBER.finditer(re.sub(r"\b[A-Za-z]+\d+\w*\b|\d{4}-\d{2}-\d{2}", " ", text)):
            if not number_ok(match.group(1), match.group(2), percents, plain):
                problems.append(f"{where}: {match.group(0).strip()} is not a number for the metrics this bullet names, "
                                "the decider or the primary")
        problems.extend(_sign_problems(where, text, decision, scorecard))
    return problems


def bar_text(v, ship_requires="point_at_bar"):
    if v.get("threshold") is None:
        return "no worse than zero" if v["role"] == "guardrail" else "none registered"
    shown = threshold_text(v["threshold"], v.get("unit", "relative"), v.get("display_unit", ""))
    down_is_good = v.get("direction") in ("decrease", "lower", "down", "lower_is_better")
    if v["role"] == "primary":
        way = "lower" if down_is_good else "higher"
        if ship_requires == "ci_at_bar":
            return f"whole interval at least {shown} {way}"
        if ship_requires == "ci_excludes_zero":
            return f"interval clear of zero; {shown} {way} to rule out"
        return f"estimate at least {shown} {way}"
    if v["role"] == "guardrail":
        return f"at most {shown} higher" if down_is_good else f"at most {shown} lower"
    return "no change"


VERDICT_TEXT = {"better": "better", "below_bar": "better, below the bar", "worse": "worse", "undetermined": "unclear",
                "within": "within margin", "harm_within_margin": "worse, within margin", "breached": "past its margin",
                "no_harm_detected": "no harm found", "missing": "not in the scorecard", "no_interval": "no interval",
                "no_direction": "no direction registered", "moved": "moved", "held": "held"}


def row(v, ship_requires="point_at_bar"):
    change = describe(v) if v.get("point") is not None else ""
    if v.get("scaled_from_triggered"):
        change += " overall"
    return f"| {v['metric']} | {v['role']} | {change} | {bar_text(v, ship_requires)} | {VERDICT_TEXT.get(v['verdict'], v['verdict'])} |"


def metric_rows(decision):
    verdicts = decision["verdicts"]
    decider = decision["decider"].get("metric")
    flagged = {"breached", "undetermined", "harm_within_margin", "missing", "no_interval", "no_direction", "moved", "worse",
               "below_bar"}
    first = [v for v in verdicts if v["metric"] == decider]
    alarms = [v for v in verdicts if v not in first and v["verdict"] in flagged]
    primaries = [v for v in verdicts if v not in first and v not in alarms and v["role"] == "primary"]
    quiet = [v for v in verdicts if v not in first and v not in alarms and v not in primaries]
    ship_requires = decision["settings"].get("ship_requires", "point_at_bar")
    lines = [row(v, ship_requires) for v in first + alarms + primaries]
    quiet_guards = [v["metric"] for v in quiet if v["role"] == "guardrail"]
    if quiet_guards:
        lines.append(f"| {len(quiet_guards)} other guardrail{'s' if len(quiet_guards) > 1 else ''} | guardrail | | | "
                     f"{'all ' if len(quiet_guards) > 1 else ''}within margin |")
    for v in quiet:
        if v["role"] == "invariant":
            lines.append(row(v, ship_requires))
    return lines


def status_line(status):
    parts = []
    if status.get("days_run") is not None:
        parts.append(f"day {status['days_run']} of {status['planned_days']}" if status.get("planned_days") else f"{status['days_run']} days")
    if status.get("units_per_arm"):
        units = f"{status['units_per_arm']:,} users per arm"
        if status.get("planned_units"):
            units += (f", above the planned {status['planned_units']:,}" if status["units_per_arm"] >= status["planned_units"]
                      else f", short of the planned {status['planned_units']:,}")
        parts.append(units)
    if status.get("whole_weeks") is not None:
        parts.append("whole weeks" if status["whole_weeks"] else "not whole weeks")
    return ", ".join(parts) or "unknown"


def checks_line(checks):
    name = lambda c: c["check"].replace("_", " ")  # noqa: E731
    parts = [f"{name(c)} {c['status']}" for c in checks if c["status"] in ("fail", "flag")]
    passed = [name(c) for c in checks if c["status"] == "pass"]
    unrun = [name(c) for c in checks if c["status"] == "could_not_run"]
    if passed:
        parts.append(f"passed: {', '.join(passed)}")
    if unrun:
        parts.append(f"not run: {', '.join(unrun)}")
    return "; ".join(parts)


def history(decision):
    """Earlier decide.py calls for this experiment in the run log that reached a different call."""
    path = os.environ.get("DS_CHECKS_LOG") or os.path.join(os.getcwd(), ".ds-checks.log")
    if not os.path.exists(path):
        return None
    runs = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            details = entry.get("details") or {}
            if entry.get("check") == "ds_experiment_decide" and details.get("experiment") == decision["experiment"].get("id"):
                runs.append(entry)
    earlier = [r for r in runs if r.get("status") != decision["call"]]
    if not earlier:
        return None
    changed = sorted({k for r in earlier for k, v in (r["details"].get("inputs") or {}).items()
                      if v != (decision.get("inputs") or {}).get(k)})
    calls = ", ".join(f"{CALL_TEXT.get(r['status'], r['status'])} ({r['details'].get('decider')})" for r in earlier[-3:])
    return (f"decide.py ran {len(runs)} times on this experiment; earlier it called {calls}"
            + (f", with different {', '.join(changed)}" if changed else "") + ". The readout shows the last run.")


def reversal(decision):
    """What could change this call, by call type."""
    d, call, items = decision["decider"], decision["call"], decision["open_items"]
    if call == "dont_ship" and d.get("metric"):
        limit = bar_text(d) if d.get("role") == "guardrail" else "an improvement"
        return f"only a version that keeps {d['metric']} at {limit}" if d.get("role") == "guardrail" else \
            f"only a version where {d['metric']} improves"
    if call == "cant_tell":
        return f"fixing the {d.get('check', 'failed check').replace('_', ' ')} problem and rerunning"
    if call == "keep_running":
        return "the remaining data; the call is made at the planned sample"
    if call == "not_ab":
        return "nothing here; this design needs its own readout"
    if call == "escalate":
        others = [i for i in items if not d.get("metric") or d["metric"] not in i]
        return others[0] if others else "triage's judgment on the trade-off; nothing else is open"
    return items[0] if items else "nothing open; every registered check ran and passed"


def triage_line(explained):
    r = explained["readiness"]
    text = f"{r['text']}: {r['reasons'][0]}"
    if r.get("first"):
        text += f". Run first: {r['first']['cut']}"
    return text.rstrip(".") + "."


def triage_prep(decision, explained):
    """The engineer's prep sheet: the standard questions, the questions triage will ask with candidate answers,
    the cuts already run and their results, and the cuts still to run."""
    exp = decision["experiment"]
    by_id = {}
    for inst in explained["patterns"]:
        by_id.setdefault(inst["id"], inst)
    cut_number = {d["cut"]: n for n, d in enumerate(explained["deep_dives"], 1)}
    lines = [f"# {exp.get('name') or exp.get('id')}: triage prep", "",
             f"Ready for triage: {triage_line(explained)}", "",
             f"Scripted call: {decision['call_text']}. {decision['reason']}", "",
             "## Before any single metric", ""]
    for q in explained.get("standard", []):
        if not q.get("note"):
            lines.append(f"- {q['question']} {q['answer'].rstrip('.')}.")
    families = next((q for q in explained.get("standard", []) if q.get("note")), None)
    if families:
        lines += ["", f"Metric families used: {families['answer']}. A wrong pairing here changes every explanation below; "
                      "fix it in the overlay's metric_families.json."]
    lines += ["", "## Questions triage will ask", ""]
    if not explained["questions"]:
        lines += ["- No metric moved clearly enough to be asked about.", ""]
    explained_at = {}
    for n, q in enumerate(explained["questions"], 1):
        change = "" if q["change"].startswith(q["metric"]) else f" It {q['change']}."
        lines.append(f"{n}. {q['question']}{change} Status: {q.get('status', 'untested')}.")
        if not q["candidates"] and q.get("status", "").startswith(("no candidate", "possibly chance")):
            lines.append("   - No candidate explanation. Say so in the meeting: 'unexplained, possibly chance' is an allowed answer.")
        for cid in q["candidates"][:2]:
            inst = by_id.get(cid)
            if not inst:
                continue
            confirmed = inst["status"].startswith("confirmed")
            strength = ("Confirmed" if confirmed else "Fits a cut, not yet confirmed" if inst["status"] == "fits a cut"
                        else "Likely" if inst["score"] >= 1 else "Possible")
            if cid in explained_at:
                lines.append(f"   - {strength}: {inst['name']}, as in question {explained_at[cid]}.")
                continue
            explained_at[cid] = n
            lines.append(f"   - {strength} [{inst['label']}]: {inst['name']}. {inst['mechanism']}")
            evidence = list(dict.fromkeys(inst["evidence"]))
            lines.append(f"     Evidence: {'; '.join(evidence[:4])}.")
            cuts = [cut_number[d["cut"]] for d in inst["deep_dives"] if d["cut"] in cut_number]
            if inst.get("awaiting"):
                lines.append(f"     Not confirmed until: {'; '.join(inst['awaiting'][:2])}.")
            if cuts and not confirmed:
                lines.append(f"     To check: cut {', '.join(str(c) for c in cuts[:2])} below.")
        lines.append("")
    if explained.get("cut_results"):
        lines += ["## Cuts already run", ""]
        lines += [f"- {r['cut'][:1].upper() + r['cut'][1:]}: {r['outcome']} '{r['tests']}'. {r['detail']}."
                  for r in explained["cut_results"]] + [""]
    ruled = explained.get("ruled_out") or []
    if ruled:
        lines += ["## Ruled out", ""]
        lines += [f"- {r['name']}: {'; '.join(r['ruled_out_by'])}." for r in ruled[:5]] + [""]
    exploratory = explained.get("exploratory") or []
    if exploratory:
        lines += ["## Exploratory, not for the decision", "",
                  "Found by scanning, not registered in advance: worth a rerun or a new test, not a reason to ship.", ""]
        lines += [f"- {i['name']}" + (f" ({i['segment']})" if i.get("segment") else "") + f": {'; '.join(i['evidence'][:2])}."
                  for i in exploratory[:4]] + [""]
    lines += ["## Cuts to run before triage, in order", ""]
    lines += [f"{n}. {d['cut'][:1].upper() + d['cut'][1:]} (tests: {d['tests']}). Supports it: {d['confirms']}. "
              f"Against it: {d['refutes']}." + (" Scored by script once its result is in evidence.json." if d.get("scriptable")
                                                 else " Record its outcome in evidence.json under cuts.")
              for n, d in enumerate(explained["deep_dives"], 1)] or ["- Nothing to run."]
    lines += ["", "Explanations become confirmed only when a cut's result is passed back to explain.py with --evidence; "
              "a cut that contradicts a candidate removes it.", ""]
    used = [i for i in explained["patterns"] if i.get("source") and i["id"] in explained_at]
    if used:
        lines += ["## Sources", ""] + [f"- {i['name']}: {i['source']}." for i in used] + [""]
    return "\n".join(lines)


def readout(decision, notes, scorecard, explained=None):
    exp = decision["experiment"]
    lines = [f"# {exp.get('name') or exp.get('id') or 'Experiment'}: {decision['call_text']}", "",
             f"Decision: {decision['call_text']}. {decision['reason']}", "",
             f"Ask: {decision['ask']}", "",
             f"Status: {status_line(decision['status'])}.", "",
             f"Could reverse it: {reversal(decision)}.", ""]
    if explained:
        lines += [f"Triage: {triage_line(explained)}", ""]
    past = history(decision)
    if past:
        lines += [f"History: {past}", ""]
    if decision["call"] != "not_ab":
        lines += [f"| Metric | Role | Change ({round(100 * decision['settings']['ci_level'])}% interval) | Bar | Verdict |",
                  "|---|---|---|---|---|"] + metric_rows(decision) + [""]
    lines += [f"Checks: {checks_line(decision['checks'])}.", ""]
    change = notes.get("would_change") or []
    lines += ["## What would change the call", ""] + ([f"- {t}" for t in change] or ["- Nothing short of new data."]) + [""]
    why = notes.get("why_it_moved") or []
    lines += ["## Why it moved", ""] + ([f"- [{w['label']}] {w['text']}" for w in why]
                                         or ["- [guess] No segment cuts were read, so the mechanism is not known."]) + [""]
    view = notes.get("analyst_view")
    if view:
        lines += ["## Analyst's view", "", f"The analyst would call this {CALL_TEXT[view['call']]}: {view['reason']}", ""]
    sec = decision["secondaries"]
    survivors = sec["survivors"]
    lines += ["## Appendix", ""]
    shown = lambda s: f"{100 * s['rel_delta']:+.2f}%" if s.get("rel_delta") is not None else f"{s.get('abs_delta'):+g}"  # noqa: E731
    if survivors:
        names = ", ".join(f"{s['metric']} ({shown(s)})" for s in survivors[:5])
        more = f" and {len(survivors) - 5} more" if len(survivors) > 5 else ""
        lines.append(f"- Secondary metrics: {sec['tested']} tested; after a false-discovery correction these moved: {names}{more}. "
                     "They are exploratory and don't change the call.")
    elif sec["tested"]:
        lines.append(f"- Secondary metrics: {sec['tested']} tested; none moved after a false-discovery correction.")
    if sec.get("untested"):
        lines.append(f"- Secondary metrics not tested: {sec['untested']} (the scorecard gives them no p-values).")
    settings = decision["settings"]
    levels = settings.get("levels", {})
    cuped = {True: "on (changes are adjusted for pre-test behaviour)", False: "off", None: "not recorded"}[settings.get("cuped")]
    level_text = f"{round(100 * settings['ci_level'])}% intervals"
    if levels.get("primary") and abs(levels["primary"] - settings["ci_level"]) > 1e-9:
        level_text += f", {100 * levels['primary']:.1f}% for primaries (shared across them)"
    if settings.get("sequential"):
        level_text += ", sequential (they stay valid when the test is checked early)"
    lines.append(f"- Settings: {level_text}; variance reduction {cuped}; "
                 f"harm inside a margin goes to {settings['standards']['harm_within_margin']}.")
    for v in decision["verdicts"]:
        if v.get("triggered"):
            t = v["triggered"]
            lines.append(f"- {v['metric']} is measured on triggered traffic ({100 * t['point']:+.2f}%); the call uses the overall "
                         f"change, scaled by its {100 * v['scaled_from_triggered']:.1f}% share of the control total.")
    quiet = [v["metric"] for v in decision["verdicts"] if v["role"] == "guardrail" and v["verdict"] in ("within", "no_harm_detected")]
    if quiet:
        lines.append(f"- Guardrails within margin: {', '.join(quiet)}.")
    for item in decision["fallbacks"]:
        lines.append(f"- Fallback: {item}.")
    lines += ["- Every metric, with means and p-values: all_metrics.md.", f"- {GLOSSARY}", ""]
    return "\n".join(lines)


def all_metrics(decision, scorecard):
    lines = [f"# {decision['experiment'].get('name')}: every metric", "",
             "| Metric | Listed as | Control | Test | Change | Interval | p |", "|---|---|---|---|---|---|---|"]
    for comparison in scorecard["comparisons"]:
        if comparison.get("test_group") != decision["experiment"].get("test_group"):
            continue
        for m in comparison["metrics"]:
            ci = m.get("rel_ci")
            lines.append(f"| {m['metric']} | {m.get('listed_as')} | {m.get('control_mean')} | {m.get('test_mean')} | "
                         f"{'' if m.get('rel_delta') is None else f'{100 * m['rel_delta']:+.2f}%'} | "
                         f"{'' if not ci else f'{100 * ci[0]:+.2f}% to {100 * ci[1]:+.2f}%'} | {m.get('p_value')} |")
    return "\n".join(lines) + "\n"


def spine(decision):
    """The cold-read answer key, from the decision only. The takeaways are what a
    reader should restate when asked for the answer, the decision, the key number
    and the main risk."""
    answer = f"{decision['call_text']}: {decision['reason']}"
    decider = decision["decider"]
    if len(answer.split()) > 30:
        answer = f"{decision['call_text']}, decided by {decider.get('metric') or decider.get('check', '').replace('_', ' ')}."
    lead = next((v for v in decision["verdicts"] if v["role"] == "primary" and v.get("point") is not None), None)
    if decider.get("metric") and decider.get("point") is not None:
        first = f"{decider['metric']} {describe(decider)} against {bar_text(decider)}."
    else:
        first = _first_words(_sentence_case(decider.get("summary", decision["reason"])), 25)
    if decision["call"] == "escalate" and lead and lead["metric"] != decider.get("metric"):
        second = f"{lead['metric']} {describe(lead)} against {bar_text(lead)}."
    else:
        second = decision["ask"]
    third = f"Could reverse it: {reversal(decision)}."
    return {"type": "readout", "reader": "the launch review (product, engineering and data science leads) deciding whether to launch",
            "answer": answer, "decision": decision["ask"], "takeaways": [_first_words(t, 40) for t in (first, second, third)]}


def _sentence_case(text):
    return text[:1].upper() + text[1:]


def _first_words(text, n):
    words = text.split()
    return " ".join(words[:n]) + ("..." if len(words) > n else "")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--decision", required=True)
    parser.add_argument("--scorecard", required=True)
    parser.add_argument("--notes", help="the model's notes.json; omit for a readout with no mechanism")
    parser.add_argument("--explain", help="explain.json from explain.py; adds the triage line and triage_prep.md")
    parser.add_argument("--out-dir")
    args = parser.parse_args(argv)
    try:
        with open(args.decision, encoding="utf-8") as handle:
            decision = json.load(handle)
        with open(args.scorecard, encoding="utf-8") as handle:
            scorecard = json.load(handle)
        explained = None
        if args.explain:
            with open(args.explain, encoding="utf-8") as handle:
                explained = json.load(handle)
        notes = {}
        if args.notes:
            with open(args.notes, encoding="utf-8") as handle:
                notes = json.load(handle)
    except (OSError, ValueError) as err:
        print(f"render: {err}", file=sys.stderr)
        return 2
    problems = check_notes(notes, decision, scorecard)
    if problems:
        print("render: notes.json was not accepted:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    out = args.out_dir or os.path.dirname(os.path.abspath(args.decision))
    files = {"readout.md": readout(decision, notes, scorecard, explained), "all_metrics.md": all_metrics(decision, scorecard),
             "spine.json": json.dumps(spine(decision), indent=2)}
    if explained:
        files["triage_prep.md"] = triage_prep(decision, explained)
    for name, text in files.items():
        with open(os.path.join(out, name), "w", encoding="utf-8") as handle:
            handle.write(text)
    print(f"render: wrote {', '.join(sorted(files))} to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
