# Porting kangaroo to the work laptop

This is for the coding agent on the work laptop, and for the person who runs it. Kangaroo is the reference: the work agent becomes kangaroo with the four work seams wired. The current agent's blocking machinery moves to `legacy/`.

The laptop reads this repository and writes nothing back to it. Only counts come home.

## 1. Prove the runtime first

```
python3 -m unittest discover -s tests
python3 -m kangaroo demo --out /tmp/kangaroo-demo
```

Both use the Python standard library only (3.14 works; no pip). The tests should pass and `DEMO.md` should say the lesson passed with 0 regressions. If either fails, stop and report the failing test's name and message.

## 2. Translate the aliases

The code says T1 to T4 and uses canonical column names. Translate them through the local alias map into `overlay/warehouse.json`, starting from `overlay.example/`:

| Key | What to fill in |
|---|---|
| `tables` | t1 (the vendor scorecard view), t3 (query-level metrics), t4 (tenant enablement), assignment (the current first-exposure source, not the one retired in May 2026) |
| `t1_columns` | T1's real column names, and under `_percent_fields` the relative fields stored as percents |
| `t3_columns` | T3's names for the columns the cut templates use (`t.<name>` in `m1/cut_templates.json`) |
| `assignment` | unit, arm, experiment and first-exposure columns |
| `split_count_metric` | the T1 metric that counts all exposed units. Without it the sample split is UNKNOWN: the flight is directional, never blocked |
| `approved` | the tables the privacy gate has approved. An unapproved table makes its cuts access gaps, and the report says so |
| `cohort` | the paying-tenant flag in T4 |

No alias or real name goes into code: only into the overlay, which stays on the laptop. T1's arm names must equal the config's group names; if they don't, map them in the overlay, or the config check blocks.

## 3. Wire the four seams

1. **Scorecard file (works today).** `analyze --scorecard export.csv` reads T1 rows exported from the vendor console. Nothing else needs wiring for M0, the launch call and the report; the deep-dive cuts show as access gaps.
2. **Warehouse.** Finish `McpWarehouse` in `kangaroo/sources/warehouse.py`. Submit the finished Spark SQL from `bind` through the allow-listed read tool (by name, never the dashboard write tool), poll the status tool for up to 15 minutes, and return `{statement_id, label, rows, row_count, sql_sha}`. Raise `TimeoutError` or `QueryRefused` on failure: the run records an access gap and goes on. Reuse the current agent's connection code for the endpoint and its authentication. Before a template is marked runnable, run `cuts.dry_run_sql` (Spark, `LIMIT 0`) on it.
3. **Config.** Finish `CliConfig` in `kangaroo/sources/config.py`. If the CLI only runs outside the sandbox, the person runs it and passes the JSON with `--config`.
4. **Cards.** For 3 to 5 past flights, the owner states the cause and the lesson: `cards confirm EXPERIMENT --cause ... --lesson ... --approver NAME`. Later reports cite only confirmed cards.

Until a seam is wired, its stub raises `NotImplementedError`. Never paper over it with a default.

## 4. Retire the blocking machinery

The current agent marks M0 and M1 not ready on every flight because of its state machinery, not the data:

1. Delete the five state families, by moving them to `legacy/`. Afterwards a grep for sealed, packet, checkpoint and accepted outside `legacy/` finds nothing, one `grade()` remains (`kangaroo/m0/grade.py`), and nothing imports `legacy/`.
2. Replace the test that pins the receipt flags false with kangaroo's `Grade` tests, which reach every grade.
3. Point the split check at the current exposure source or `split_count_metric`, never the retired first-exposure table.
4. Copy `kangaroo/vendor/ds_experiment_tools/` verbatim, including `srm.py` and `_common.py`, and record their file hashes. Never re-implement them.

## 5. Rules that hold at work

- Only aggregates leave the warehouse. Rows never enter a trace, a reply or a lesson.
- Traces, cases, lessons and cards stay on the laptop.
- The deep dives need T3 through the privacy gate. Until then they're access gaps, and the grade doesn't depend on them.
- A lesson changes the knowledge only after its evaluation passes and the owner approves it by name.

## 6. Use it from Codex

`python3 bin/kangaroo setup codex` prints the `codex mcp add` command with this folder's absolute paths, and the skill copy command. Then, in a Codex chat: "analyze the scorecard at export.csv".

## 7. What to send home

Counts only:
- the test summary;
- which seams are wired;
- for one real flight, each arm's grade, the number of cuts that ran and of access gaps, and the names of the checks that weren't PASS.

Send no metric values, no rows and no names from the alias map.
