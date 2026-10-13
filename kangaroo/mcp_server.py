#!/usr/bin/env python3
"""Experimental: the agent as a stdio MCP server. The supported chat route is the skill and the
command line; this stays for research (see experimental/mcp/README.md). Standard library only.

    python3 -m kangaroo.mcp_server          (the host starts it; it speaks JSON-RPC on stdio)

Tools: analyze, report, why, review, propose_lessons, list_lessons, evaluate_lesson,
approve_lesson, approve_report, list_cards, confirm_card, list_runs. They all read and write
the workspace (KANGAROO_HOME, default ~/.kangaroo) and never write to a warehouse. The two
approval tools and confirm_card change what the agent knows: call them only when the person
has said, in their own words, that they approve, and pass their name.
"""

import json
import sys
import traceback

from kangaroo import api

PROTOCOL = "2025-06-18"
INSTRUCTIONS = (
    "Search experiment analysis. Start with `analyze` (a scorecard file, or an experiment in the warehouse); reply with "
    "the summary it returns, as written. Use `why` for a metric, `report` for the tables, "
    "`review` when the person says a claim is right or wrong. Lessons: `propose_lessons`, then `evaluate_lesson`; "
    "`approve_lesson`, `approve_report` and `confirm_card` only when the person explicitly approves, with their name. "
    "Never restate numbers in your own words and never present a likely cause as confirmed.")

S = lambda **props: {"type": "object", "properties": props, "additionalProperties": False}  # noqa: E731
STR = {"type": "string"}
RUN = {"type": "string", "description": "a run folder, an experiment name (its latest run), or 'latest'"}
TOOLS = [
    ("analyze", api.analyze, "Analyze one experiment. Give a scorecard file (CSV or JSON rows), with its contract and "
     "config if known, or an experiment name and a warehouse. Returns the summary and the report's path.",
     S(experiment=STR, scorecard=STR, contract=STR, config=STR, warehouse=STR, configs=STR)),
    ("report", api.report, "The latest (or a named) run's report: part = summary, details or all.",
     S(run=RUN, part={"type": "string", "enum": ["summary", "details", "all"]})),
    ("why", api.why, "What the run found about one metric: its change, the best explanation and its support, and the "
     "checks that ran.", {**S(metric=STR, run=RUN), "required": ["metric"]}),
    ("review", api.review, "Record the person's verdict on a claim: correct, wrong or missing, with their note.",
     {**S(verdict={"type": "string", "enum": ["correct", "wrong", "missing"]}, claim=STR, metric=STR, note=STR,
          reviewer=STR, run=RUN), "required": ["verdict"]}),
    ("propose_lessons", api.propose, "Propose candidate lessons from reviews and past runs.", S()),
    ("list_lessons", api.list_lessons, "List candidate lessons, their evaluation status and the knowledge version.", S()),
    ("evaluate_lesson", api.evaluate_lesson, "Replay the frozen past runs with and without a lesson, its source flights "
     "held out; report improvements and regressions.", {**S(lesson_id=STR, warehouse=STR), "required": ["lesson_id"]}),
    ("approve_lesson", api.approve_lesson, "Apply a lesson whose evaluation passed. Only with the person's explicit "
     "approval in their own words; pass their name.", {**S(lesson_id=STR, approver=STR), "required": ["lesson_id", "approver"]}),
    ("approve_report", api.approve_report, "Mark a decision-grade report approved. Only with the person's explicit "
     "approval; pass their name and every warning they acknowledge.",
     {**S(approver=STR, warns={"type": "array", "items": STR}, run=RUN), "required": ["approver"]}),
    ("list_cards", api.list_cards, "List experiment cards, drafted and confirmed.", S()),
    ("confirm_card", api.confirm_card, "Confirm a past experiment's card with its cause and lesson, as the person "
     "states them; pass their name.", {**S(experiment=STR, cause=STR, lesson=STR, approver=STR),
                                        "required": ["experiment", "cause", "lesson", "approver"]}),
    ("list_runs", api.list_runs, "List the recent runs.", S()),
]
BY_NAME = {name: fn for name, fn, _, _ in TOOLS}
SCHEMAS = {name: schema for name, _, _, schema in TOOLS}


def _check_args(name, args):
    """Plain messages for a missing or unknown argument, before the call."""
    schema = SCHEMAS[name]
    unknown = sorted(set(args) - set(schema["properties"]))
    missing = [k for k in schema.get("required", []) if args.get(k) in (None, "")]
    if unknown:
        return f"{name} takes {', '.join(schema['properties']) or 'no arguments'}; not {', '.join(unknown)}"
    if missing:
        return f"{name} needs {', '.join(missing)}" + (
            " (the approving person's name, given in their own words)" if "approver" in missing else "")
    return None


def handle(msg):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}},
                "serverInfo": {"name": "kangaroo", "version": "0.1.0"}, "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": schema} for n, _, d, schema in TOOLS]}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in BY_NAME:
            raise LookupError(f"unknown tool {name}")
        problem = _check_args(name, args)
        if problem:
            return {"content": [{"type": "text", "text": problem}], "isError": True}
        try:
            result = BY_NAME[name](**args)
            return {"content": [{"type": "text", "text": result["text"]}], "isError": False}
        except (ValueError, FileNotFoundError, KeyError, TypeError) as err:
            return {"content": [{"type": "text", "text": f"{name}: {err}"}], "isError": True}
    raise LookupError(f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in msg:
            continue  # a notification (initialized, cancelled): nothing to answer
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
        except LookupError as err:
            code = -32602 if str(err).startswith("unknown tool") else -32601
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": code, "message": str(err)}}
        except Exception as err:  # noqa: BLE001  (never crash the session)
            traceback.print_exc(file=sys.stderr)
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(err)}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
