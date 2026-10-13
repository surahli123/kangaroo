# Stage contract: M0 to M1

M0 checks setup and data trust; M1 explains the movements and says whether the flight is ready for triage. This file is the only rule between them. Nothing else holds stage state: there are no packets, seals or acceptance flags.

## M0 results

Each check returns PASS, WARN, BLOCK or UNKNOWN, with a source and a reason. A PASS needs a source. A check that lacks its data is UNKNOWN and names what would fetch it; it is never a BLOCK.

| Check | Source | BLOCK when |
|---|---|---|
| config | config JSON against T1's arms | an arm changes a parameter the contract doesn't intend, or a T1 arm isn't in the config |
| sample split | the overlay's count of all exposed units, else the vendor health check | p < 0.001, or the vendor check FAILED |
| metric coverage | T1's latest snapshot | a contract metric has zero units in an arm (missing is UNKNOWN) |
| scorecard consistency | T1's latest snapshot | relative × control and absolute disagree |
| power | T1 intervals | never (WARN when a primary can't detect its bar) |
| data quality | T1 daily series | never (WARN when units fall or jump) |
| duration | config end date | never (it sets mid-flight) |
| stratification | contract | never; it doesn't affect the grade |

Per-metric and query counts never test the sample split, because the treatment can move their denominators.

## The grade

One function, per (control, test) pair, from data only:

- `not_permitted`: a BLOCK. M1 runs only the cuts that diagnose it, and the readout doesn't interpret movements.
- `directional_only`: an UNKNOWN that affects the grade, or the snapshot is before the planned end.
- `decision_grade`: everything else. The readout says "not yet approved" until a person stamps it, naming every WARN.

M1 runs on every pair that isn't `not_permitted`. The launch call (decide.py) is a separate output. While a contract metric is missing, no call is made.

## M1 findings

A cause is written as confirmed only when a cut or the scorecard confirmed its pattern, and the claim names the statement that did. Every other cause is likely, exploratory or unexplained, and the readout says which.
