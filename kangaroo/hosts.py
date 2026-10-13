"""How to use the agent from a chat host. `setup` prints, for one host or all of them, what to
add, with absolute paths, so it works whichever folder the session starts in.

The supported route is the skill (`skills/kangaroo/SKILL.md`) and the command line
(`bin/kangaroo`): every host can run a shell command. The MCP server is experimental
(`experimental/mcp/`); `setup --mcp` prints its configs. Only `install_skill` writes outside
this repository, and only to the folder the person names.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOSTS = ("claude", "codex", "cursor")
SKILL = os.path.join(ROOT, "skills", "kangaroo", "SKILL.md")
LAUNCHER = os.path.join(ROOT, "bin", "kangaroo")


def install_skill(dest):
    """Copy the skill into a host's skills folder, with this repository's absolute path in its
    commands, so it works from any folder."""
    target = os.path.join(os.path.abspath(os.path.expanduser(dest)), "kangaroo")
    os.makedirs(target, exist_ok=True)
    with open(SKILL, encoding="utf-8") as handle:
        text = handle.read()
    text = text.replace("Run the commands from the kangaroo repository: `python3 bin/kangaroo <command>`.",
                        f"Run the commands with `{sys.executable} {LAUNCHER} <command>`.")
    with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as handle:
        handle.write(text)
    return {"path": target, "text": f"Skill installed at {target}. Start a new session to load it."}


def _skill_lines(host):
    me = f"{sys.executable} {LAUNCHER}"
    if host == "claude":
        return ["## Claude Code (CLI and desktop app)", "",
                f"Open {ROOT} as the project: the skill loads from .claude/skills. For every project:", "",
                f"    {me} install-skill ~/.claude/skills"]
    if host == "codex":
        return ["## Codex (CLI and desktop app)", "",
                f"Open {ROOT} as the project: AGENTS.md points Codex to the skill. For every project, add this line to "
                "~/.codex/AGENTS.md:", "",
                f"    For a search experiment, follow {SKILL}; run its commands as `{me} <command>`."]
    return ["## Cursor", "",
            f"Open {ROOT} as the project: .cursor/rules/kangaroo.mdc points Cursor to the skill. For every project, add "
            "this to Cursor Settings, Rules, User Rules:", "",
            f"    For a search experiment, follow {SKILL}; run its commands as `{me} <command>`."]


def _mcp_lines(host, home):
    entry = {"command": sys.executable, "args": [LAUNCHER, "mcp"]}
    if home:
        entry["env"] = {"KANGAROO_HOME": os.path.abspath(os.path.expanduser(home))}
    args = " ".join(entry["args"])
    if host == "claude":
        env = "".join(f" -e {k}={v}" for k, v in entry.get("env", {}).items())
        return ["## Claude Code, MCP (experimental)", "", f"    claude mcp add --scope user kangaroo{env} -- {sys.executable} {args}"]
    if host == "codex":
        env = "".join(f" --env {k}={v}" for k, v in entry.get("env", {}).items())
        return ["## Codex, MCP (experimental)", "", f"    codex mcp add{env} kangaroo -- {sys.executable} {args}"]
    return ["## Cursor, MCP (experimental): ~/.cursor/mcp.json", "",
            "\n".join("    " + line for line in json.dumps({"mcpServers": {"kangaroo": entry}}, indent=2).splitlines())]


def setup(host=None, home=None, mcp=False):
    hosts = [host] if host else list(HOSTS)
    if any(h not in HOSTS for h in hosts):
        raise ValueError(f"host must be one of {', '.join(HOSTS)}")
    parts = ["\n".join(_mcp_lines(h, home) if mcp else _skill_lines(h)) for h in hosts]
    if mcp:
        parts.append("The MCP server is experimental; see experimental/mcp/README.md.")
    else:
        parts.append(f"Then, in a chat: \"analyze the scorecard at <path>\". Runs, lessons and cards go to "
                     f"{home or os.environ.get('KANGAROO_HOME') or '~/.kangaroo'}.")
    return {"text": "\n\n".join(parts)}
