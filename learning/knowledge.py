"""The knowledge version: a hash of everything that shapes an explanation. Every trace and
readout records it, so a result can always be traced to the knowledge that produced it.

Knowledge = the base library and cut templates shipped with the agent, plus the knowledge
folder (by default the overlay): patterns.json, metric_families.json, cut_templates.json and
readout_rules.json, which approved lessons write to.
"""

import hashlib
import os

from kangaroo import CUTS, LIBRARY

FILES = ("patterns.json", "metric_families.json", "metric_aliases.json", "cut_templates.json", "readout_rules.json")


def files(knowledge_dir):
    out = [LIBRARY, CUTS]
    if knowledge_dir:
        out += [os.path.join(knowledge_dir, f) for f in FILES if os.path.exists(os.path.join(knowledge_dir, f))]
    return out


def version_hash(knowledge_dir):
    h = hashlib.sha256()
    for path in files(knowledge_dir):
        h.update(os.path.basename(path).encode())
        with open(path, "rb") as handle:
            h.update(handle.read())
    return h.hexdigest()


def run_version(knowledge_dir, overlay=None):
    """Everything else that can change a result: the overlay's settings and the code (the agent,
    the learning loop and the ds-experiment tools it runs). Contracts are frozen with each case,
    so they are not part of it. Recorded beside the knowledge version in every trace."""
    import kangaroo
    h = hashlib.sha256(version_hash(knowledge_dir).encode())
    paths = [os.path.join(overlay, f) for f in ("standards.json", "warehouse.json")] if overlay else []
    for root in (os.path.dirname(kangaroo.__file__), os.path.dirname(os.path.abspath(__file__)), kangaroo.TOOLS):
        for folder, _, names in sorted(os.walk(root)):
            paths += [os.path.join(folder, n) for n in sorted(names) if n.endswith(".py")]
    for path in paths:
        if os.path.exists(path):
            h.update(os.path.basename(path).encode())
            with open(path, "rb") as handle:
                h.update(handle.read())
    return h.hexdigest()
