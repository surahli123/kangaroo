"""Resolve what the person typed (a config UUID, a console URL or an experiment name) to the
pair every later step needs: the config (or None, with the command that would fetch it) and
the experiment name that keys T1."""

import re

from kangaroo.sources import t1

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class Unresolved(ValueError):
    pass


def resolve(text, configs, warehouse, table, cols):
    notes = []
    match = UUID.search(text or "")
    if match:
        cfg = configs.get(match.group(0).lower()) or configs.get(match.group(0))
        if not cfg:
            raise Unresolved(f"no config for {match.group(0)}; fetch it with {configs.command}, or pass the experiment name")
        name, how = cfg.get("experiment_name"), "config id"
    else:
        name, cfg, how = text.strip(), None, "experiment name"
        found = configs.find(name)
        if len(found) > 1:
            raise Unresolved(f"'{name}' matches {len(found)} configs; pass the config id")
        if found:
            cfg = found[0]
            name = cfg.get("experiment_name") or name
        else:
            notes.append(f"no config found for '{name}'; the config checks need {configs.command}")
    result = warehouse.query(t1.names_sql(table, cols), {"name": name}, label="resolve experiment name in T1")
    if not result["rows"]:
        raise Unresolved(f"T1 has no rows for experiment '{name}'")
    return {"experiment_name": name, "config": cfg, "config_id": (cfg or {}).get("id"), "how": how, "notes": notes,
            "statement": {k: result[k] for k in ("statement_id", "label", "row_count", "sql_sha")}}
