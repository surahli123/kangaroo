"""Experiment configs, in one canonical shape.

Canonical config (what the rest of the agent reads):
    {"id": uuid, "name": display name, "experiment_name": the T1 key, "layer", "tags": [],
     "start": "YYYY-MM-DD", "end": "YYYY-MM-DD" or None, "target_days": int or None,
     "control_group": group id, "hypothesis": text,
     "groups": [{"id", "name", "size", "parameters": {}}],
     "health_checks": [{"name", "status": "PASSED" | "WARNING" | "FAILED"}]}

- JsonDirConfig reads canonical files (the synthetic warehouse writes them).
- CliConfig is the work config CLI. It runs only outside the sandbox; write `to_canonical`
  at work from its JSON, with a test on one sanitized example.
"""

import json
import os
import re


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


class JsonDirConfig:
    command = "a config file in the configs folder"

    def __init__(self, folder):
        self.folder = folder
        self.by_id = {}
        if folder and os.path.isdir(folder):
            for name in os.listdir(folder):
                if name.endswith(".json"):
                    with open(os.path.join(folder, name), encoding="utf-8") as handle:
                        cfg = json.load(handle)
                    self.by_id[cfg["id"]] = cfg

    def get(self, config_id):
        return self.by_id.get(config_id)

    def find(self, name):
        hits = [c for c in self.by_id.values() if _norm(c.get("experiment_name")) == _norm(name)
                or _norm(c.get("name")) == _norm(name)]
        return hits


class OneConfig:
    """A single canonical config given by hand (or none)."""

    command = "the config file, passed to analyze as config"

    def __init__(self, cfg):
        self.cfg = cfg

    def get(self, config_id):
        return self.cfg if self.cfg and self.cfg.get("id") == config_id else None

    def find(self, name):
        if self.cfg and _norm(name) in (_norm(self.cfg.get("experiment_name")), _norm(self.cfg.get("name"))):
            return [self.cfg]
        return []


class CliConfig:
    """The work config CLI. Finish at work: run it outside the sandbox and map its JSON."""

    command = "the config CLI's details command, run outside the sandbox"

    def __init__(self, run):
        self.run = run

    def get(self, config_id):
        raise NotImplementedError("CliConfig.get: call the config CLI and return to_canonical(json)")

    def find(self, name):
        return []


def _health_type(name):
    """The vendor's check name -> the type checks.py knows (HEALTH in ds-experiment's checks.py)."""
    low = str(name or "").lower()
    for key, kind in (("balanced", "has_balanced_exposures"), ("crossover", "has_crossover"), ("bias", "pre_experimental_bias_check"),
                      ("behind", "exposures_data_is_behind"), ("unit type", "has_valid_unit_type")):
        if key in low:
            return kind
    return name


def for_decide(cfg, split=None, keep=None):
    """Canonical config -> ds-experiment's config shape (checks.py and decide.py read it).

    `split` is {group name: count of all exposed units} when the overlay names that count.
    `keep` is the pair's (control, test) names: decide.py then sees only those two groups, so
    one bad arm never changes another arm's launch call. The vendor's balance check covers
    every arm, so it's dropped when the pair's own counts can test the split."""
    if not cfg:
        return None
    wanted = {str(k).lower() for k in keep} if keep else None
    groups = [{"id": g.get("id"), "name": g.get("name"), "size_pct": g.get("size"),
               "parameters": g.get("parameters") or {}, "disabled": bool(g.get("disabled"))} for g in cfg.get("groups", [])
              if not wanted or str(g.get("name")).lower() in wanted or str(g.get("id")).lower() in wanted]
    exposures = {k: v for k, v in (split or {}).items() if not wanted or str(k).lower() in wanted}
    pair_counts = bool(wanted) and len(exposures) == len(wanted)
    checks = [{"type": _health_type(h.get("name")), "name": h.get("name"), "status": h.get("status"), "metadata": {}}
              for h in cfg.get("health_checks", [])]
    if pair_counts:
        checks = [h for h in checks if h["type"] != "has_balanced_exposures"]
    return {"experiment_id": cfg.get("id"), "name": cfg.get("name"), "hypothesis": cfg.get("hypothesis"),
            "groups": groups, "control_group": cfg.get("control_group"), "start": cfg.get("start"), "end": cfg.get("end"),
            "target_days": cfg.get("target_days"), "status": cfg.get("status"), "layer_id": cfg.get("layer"),
            "health_checks": checks, "settings": {}, "edits": [], "exposures": exposures}
