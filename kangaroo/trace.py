"""The live trace of one run: what was read, what was tried, what the grade was after each
step, and what was claimed. It uses the investigator schema's record names (action, claim
and receipt records) so the work repo's readers keep working. Rows never enter a trace:
only statement IDs, row counts and SQL hashes.
"""

import json
import time


class Trace:
    def __init__(self, run_id, flight, knowledge_version):
        self.run_id = run_id
        self.data = {"schema": "search-agent-trace/1", "run_id": run_id, "flight": flight,
                     "knowledge_version": knowledge_version, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "steps": [], "action_records": [], "claim_records": [], "receipt_records": [], "grades": []}

    def step(self, name, **fields):
        self.data["steps"].append({"step": name, **fields})

    def receipt(self, statement, origin, source_role):
        self.data["receipt_records"].append({"status": "ok", "gap": None, "origin": origin, "source_role": source_role,
                                             **statement})

    def gap(self, origin, reason):
        self.data["receipt_records"].append({"status": "gap", "gap": reason, "origin": origin, "source_role": "deep dive",
                                             "statement_id": None, "row_count": 0})

    def grade(self, pair, graded, after):
        self.data["grades"].append({"pair": pair, "after": after, "grade": graded["grade"], "reasons": graded["reasons"],
                                    "warns": graded["warns"]})

    def action(self, record, grade_after):
        self.data["action_records"].append({**record, "grade_after": grade_after})
        self.data["grades"].append({"pair": record["pair"], "after": record["exact_next_task"], "grade": grade_after})

    def claims(self, pair, claims):
        self.data["claim_records"] += [{**c, "pair": pair, "hypothesis": c["mechanism"], "status": c["support_level"],
                                        "supporting": c["statement_ids"], "contradicting": []} for c in claims]

    def finish(self, readout_sha, counts):
        self.data.update(finished=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), readout_sha=readout_sha, counts=counts)

    def write(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=1, default=str)
