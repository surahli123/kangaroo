---
name: kangaroo_m0_readiness
description: >-
  Use when asked whether a search experiment's setup and data can be trusted, or why a run
  says "directional only" or "not permitted". Runs the agent, reads the M0 checks from the
  trace, and says for each UNKNOWN what would fetch its data.
---

# kangaroo_m0_readiness

M0 is part of every run; there is no separate M0 command. Run the agent (AGENTS.md), then read `trace.json` and the readout's grade line.

1. For each pair, list the checks that aren't PASS, with their reasons. The reasons are written to be quoted.
2. An UNKNOWN names what would fetch its data, such as the config CLI outside the sandbox, a metric pipeline job, or a table approval. Say that, and nothing more. An UNKNOWN never blocks M1.
3. A BLOCK means the pair's movements aren't interpretable. Name the diagnosing cut that runs, and stop.
4. A WARN goes to the person who approves a decision-grade readout; the approval must name it.

Never change a check's status, never fill a check from a lesson, and never edit an input to make a check pass. The rule is in `docs/specs/stage-contract.md`.
