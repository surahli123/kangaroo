---
name: kangaroo_m1
description: >-
  Use when asked why a search experiment's metrics moved, which deep dive to run next, or
  whether the flight is ready for triage. Runs the agent, which explains the movements, runs
  the cuts it can script, and writes the readout and a triage prep per arm. Also proposes
  lessons for the learning loop.
---

# kangaroo_m1

Run the agent (AGENTS.md). M1 runs on every pair that isn't `not_permitted`.

## What the run does

1. decide.py makes the launch call; its rule is the team's standards and the contract.
2. explain.py maps metric families and matches the enterprise patterns.
3. The agent runs each planned cut that has a template, through the fixed runner, and feeds the result back. A cut's result confirms or refutes a pattern by script.
4. Claims carry their support: confirmed (with the statement), likely, exploratory or unexplained.

## Your reply

Give the readout as written: grade, readiness, call, what moved, why, before triage, ask the engineer, similar experiments. Add one line of counts from the run: claims, confirmed, unsupported, cuts run, access gaps. Don't restate numbers in your own words. Don't present a likely cause as confirmed.

When a follow-up needs a number the run doesn't have, say which cut would produce it. If no template can, propose one as a lesson.

## Lessons

- **When to propose one.** When a reviewer marks a claim wrong or missing, a metric maps to no family, or the same movement stays unexplained across flights. Use `python3 -m learning.loop propose`, or write a lesson file by hand.
- **Kinds.** A lesson is a metric mapping, a pattern, a cut template, a readout rule or an experiment card.
- **What happens next.** Evaluation replays the frozen cases with the lesson's source flights held out. A person approves it.
- **Never** edit `metric_families.json`, `patterns.json` or `cut_templates.json` directly.
