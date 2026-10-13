# Kangaroo

Kangaroo is a search experiment agent. Give it one A/B experiment's scorecard and it tells the owner three things in plain words: whether the experiment can go to triage, why its metrics moved, and what to check before the meeting.

- **M0** checks the setup and the data, and grades each arm. A check that can't run lowers the grade; it never stops the analysis.
- **M1** explains the movements. It runs the deep-dive cuts it can script against the query-level table and counts a cause as confirmed only when the data separates it from its look-alikes.
- **Learning loop.** Each run leaves a trace. A person's verdicts become typed lessons. A lesson changes the agent only after a paired evaluation on held-out past runs passes and a person approves it by name. Reports cite what confirmed, similar past experiments taught.

Everything here runs at home on a synthetic warehouse shaped like the work one, with the Python standard library only.

## See it work

```
python3 -m kangaroo demo --out /tmp/kangaroo-demo
```

In about a minute this runs the whole loop. Six past experiments are analyzed and frozen. A reviewer's flag yields a lesson, which passes evaluation on a held-out experiment with no regressions and is approved. A new experiment then arrives as a scorecard file and gets its report. `DEMO.md` in the output folder walks through each step, and each report is in `runs/<name>/readout.html`.

## Use it in a chat

Kangaroo runs as an MCP server that Claude Code (CLI and desktop app), Codex (CLI and desktop app) and Cursor can call, plus a conversation skill (`skills/kangaroo/SKILL.md`).

- **Open this folder in the host.** `.mcp.json` (Claude Code), `.cursor/mcp.json` (Cursor) and the skill links in `.claude/skills/` and `.agents/skills/` load on their own; approve the server when the host asks.
- **Use it from any folder.** `python3 bin/kangaroo setup` prints the exact command or config for each host, with absolute paths. For Claude Code that's `claude mcp add --scope user kangaroo -- python3 /path/to/bin/kangaroo mcp`; for Codex, `codex mcp add kangaroo -- python3 /path/to/bin/kangaroo mcp`.

Then talk to it:

> Here is my experiment's scorecard: exports/scorecard.csv. Can it go to triage?
> Why did 1P Long Click Rate fall?
> That's wrong, it's 3P results pushing 1P results down. Record my verdict, I'm Suri.

The host replies with the report's "In one minute" text as written, answers follow-ups from the run's trace, records verdicts, and proposes and evaluates lessons. It approves nothing unless the person says so, with their name. Without the MCP tools, the skill runs the same commands in a shell.

## Use it from the command line

```
python3 -m kangaroo analyze --scorecard S.csv [--contract C.json] [--config CFG.json]
python3 -m kangaroo why "1P Long Click Rate"
python3 -m kangaroo review wrong --metric "1P Long Click Rate" --note "..." --reviewer NAME
python3 -m kangaroo lessons propose | list | evaluate ID | approve ID --approver NAME
python3 -m kangaroo report --part details | triage_prep | all
```

Runs, frozen cases, lessons and cards live in one workspace, `KANGAROO_HOME` (default `~/.kangaroo`).

## How a run goes

1. Read the scorecard (T1's latest snapshot and its daily series) and the config, if there is one.
2. **M0**: config, sample split, metric coverage, consistency, power, data quality and duration. Each check is PASS, WARN, BLOCK or UNKNOWN with its source. `docs/specs/stage-contract.md` turns them into a grade per arm: not permitted (a BLOCK), directional only (an UNKNOWN, or mid-flight) or decision grade.
3. **M1**, one arm at a time:
   - The launch call comes from that arm and control alone, so one bad arm never decides another.
   - Each candidate cause names a cut where it acts and a control cut that its look-alikes would fail. The agent runs both through a fixed, read-only runner; each is a ratio of sums per user that ties to T1.
   - A cause is **Confirmed** when both agree. When one agrees, or a full-page cut compares different query sets because the full-page share moved, it **Fits the data, not yet confirmed**. A cause from co-movement alone is **Likely**.
4. Write the report (Markdown and HTML), a triage prep per arm, the trace, a review form, a card draft and the frozen case.

## Results at home

These results come from 13 synthetic flights with planted causes, plus 10 adversarial flights from a separate review.

- **Grades:** right on 11 of 13 flights. The other two have a renamed primary metric, so they read as directional only until a person approves the alias lesson; then they're right too.
- **Causes:** the planted cause is confirmed on 10 of 13. The 3 misses share a renamed placement metric that one mapping lesson fixes. No flight confirms a cause that wasn't planted, and no claim is stated as confirmed without the cuts behind it.
- **Learning loop:** a useful lesson passes on held-out runs with 0 regressions. A harmful one, a vacuous one that names its targets, an edited report and a probing cut template are each refused.
- **Tests:** `python3 -m unittest discover -s tests` runs 36 tests in under two minutes.

## Layout

- `kangaroo/`: the run, `api.py` (what a chat or the command line calls), `mcp_server.py`, `hosts.py`, `demo.py`
  - `sources/`, `m0/`, `m1/` (method, cut runner, pattern library, cut templates), `render/` (report, brief, HTML)
- `learning/`: cards, lessons, paired evaluation and approval, the knowledge version
- `skills/kangaroo/`: the conversation skill; `.agents/skills/`: skills for a coding agent that changes the code
- `bin/kangaroo`: the launcher; `overlay.example/`: warehouse settings, standards, contracts
- `synth/`: the synthetic warehouse; `tests/`: unittest only

At work, see `PORTING.md`.
