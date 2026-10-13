# Kangaroo: instructions for the coding agent

## Intent

Give triage a readout it can act on for one search experiment.
- M0 says whether the setup and data can be trusted.
- M1 says why metrics moved, what to check next, and whether the flight is ready for triage.

An M0 check that can't run lowers the grade. It never stops M1.

## Two jobs

- **Someone asks about an experiment in this chat.** Follow `skills/kangaroo/SKILL.md`: run `python3 bin/kangaroo <command>` in a shell, and give the summary as kangaroo writes it.
- **Someone asks you to change the agent.** Read `.agents/skills/kangaroo_m0_readiness` or `kangaroo_m1` first, change the code, and run `python3 -m unittest discover -s tests` before you finish.

## Run

```
python3 -m kangaroo analyze --scorecard <file> [--contract <file>] [--config <file>]
python3 -m kangaroo analyze --experiment <name | config id | console URL> --warehouse sqlite:<path> --configs <dir>
python3 -m kangaroo demo --out <dir>
```

A run writes `readout.md`, `readout.html`, `trace.json`, `review.json`, `card_draft.json` and `case/` into `runs/<experiment>-<time>/` of the workspace (`KANGAROO_HOME`, default `~/.kangaroo`).

## Boundaries

1. Inputs. Never edit an input, the T1 rows, a config or a contract to change a result. If an input is wrong, say so.
2. Numbers. Every number comes from the tools. Never compute or restate one in your own words.
3. The warehouse. Use only the allow-listed read tool. Never call a dashboard or write tool.
4. What leaves this machine. Rows never enter a trace or a reply: only statement IDs, row counts and aggregates. Traces, lessons and cases stay here.
5. Knowledge. Lessons change it only after an evaluation passes and a person approves by name. Never edit the knowledge files directly.
6. M0. No lesson changes a check, the grade, or the cut runner; those go through engineering review.
7. Confirmation. A cause is confirmed only when its cut and its control cut agree. Never relax that to make a readout look stronger.
8. Writing. The report follows the ASD-STE100 rules that `render/ste.py` tests, and each claim shows its evidence. A change to the report must keep `ste.check` at zero problems.

## Map

- `kangaroo/`: the run (`run.py`), `api.py`, `hosts.py`, `demo.py`, and `mcp_server.py` (experimental, see `experimental/mcp/`).
  - `sources/`: warehouse, T1, config, resolver.
  - `m0/`: checks and the grade.
  - `m1/`: method, cuts, the pattern library, the cut templates.
  - `render/`: the readout, the STE check, HTML.
- `learning/`: cards, lessons, evaluation, approval, the knowledge version, and the `loop` command.
- `docs/specs/stage-contract.md`: the only rule between M0 and M1.
- `skills/kangaroo/`: the conversation skill. `.agents/skills/`: `kangaroo_m0_readiness` and `kangaroo_m1`.
- `synth/`: the synthetic warehouse, for tests and evaluation at home.
- `tests/`: unittest. Run them with `python3 -m unittest discover -s tests`.
