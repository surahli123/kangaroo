"""Search experiment agent: M0 (setup and data trust) and M1 (why metrics moved) for one flight.

The analysis core is ds-experiment's tools (decide, explain, checks, render), used as is.
They are found at $DS_EXPERIMENT_TOOLS, else in vendor/ds_experiment_tools (the port
bundle), else in the ds-stack checkout this package sits in.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [os.environ.get("DS_EXPERIMENT_TOOLS"),
               os.path.join(HERE, "vendor", "ds_experiment_tools"),
               os.path.join(HERE, "..", "..", "skills", "ds-experiment", "tools")]
TOOLS = next((os.path.abspath(p) for p in _CANDIDATES if p and os.path.exists(os.path.join(p, "decide.py"))), None)
if TOOLS is None:
    raise ImportError("ds-experiment tools not found; set DS_EXPERIMENT_TOOLS to the folder that holds decide.py")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
LIBRARY = os.path.join(HERE, "m1", "enterprise_search_patterns.json")
CUTS = os.path.join(HERE, "m1", "cut_templates.json")
