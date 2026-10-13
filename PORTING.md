# Porting kangaroo to the work laptop

This is for the coding agent on the work laptop (Codex, Cursor or Claude Code), and for the person who runs it. Kangaroo is the reference. The work agent becomes kangaroo, with its four work seams connected. The blocking machinery of the current agent moves to `legacy/`.

The laptop reads this repository and writes nothing back to it. Only counts come home. If a document in the work repository disagrees with this file, this file wins. List each disagreement.

## 1. Order of work

Do one phase in each session. Make a plan first (plan mode) and get the person's approval before you edit. A phase is done only when its check passes. Then commit it locally.

| Phase | Goal | Check |
|---|---|---|
| 0. Audit | Read only. Map the work repository to kangaroo. Write `PLAN.md` | The existing tests ran. `PLAN.md` gives file:line evidence for each item in section 5 |
| 1. Unblock | One `grade()`. An M0 check that cannot run makes the grade lower and never stops M1 (section 5) | A grep for sealed, packet, checkpoint and accepted outside `legacy/` finds nothing. One real flight gets M1 output with a directional or decision grade |
| 2. Core | Copy the analysis core (section 2). `analyze --scorecard export.csv` works | The kangaroo tests and the demo pass on the laptop |
| 3. Report | Use the kangaroo renderer and its STE check | `ste.check` finds 0 problems in the report of a real flight |
| 4. Seams | Connect the warehouse, the config and the overlay (sections 3 and 4) | Each cut runs or shows an access gap. No rows in the trace |
| 5. Loop and tests | Confirm 3 to 5 cards. Make the tests follow section 7 | A report cites a confirmed card. The suite is end to end first |

## 2. Prove the runtime

```
python3 -m unittest discover -s tests
python3 -m kangaroo demo --out /tmp/kangaroo-demo
```

Both use only the Python standard library (3.14 works, no pip). The tests must pass, and `DEMO.md` must say that the lesson passed with 0 regressions. If one fails, stop. Report the name and the message of the failing test.

Copy `kangaroo/vendor/ds_experiment_tools/` without changes, with `srm.py` and `_common.py`, and record the file hashes. Do not write the tools again.

## 3. Translate the aliases

The code uses T1 to T4 and canonical column names. Translate them through the local alias map into `overlay/warehouse.json`. Start from `overlay.example/`.

| Key | Value |
|---|---|
| `tables` | t1 (the vendor scorecard view), t3 (query-level metrics), t4 (tenant enablement), assignment (the current first-exposure source, not the source that was retired in May 2026) |
| `t1_columns` | the real T1 column names. Under `_percent_fields`, the relative fields that T1 stores as percents |
| `t3_columns` | the T3 names of the columns that the cut templates use (`t.<name>` in `m1/cut_templates.json`) |
| `assignment` | the unit, arm, experiment and first-exposure columns |
| `split_count_metric` | the T1 metric that counts all exposed units. Without it, the sample split is UNKNOWN: the flight is directional, not blocked |
| `approved` | the tables that the privacy gate approved. A table that is not approved makes its cuts access gaps, and the report says so |
| `cohort` | the paying-tenant flag in T4 |

No alias or real name goes into code, only into the overlay, which stays on the laptop. The T1 arm names must be the same as the config group names. If they are not, map them in the overlay, or the config check blocks.

## 4. Connect the four seams

1. **Scorecard file (works now).** `analyze --scorecard export.csv` reads T1 rows that you export from the vendor console. M0, the launch call and the report need nothing more. The deep-dive cuts show as access gaps.
2. **Warehouse.** Complete `McpWarehouse` in `kangaroo/sources/warehouse.py`. Send the Spark SQL from `bind` through the allow-listed read tool (by name, never the dashboard write tool). Poll the status tool for up to 15 minutes. Return `{statement_id, label, rows, row_count, sql_sha}`. On a failure, raise `TimeoutError` or `QueryRefused`: the run records an access gap and continues. Use the connection code of the current agent for the endpoint and its authentication. Before you mark a template as runnable, run `cuts.dry_run_sql` (Spark, `LIMIT 0`) on it.
3. **Config.** Complete `CliConfig` in `kangaroo/sources/config.py`. If the CLI runs only outside the sandbox, the person runs it and gives the JSON with `--config`.
4. **Cards.** For 3 to 5 past flights, the owner gives the cause and the lesson: `cards confirm EXPERIMENT --cause ... --lesson ... --approver NAME`. Later reports cite only confirmed cards.

Until a seam is connected, its stub raises `NotImplementedError`. Do not replace it with a default.

## 5. Remove the blocking machinery

The current agent marks M0 and M1 as not ready on each flight. The cause is its state machinery, not the data.

1. Move the five state families to `legacy/`. After the move, a grep for sealed, packet, checkpoint and accepted outside `legacy/` finds nothing, one `grade()` remains (`kangaroo/m0/grade.py`), and no code imports `legacy/`.
2. Delete the test that pins the receipt flags to false. Replace it with a test that gets each grade from real checks, as `tests/test_agent.py` does.
3. Point the split check at the current exposure source or at `split_count_metric`, never at the retired first-exposure table.

## 6. Rules at work

- Only aggregates leave the warehouse. Rows never go into a trace, a reply or a lesson.
- Traces, cases, lessons and cards stay on the laptop.
- The deep dives need T3 through the privacy gate. Until then, they are access gaps, and the grade does not depend on them.
- A lesson changes the knowledge only after its evaluation passes and the owner approves it by name.
- In a chat, open this folder: `AGENTS.md` points the agent to the skill, and the skill runs `bin/kangaroo` in the shell. The MCP server is experimental. `experimental/mcp/README.md` gives the open questions to examine on the laptop.

## 7. Test rules

Use these rules for the suite of the work repository. It has many unit tests that pin internal state.

1. Test through the doors that a person uses: a run from a scorecard or a warehouse, a lesson through the loop, a command through the API or the CLI.
2. Assert outcomes, not internal state. Do not pin a flag, a status word, a counter or a private field. Assert the result that the flag must cause.
3. For each safety boundary, add one assertion that tries to cross it and is refused, next to the test of the allowed action.
4. Keep a unit test only when no end-to-end path gets to the behavior cheaply, for example a dialect that you cannot run at home. Its docstring tells why.
5. Assert on structured outputs: counts, records, return values, files. Do not assert on prose. Use at most one check that the report has its top section.
6. Make expensive fixtures one time and share them. Tests read them and do not change them.
7. The oracle is the planted truth, not the output of the last run.
8. A change in a held-out result is a regression, unless the oracle says that it is an improvement.
9. Before you delete a test, measure the lines that only it runs. Move its assertion, or show that other tests run those lines or that they are dead code.
10. Keep the suite at or below its current runtime. A new test uses the shared fixtures, never a second warehouse.

## 8. What to send home

Counts only:
- the test summary;
- the seams that are connected;
- for one real flight: the grade of each arm, the number of cuts that ran and of access gaps, and the names of the checks that did not pass.

Do not send metric values, rows or names from the alias map.
