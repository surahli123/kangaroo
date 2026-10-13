"""How to plug the agent into a chat host. `setup` prints, for one host or all of them, the
exact config to add, with absolute paths, so it works whichever folder the session starts in.

Every host gets the same stdio MCP server (`bin/kangaroo mcp`) and the same conversation
skill (`skills/kangaroo/SKILL.md`). Nothing here writes outside this repository: the person
pastes or runs what it prints.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOSTS = ("claude", "codex", "cursor")


def server(home=None):
    """The command a host runs: this Python, the launcher, `mcp`, and the workspace."""
    entry = {"command": sys.executable, "args": [os.path.join(ROOT, "bin", "kangaroo"), "mcp"]}
    if home:
        entry["env"] = {"KANGAROO_HOME": os.path.abspath(os.path.expanduser(home))}
    return entry


def _json(entry):
    return json.dumps({"mcpServers": {"kangaroo": entry}}, indent=2)


def claude(home=None):
    e = server(home)
    env = "".join(f" -e {k}={v}" for k, v in e.get("env", {}).items())
    skill = os.path.join(ROOT, "skills", "kangaroo")
    return "\n".join([
        "## Claude Code (CLI and desktop app)",
        "",
        "Add the server for every project (user scope):",
        "",
        f"    claude mcp add --scope user kangaroo{env} -- {e['command']} {' '.join(e['args'])}",
        "",
        "Add the conversation skill:",
        "",
        f"    mkdir -p ~/.claude/skills && cp -R {skill} ~/.claude/skills/",
        "",
        "Or install this folder as a plugin, which brings both: `/plugin marketplace add "
        f"{ROOT}`, then `/plugin install kangaroo@kangaroo`.",
        "",
        "Claude desktop chat app: add this to claude_desktop_config.json (Settings, Developer, Edit Config):",
        "",
        _json(e),
    ])


def codex(home=None):
    e = server(home)
    env = "".join(f" --env {k}={v}" for k, v in e.get("env", {}).items())
    toml = "\n".join(["[mcp_servers.kangaroo]", f"command = {json.dumps(e['command'])}",
                      f"args = {json.dumps(e['args'])}"]
                     + ([f"env = {{ {', '.join(f'{k} = {json.dumps(v)}' for k, v in e['env'].items())} }}"]
                        if e.get("env") else []))
    skill = os.path.join(ROOT, "skills", "kangaroo")
    return "\n".join([
        "## Codex (CLI and desktop app share ~/.codex/config.toml)",
        "",
        "Add the server:",
        "",
        f"    codex mcp add{env} kangaroo -- {e['command']} {' '.join(e['args'])}",
        "",
        "or paste this into ~/.codex/config.toml:",
        "",
        "\n".join("    " + line for line in toml.splitlines()),
        "",
        "Add the conversation skill:",
        "",
        f"    mkdir -p ~/.agents/skills && cp -R {skill} ~/.agents/skills/",
    ])


def cursor(home=None):
    e = server(home)
    return "\n".join([
        "## Cursor",
        "",
        "Put this in ~/.cursor/mcp.json for every project, or in .cursor/mcp.json of one project, "
        "then turn the server on in Cursor Settings, MCP:",
        "",
        _json(e),
        "",
        f"Opening {ROOT} itself in Cursor needs none of this: its .cursor/mcp.json and "
        ".cursor/rules/kangaroo.mdc already point here.",
    ])


def setup(host=None, home=None):
    hosts = [host] if host else list(HOSTS)
    unknown = [h for h in hosts if h not in HOSTS]
    if unknown:
        raise ValueError(f"host must be one of {', '.join(HOSTS)}")
    parts = [{"claude": claude, "codex": codex, "cursor": cursor}[h](home) for h in hosts]
    tail = ("Then, in a chat: \"analyze the scorecard at <path>\". The workspace (runs, lessons, cards) is "
            f"{home or os.environ.get('KANGAROO_HOME') or '~/.kangaroo'}.")
    return {"text": "\n\n".join(parts + [tail])}
