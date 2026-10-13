# Porting kangaroo to the work laptop

For the coding agent on the work laptop (Codex, Cursor or Claude Code) and the person who runs it. The work agent becomes kangaroo, with its four work seams connected. Its blocking machinery moves to `legacy/`.

The laptop reads this repository and writes nothing back to it. Only counts come home. If a document in the work repository disagrees with this file, this file wins. List each disagreement.

## 1. Order of work

Do not rewrite kangaroo. Copy it. Write new code only for the seams in section 4. Do one session for each row, in plan mode, and get the person's approval before you edit. A session is done only when its check passes. Then commit it locally.

| Session | Goal | Check |
|---|---|---|
| 1. Replace | Inventory, approval, move the current code to `legacy/`, copy kangaroo, analyze one real flight | Tests and demo pass. The flight gets M1 output. No code imports `legacy/` |
| 2. Seams | Connect the overlay, the warehouse and the config (sections 3 and 4) | Each cut runs or shows an access gap. No rows in the trace. `ste.check` finds 0 problems in the report |
| 3. Loop and tests | Confirm 3 to 5 cards. Remove the old tests that test `legacy/` or pin internal state (section 7) | A report cites a confirmed card. The suite passes |

In session 1, list what the work repository has that kangaroo does not, and get approval before the move. The prompts for the three sessions are in the work-prompts repository.

## 2. Prove the runtime

```
python3 -m unittest discover -s tests
python3 -m kangaroo demo --out /tmp/kangaroo-demo
```

Both use only the Python standard library (3.14 works). The tests must pass, and `DEMO.md` must say that the lesson passed with 0 regressions. If one fails, stop and report its name and message.

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
2. **Warehouse.** Complete `McpWarehouse` in `kangaroo/sources/warehouse.py`. Send the Spark SQL from `bind` through the allow-listed read tool (by name, never the dashboard write tool). Poll the status tool for up to 15 minutes. Return `{statement_id, label, rows, row_count, sql_sha}`. On a failure, raise `TimeoutError` or `QueryRefused`: the run records an access gap and continues. Use the connection code of the current agent for the endpoint and its authentication. Add the `mcp` kind to `warehouse_from` in `kangaroo/run.py`, the one place that builds a warehouse. Before you mark a template as runnable, run `cuts.dry_run_sql` (Spark, `LIMIT 0`) on it.
3. **Config.** Complete `CliConfig` in `kangaroo/sources/config.py`. If the CLI runs only outside the sandbox, the person runs it and gives the JSON with `--config`.
4. **Cards.** For 3 to 5 past flights, the owner gives the cause and the lesson: `cards confirm EXPERIMENT --cause ... --lesson ... --approver NAME`. Later reports cite only confirmed cards.

Until a seam is connected, its stub raises `NotImplementedError`. Do not replace it with a default.

## 5. Remove the blocking machinery

The current agent marks M0 and M1 as not ready on each flight. The cause is its state machinery, not the data. Session 1 moves all current code to `legacy/`, with the five state families. After the move, no code imports `legacy/`, one `grade()` remains (`kangaroo/m0/grade.py`), and the split check uses `split_count_metric` or the current exposure source, never the retired first-exposure table. Delete the test that pins the receipt flags to false.

## 6. Rules at work

- Only aggregates leave the warehouse. Rows never go into a trace, a reply or a lesson.
- Traces, cases, lessons and cards stay on the laptop.
- The deep dives need T3 through the privacy gate. Until then, they are access gaps, and the grade does not depend on them.
- A lesson changes the knowledge only after its evaluation passes and the owner approves it by name.
- In a chat, the skill runs `bin/kangaroo` in the shell. The MCP server is experimental: see `experimental/mcp/README.md`.

## 7. Test rules

For the suite of the work repository, which has many unit tests that pin internal state:

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
