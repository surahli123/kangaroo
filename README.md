# Kangaroo

Kangaroo is a search experiment agent. You give it the scorecard of one A/B experiment. It tells the owner three things: if the experiment can go to triage, why its metrics changed, and what to do before the meeting.

- **M0** examines the setup and the data, and gives each arm a grade. A check that cannot run makes the grade lower. It does not stop the analysis.
- **M1** explains the changes. It runs the cuts that it can script on the query table. A cause is confirmed only when a cut and its control cut agree.
- **Learning loop.** Each run keeps a trace. The verdicts of a person become typed lessons. A lesson changes the agent only if it passes an evaluation on past runs that it did not come from, and a person approves it by name. Reports cite what similar, confirmed experiments taught.

Kangaroo runs at home on a synthetic warehouse with the same shape as the work warehouse. It uses only the Python standard library.

## The report

Each report has these sections. It uses ASD-STE100 writing rules: short sentences, active voice, one meaning for each word. Each claim has its evidence next to it.

| Section | What it gives |
|---|---|
| Summary | Grade, triage readiness, launch call with its deciding result, open questions, steps before triage, triage questions |
| Metrics | Each contract metric: its change, the 95% CI, the result against its bar or limit, and its cause |
| Causes | Each candidate cause, its support (Confirmed, Consistent, Likely, Ruled out) and the cuts behind it |
| Data checks | Each M0 check: its result, its evidence and its source |

`render/ste.py` tests the writing rules that a script can test. The test suite runs it on every report. It does not test the approved dictionary, because that dictionary is licensed.

## See it work

```
python3 -m kangaroo demo --out /tmp/kangaroo-demo
```

This runs the full loop in about one minute. Six past experiments are analyzed and frozen. A reviewer flags one of them. Kangaroo proposes a lesson. The lesson passes its evaluation on a different experiment, with no regressions, and a person approves it. Then a new experiment arrives as a scorecard file and gets its report. `DEMO.md` in the output folder shows each step. Each report is in `runs/<name>/readout.html`.

## Use it in a chat

Every chat host can run a shell command, so the supported route is the skill (`skills/kangaroo/SKILL.md`) and the command line (`bin/kangaroo`).

- **Open this folder in the host.** Claude Code loads the skill from `.claude/skills/`. Codex reads `AGENTS.md`. Cursor reads `.cursor/rules/kangaroo.mdc`.
- **Use it from a different folder.** `python3 bin/kangaroo setup` prints what to add for each host, with absolute paths. `python3 bin/kangaroo install-skill ~/.claude/skills` copies the skill with this folder's path in its commands.

Then write in the chat:

> Here is the scorecard of my experiment: exports/scorecard.csv. Can it go to triage?

The host runs `analyze` and gives the summary as kangaroo writes it. It answers follow-up questions with `why`, records your verdicts with `review`, and proposes and evaluates lessons. It does not approve a lesson or a report unless you approve it in the chat and give your name.

An MCP server is also available for research, but it is not the supported route. See `experimental/mcp/README.md`.

## Use it from the command line

```
python3 -m kangaroo analyze --scorecard S.csv [--contract C.json] [--config CFG.json]
python3 -m kangaroo why "1P Long Click Rate"
python3 -m kangaroo review wrong --metric "1P Long Click Rate" --note "..." --reviewer NAME
python3 -m kangaroo lessons propose | list | evaluate ID | approve ID --approver NAME
python3 -m kangaroo report --part summary | details | all
```

Runs, frozen cases, lessons and cards are in one workspace: `KANGAROO_HOME` (the default is `~/.kangaroo`).

## How a run works

1. Read the scorecard (the latest T1 snapshot and its daily series), and the config if there is one.
2. **M0** checks the config, the sample split, the metric coverage, the consistency, the power, the data quality and the duration. Each check gives PASS, WARN, BLOCK or UNKNOWN, with its source. `docs/specs/stage-contract.md` changes them into one grade for each arm: not readable (a BLOCK), directional only (an UNKNOWN, or the test has not ended) or decision grade.
3. **M1** works on one arm at a time:
   - The launch call uses only that arm and control. Thus one bad arm cannot decide a different arm.
   - Each candidate cause has a cut where it acts, and a control cut that its look-alike causes fail. Kangaroo runs both through a fixed, read-only runner. Each cut is a ratio of sums for each user, so its numbers agree with T1.
   - Confirmed: both cuts agree. Consistent: one cut agrees, or a full-page cut compares different query sets. Likely: metrics changed together, and no cut tested the cause.
4. Write the report (Markdown and HTML), the trace, a review form, a card draft and the frozen case.

## Results at home

These results come from 13 synthetic flights with known causes, and 10 adversarial flights from a separate review.

- **Grades:** correct on 11 of 13 flights. The other 2 have a renamed primary metric. They are directional only until a person approves the alias lesson. Then they are correct.
- **Causes:** kangaroo confirms the planted cause on 10 of 13 flights. The 3 misses have a renamed placement metric, and one mapping lesson fixes them. No flight confirms a cause that is not planted. No claim is confirmed without the cuts behind it.
- **Learning loop:** a useful lesson passes on held-out runs with 0 regressions. The evaluation refuses a harmful lesson, a vacuous lesson that names its own targets, an edited report and a probing cut template.

## Layout

- `kangaroo/`: the run (`run.py`), `api.py` (the functions that the command line calls), `hosts.py`, `demo.py`
  - `sources/`, `m0/`, `m1/` (method, cut runner, pattern library, cut templates), `render/` (report, STE check, HTML)
- `learning/`: cards, lessons, paired evaluation and approval, the knowledge version
- `skills/kangaroo/`: the skill for a chat. `.agents/skills/`: skills for a coding agent that changes the code
- `experimental/mcp/`: the MCP route, kept for research
- `bin/kangaroo`: the launcher. `overlay.example/`: warehouse settings, standards, contracts
- `synth/`: the synthetic warehouse. `tests/`: unittest only

For the work laptop, see `PORTING.md`.
