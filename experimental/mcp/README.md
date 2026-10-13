# MCP server (experimental)

The supported way to use kangaroo in a chat is the skill and the command line (`skills/kangaroo/SKILL.md`). This folder keeps the MCP route for research. Nothing here loads on its own.

## What exists

- `kangaroo/mcp_server.py`: a stdio MCP server, standard library only. It serves the same functions as the command line (`kangaroo/api.py`) as 12 tools. Start it with `python3 bin/kangaroo mcp`.
- `claude-code.mcp.json`, `cursor.mcp.json`: sample host configs. To use one, copy it to `.mcp.json` (Claude Code) or `.cursor/mcp.json` (Cursor) at the repository root.
- `python3 bin/kangaroo setup --mcp` prints the host commands with absolute paths.

## What was tested at home

| Test | Result |
|---|---|
| JSON-RPC on stdin and stdout: initialize, tools/list, tools/call, ping, a notification | Pass (the test suite) |
| Claude Code CLI 2.1: `claude mcp list` health check | Connected |
| Claude Code CLI: a three-turn chat (analyze, why, review, propose and evaluate lessons) | Pass. The host asked for approval and did not approve on its own. |
| Codex, Cursor | Not tested |

## Open questions for the work laptop

1. Does Codex run an MCP server outside its sandbox? If yes, the server can write the workspace and run the config CLI, which the shell route can't do inside the sandbox.
2. When the measurement playbook gets its own MCP server, choose one:
   - kangaroo reads the playbook as a knowledge source (metric definitions, standards). Then only the knowledge loader changes, and the playbook's version must go into the knowledge version.
   - kangaroo's tools move into the playbook server. Then register the 12 tools there, each one calling one `api.py` function, and delete `mcp_server.py`.
3. The server speaks protocol `2025-06-18` and is written by hand, because the laptop can't install the MCP SDK. Check that the host accepts that version.

All logic is in `api.py`, so removing this folder and `mcp_server.py` removes nothing else.
