"""The agent end to end on the synthetic warehouse, with unittest only (the work laptop has
no pytest). Run from the repository root:

    python3 -m unittest discover -s tests

Three doors, one class each: a flight through the run (Flights), the learning loop on frozen
cases (Learning), and a person's session through the API, the command line and the MCP
server (Session). Expensive fixtures are built once: one warehouse, one run per flight, one
set of frozen cases. Assertions read structured outputs (counts, the trace, return values,
files), never the report's prose, which is being rewritten.
"""

import contextlib
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "synth"))

import make_warehouse  # noqa: E402

from learning import cards, evaluate, knowledge, lessons  # noqa: E402
from kangaroo import LIBRARY, api  # noqa: E402
from kangaroo import run as runner  # noqa: E402
from kangaroo.__main__ import main as cli  # noqa: E402
from kangaroo.m0 import checks as m0  # noqa: E402
from kangaroo.m1 import cuts, method  # noqa: E402
from kangaroo.render import readout as rd  # noqa: E402
from kangaroo.render import ste  # noqa: E402
from kangaroo.sources import config as cfgsrc  # noqa: E402
from kangaroo.sources.warehouse import QueryRefused, SqliteWarehouse, bind, guard  # noqa: E402

FLIGHTS = ["synth-3p-boost-mid", "synth-typeahead-regress", "synth-auth-requery", "synth-answer-shift", "synth-null",
           "synth-split-loss", "synth-3p-boost-v2", "synth-3p-pin", "synth-split-one-arm", "synth-renamed-primary",
           "synth-renamed-primary-b"]
CASES = ("synth-3p-boost-v2", "synth-3p-pin", "synth-null", "synth-typeahead-regress", "synth-auth-requery",
         "synth-renamed-primary", "synth-renamed-primary-b")
STATE = {}


def setUpModule():
    tmp = tempfile.mkdtemp(prefix="agent-tests-")
    synth = os.path.join(tmp, "synth")
    make_warehouse.main(["--out", synth, "--flights", *FLIGHTS])
    overlay = os.path.join(tmp, "overlay")
    shutil.copytree(os.path.join(ROOT, "overlay.example"), overlay, ignore=shutil.ignore_patterns("contracts"))
    shutil.copytree(os.path.join(synth, "contracts"), os.path.join(overlay, "contracts"))
    STATE.update(tmp=tmp, synth=synth, overlay=overlay, gold=load(os.path.join(synth, "gold.json")),
                 db=os.path.join(synth, "warehouse.sqlite"), configs=os.path.join(synth, "configs"))


def tearDownModule():
    shutil.rmtree(STATE["tmp"], ignore_errors=True)


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle)


def run_flight(name, overlay=None, knowledge_dir=None, cards_dir=None):
    out = tempfile.mkdtemp(dir=STATE["tmp"])
    return runner.run(name, overlay or STATE["overlay"], SqliteWarehouse(STATE["db"]), cfgsrc.JsonDirConfig(STATE["configs"]),
                      out, knowledge_dir, cards_dir), out


def flights():
    """Every flight through the run, once, sharing one cards folder (so later runs see earlier cards)."""
    if "runs" not in STATE:
        cards_dir = os.path.join(STATE["tmp"], "cards")
        STATE["runs"] = {name: run_flight(name, cards_dir=cards_dir) for name in FLIGHTS}
    return STATE["runs"]


def t1_rows(name):
    """What a person exports: every T1 row of the experiment, all days."""
    wh = SqliteWarehouse(STATE["db"])
    rows = wh.query("SELECT * FROM t1_scorecard WHERE experiment_name = :e ORDER BY ds, metric_name", {"e": name})["rows"]
    wh.close()
    return rows


def config_path(name):
    return next(os.path.join(STATE["configs"], f) for f in sorted(os.listdir(STATE["configs"]))
                if load(os.path.join(STATE["configs"], f)).get("experiment_name") == name)


def contract_path(name):
    return os.path.join(STATE["overlay"], "contracts", f"{name}.json")


FAKE_DATABRICKS = """import json, sqlite3, sys
def run(db, sql):
    cur = sqlite3.connect(db).execute("\\n".join(l for l in sql.splitlines() if not l.startswith("--")))
    return {"statement_id": "dbx-fake", "manifest": {"schema": {"columns": [{"name": c[0]} for c in cur.description]}},
            "result": {"data_array": [[None if v is None else str(v) for v in r] for r in cur.fetchall()]}}
if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1], sys.stdin.read())))
"""  # stands in for the Databricks skill script: SQL on stdin, a statement-API result (string values) on stdout


def keys_in(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from keys_in(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from keys_in(v)


class Flights(unittest.TestCase):
    """Each synthetic flight gets its planted grade and mechanism, every finding cites its data, and the
    grade comes from the checks alone."""

    @classmethod
    def setUpClass(cls):
        cls.runs = flights()

    def pair(self, name, arm=None):
        pairs = {p["test"]: p for p in self.runs[name][0]["pairs"]}
        return pairs[arm] if arm else next(iter(pairs.values()))

    def test_grades_and_mechanisms_match_the_planted_ones(self):
        for name, (result, _) in self.runs.items():
            gold = STATE["gold"][name]
            o = evaluate.outcome(result)
            with self.subTest(flight=name):
                if name.startswith("synth-renamed-primary"):  # the contract's primary is missing until an alias is approved
                    self.assertEqual({p["grade"] for p in o.values()}, {"directional_only"})
                elif gold.get("grades"):
                    self.assertEqual({t: p["grade"] for t, p in o.items()}, gold["grades"])
                else:
                    self.assertEqual({p["grade"] for p in o.values()}, {gold["grade"]})
                # two flights name their placement metric differently: the mechanism is hidden until a lesson maps it
                # (Learning proves the lesson lifts synth-3p-pin)
                self.assertEqual(evaluate.gold_score(o, gold), 0 if name in ("synth-3p-boost-v2", "synth-3p-pin") else 2)

    def test_the_grade_comes_from_the_checks_alone(self):
        """A BLOCK is not permitted and runs no deep dive; an UNKNOWN that affects the grade, or a snapshot before the
        planned end, is directional only and M1 still runs; anything else is decision grade, whatever the WARNs."""
        blocked = self.pair("synth-split-loss")
        self.assertEqual((blocked["grade"]["grade"], blocked["grade"]["blocks"]), ("not_permitted", ["M0-2"]))
        self.assertEqual(self.runs["synth-split-loss"][0]["counts"]["cuts_ran"], 0)
        no_config = self.pair("synth-answer-shift")  # the config can't be fetched: the check is UNKNOWN, not a BLOCK
        self.assertEqual((no_config["grade"]["grade"], no_config["grade"]["blocks"]), ("directional_only", []))
        self.assertIn("M0-1", no_config["grade"]["unknowns"])
        self.assertGreater(sum(a["status"] == "ran" for a in no_config["actions"]), 0)  # M1 ran its cuts anyway
        mid = self.pair("synth-3p-boost-mid")
        duration = next(c["details"] for c in mid["m0"] if c["id"] == "duration")
        self.assertLess(str(duration["as_of"]), str(duration["end"]))
        self.assertEqual(mid["grade"]["grade"], "directional_only")
        pin = self.pair("synth-3p-pin")
        self.assertEqual((pin["grade"]["grade"], pin["grade"]["blocks"], pin["grade"]["unknowns"]), ("decision_grade", [], []))
        self.assertTrue(pin["grade"]["warns"])  # a WARN never lowers the grade
        self.assertEqual([c["status"] for c in pin["m0"] if c["id"] == "stratification"], ["UNKNOWN"])  # doesn't affect it
        for c in (c for p in self.runs.values() for q in p[0]["pairs"] for c in q["m0"]):
            self.assertTrue(c["status"] != "PASS" or c["source"], c)  # a PASS always names its source

    def test_one_bad_arm_never_decides_a_clean_arm(self):
        result, _ = self.runs["synth-split-one-arm"]
        good, bad = self.pair("synth-split-one-arm", "good"), self.pair("synth-split-one-arm", "bad")
        self.assertEqual((good["grade"]["grade"], bad["grade"]["grade"]), ("decision_grade", "not_permitted"))
        self.assertNotEqual(good["decision"]["call"], "cant_tell")  # the 3-arm split used to decide it
        self.assertEqual([g["name"] for g in good["config_for_decide"]["groups"]], ["control", "good"])
        self.assertEqual([a for a in bad["actions"] if a["status"] == "ran" and a["kind"] == "cut"], [])
        self.assertEqual(result["counts"]["grades"], {"good": "decision_grade", "bad": "not_permitted"})

    def test_a_cause_is_confirmed_only_by_its_cut_and_its_control_cut(self):
        import explain
        for name, (result, _) in self.runs.items():
            statements = {r["statement_id"] for r in result["trace"]["receipt_records"] if r["statement_id"]}
            for c in result["trace"]["claim_records"]:
                if c["support_level"] == "confirmed":
                    with self.subTest(flight=name, claim=c["claim_id"]):
                        self.assertTrue(c["statement_ids"])
                        self.assertTrue(set(c["statement_ids"]) <= statements)
            self.assertEqual(result["counts"]["unsupported"], 0, name)
        p = self.pair("synth-auth-requery")
        confirmed = [c for c in p["claims"] if c["support_level"] == "confirmed"]
        self.assertEqual({c["candidate_id"] for c in confirmed}, {"authorized_struggle"})
        self.assertTrue({"first_party_displaced", "typeahead_funnel"} <= {i["id"] for i in p["explained"]["ruled_out"]})
        ran = [a for a in p["actions"] if a["status"] == "ran" and a.get("target_pattern") == "authorized_struggle"]
        self.assertEqual({a["role"] for a in ran}, {"where", "control"})
        # The audit re-derives support from what ran: take away the control cut, or caveat the cut where the
        # mechanism acts, and the same claims are no longer backed.
        library = explain.load_library(None, base=LIBRARY)
        self.assertEqual(method.unsupported(confirmed, p["actions"], library), [])
        without_control = [a for a in p["actions"] if a.get("role") != "control"]
        self.assertEqual(len(method.unsupported(confirmed, without_control, library)), len(confirmed))
        caveated = [{**a, "caveat": "the share moved"} if a.get("role") != "control" else a for a in p["actions"]]
        self.assertEqual(len(method.unsupported(confirmed, caveated, library)), len(confirmed))

    def test_a_run_leaves_its_files_and_keeps_rows_out_of_the_trace(self):
        for name, (result, out) in self.runs.items():
            with self.subTest(flight=name):
                for f in ("readout.md", "readout.html", "trace.json", "review.json", "card_draft.json", "case/scorecard.json",
                          "case/series.json", "case/contract.json", "case/queries.json", "case/meta.json"):
                    self.assertTrue(os.path.exists(os.path.join(out, f)), f)
                trace = load(os.path.join(out, "trace.json"))
                self.assertNotIn("rows", set(keys_in(trace)))  # only statement IDs, counts and hashes leave a run
                self.assertEqual(trace["counts"], result["counts"])
                self.assertTrue(trace["action_records"] and all(a.get("grade_after") for a in trace["action_records"]))
                self.assertEqual({g["pair"] for g in trace["grades"] if g["after"] == "M0"}, set(result["counts"]["grades"]))
                # the writing rules (render/ste.py) hold on every report; team names are not checked
                self.assertEqual(ste.check(result["readout"], rd.team_names(result["pairs"])), [])
        self.assertIn("## Summary", self.runs["synth-null"][0]["readout"])  # the one check on the report's shape
        card = load(os.path.join(self.runs["synth-null"][1], "card_draft.json"))  # a null flight is not like everything
        self.assertEqual(set(card["families_moved"]) & {"success", "success_weighted", "p3_clicks_auth", "answer_success"}, set())
        self.assertFalse(any(m["change"].startswith("didn't move") for m in card["what_moved"]))

    def test_the_warehouse_only_reads_and_a_cut_ties_to_t1(self):
        """The guard refuses every write; a template with T1's definition gives T1's number; the work dialect
        (Spark, which the home can't run) gets a finished, quoted, read-only statement; both work routes to the
        warehouse (a command, a hand-off) give the direct run's cuts, and a changed number is refused."""
        wh = SqliteWarehouse(STATE["db"])
        for bad in ("DELETE FROM t1_scorecard", "SELECT 1; DROP TABLE t1_scorecard", "INSERT INTO t1_scorecard VALUES (1)",
                    "PRAGMA table_info(t1_scorecard)"):
            with self.assertRaises(QueryRefused):
                wh.query(bad)
        guard("WITH a AS (SELECT 'drop' AS x) SELECT x FROM a")  # a word inside a literal isn't a write
        wh_cfg = load(os.path.join(STATE["overlay"], "warehouse.json"))
        tpl = {"same_as_t1": {"produces": "1P long clicks per full-page query", "filter": "t.experience = 'surface_full'",
                              "x": "t.long_click_1p", "y": "1"}}
        as_of = wh.query("SELECT MAX(ds) AS d FROM t1_scorecard WHERE experiment_name = :e", {"e": "synth-null"})["rows"][0]["d"]
        res = cuts.run_cut(wh, "same_as_t1", tpl, {**wh_cfg, "cohort": None}, "synth-null", as_of, "control", ["test"], min_units=1)
        t1 = wh.query("SELECT relative_delta FROM t1_scorecard WHERE experiment_name = :e AND ds = :d AND "
                      "metric_name = '1P Long Click Rate'", {"e": "synth-null", "d": as_of})["rows"][0]["relative_delta"]
        self.assertAlmostEqual(100 * res["metrics"][0]["rel_delta"], t1, places=6)
        # Work differences stay in the overlay: a template's value maps to the warehouse's value, a column that the
        # warehouse lacks makes the cut an access gap (and the dry run skips it), and a unit in two arms counts once.
        cut = lambda templates, cfg: cuts.run_cut(wh, "same_as_t1", templates, {**wh_cfg, "cohort": None, **cfg},  # noqa: E731
                                                  "synth-null", as_of, "control", ["test"], min_units=1)["metrics"][0]
        renamed = {"same_as_t1": {**tpl["same_as_t1"], "filter": "t.experience = 'work_value'"}}
        with self.assertRaisesRegex(cuts.AccessGap, "no column for long_click_1p"):
            cut(tpl, {"t3_columns": {"long_click_1p": None}})
        twins = ("(SELECT unit_id, experiment_name, group_name, first_exposure_date FROM exposures UNION ALL "
                 "SELECT unit_id, experiment_name, CASE WHEN group_name = 'control' THEN 'test' ELSE 'control' END, "
                 "DATE(first_exposure_date, '+1 day') FROM exposures WHERE unit_id LIKE '%7')")
        crossed = {"tables": {**wh_cfg["tables"], "assignment": twins}, "t3_values": {"work_value": "surface_full"}}
        first = cut(renamed, crossed)
        self.assertAlmostEqual(100 * first["rel_delta"], t1, places=6)  # each unit in the arm of its first exposure
        left_out = cut(renamed, {**crossed, "assignment": {**wh_cfg["assignment"], "multi_arm": "exclude"}})
        self.assertLess(left_out["control_units"], first["control_units"])
        dry_home = tempfile.mkdtemp(dir=STATE["tmp"])
        api.init(dry_home)
        overlay_file = os.path.join(dry_home, "overlay", "warehouse.json")
        save(overlay_file, {**load(overlay_file), "t3_columns": {"typeahead_suggested": None}})
        dry = api.dry_run(os.path.join(dry_home, "dry"), path=dry_home)
        self.assertEqual(sorted(dry["skipped"]), ["nav_share", "ta_known_item_unauth"])
        self.assertEqual(len(dry["files"]) + len(dry["skipped"]), len(cuts.load_templates()))
        sql = cuts.dry_run_sql(cuts.load_templates()["primary_by_exposure_day"], wh_cfg, "spark")
        bound, params = bind(sql, {"experiment": "it's", "as_of": "2026-01-14"}, "spark")
        self.assertIsNone(params)
        self.assertIn("= 'it''s'", bound)
        self.assertIn("DATEDIFF(", bound)
        self.assertTrue(bound.endswith("LIMIT 0"))
        guard(bound)
        self.assertEqual(bind("a = :a AND b IN :b AND c = :c AND d = :d", {"a": 1.5, "b": ["x", 2], "c": None, "d": True}, "spark"),
                         ("a = 1.5 AND b IN ('x', 2) AND c = NULL AND d = TRUE", None))
        # The two work routes give the direct run's cuts: a command (the Databricks skill's script) and a hand-off
        # (the Databricks MCP, which only the chat agent can call). A copied result with a changed number is refused.
        name, direct = "synth-auth-requery", self.runs["synth-auth-requery"][0]["counts"]
        fake = os.path.join(STATE["tmp"], "fake_databricks.py")
        with open(fake, "w", encoding="utf-8") as handle:
            handle.write(FAKE_DATABRICKS)
        sys.path.insert(0, STATE["tmp"])
        import fake_databricks  # noqa: E402

        def session(warehouse):
            home = tempfile.mkdtemp(dir=STATE["tmp"])
            api.init(home)
            cfg = os.path.join(home, "overlay", "warehouse.json")
            save(cfg, {**load(cfg), "dialect": "sqlite"})  # the fake runs SQLite; the laptop keeps "spark"
            scorecard = os.path.join(home, "scorecard.json")
            save(scorecard, t1_rows(name))
            return lambda: api.analyze(scorecard=scorecard, contract=contract_path(name), config=config_path(name),
                                       warehouse=warehouse, path=home)
        by_command = session(f"cmd:{sys.executable} {fake} {STATE['db']}")()
        self.assertEqual((by_command["counts"]["grades"], by_command["counts"]["cuts_ran"]), (direct["grades"], direct["cuts_ran"]))
        folder = tempfile.mkdtemp(dir=STATE["tmp"])
        handoff, rounds = session(f"handoff:{folder}"), 0
        out = handoff()
        while out["pending"] and rounds < 6:  # the agent runs each pending statement and saves the tool output unchanged
            rounds += 1
            for p in out["pending"]:
                save(os.path.join(folder, "results", os.path.basename(p)[:-4] + ".json"),
                     fake_databricks.run(STATE["db"], open(p, encoding="utf-8").read()))
            out = handoff()
        self.assertEqual((out["counts"]["cuts_ran"], out["pending"]), (direct["cuts_ran"], []))
        self.assertGreater(rounds, 1)  # a control cut is asked for only after its cut agrees
        result = sorted(os.listdir(os.path.join(folder, "results")))[0]
        doc = load(os.path.join(folder, "results", result))
        doc["result"]["data_array"][0][2] = str(float(doc["result"]["data_array"][0][2]) + 1)
        save(os.path.join(folder, "results", result), doc)
        tampered = handoff()
        gaps = [a["source_or_access_gap"] for a in load(os.path.join(tampered["run_dir"], "trace.json"))["action_records"]
                if a["status"] == "access gap"]
        self.assertTrue(any("checksum" in g for g in gaps), gaps)


class Learning(unittest.TestCase):
    """The loop on frozen cases: a lesson changes the knowledge only after it passes with its source flights
    held out and a named person approves it; the gate refuses everything else."""

    @classmethod
    def setUpClass(cls):
        cls.cases = os.path.join(STATE["tmp"], "cases")
        for name in CASES:
            evaluate.freeze(flights()[name][1], cls.cases, STATE["gold"][name])

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=STATE["tmp"])
        self.k = os.path.join(self.dir, "knowledge")
        self.lessons = os.path.join(self.dir, "lessons")
        os.makedirs(self.k)

    def evaluate(self, lesson, live=False):
        return evaluate.evaluate(lesson, self.cases, STATE["overlay"], self.k, SqliteWarehouse(STATE["db"]) if live else None)

    def test_a_mapping_lesson_generalises_held_out_and_needs_a_named_approver(self):
        import explain
        library = explain.load_library(self.k, base=LIBRARY)
        scorecard = load(os.path.join(self.cases, "synth-3p-boost-v2", "scorecard.json"))
        proposed = lessons.propose_mappings(scorecard, library, "synth-3p-boost-v2")
        self.assertEqual([p["change"] for p in proposed], [{"family": "p3_placement", "metric": "3P Top-1 Placement Rate"}])
        report = self.evaluate(proposed[0], live=True)
        self.assertTrue(report["pass"], report["status"])
        self.assertEqual((report["improved"], report["regressions"]), (["synth-3p-pin"], []))
        self.assertEqual([c["case"] for c in report["fit_cases"]], ["synth-3p-boost-v2"])  # the source flight never counts
        before = knowledge.version_hash(self.k)
        with self.assertRaises(ValueError):
            evaluate.approve(proposed[0], report, " ", self.k, self.lessons)
        self.assertEqual(knowledge.version_hash(self.k), before)
        after = evaluate.approve(proposed[0], report, "owner", self.k, self.lessons)
        self.assertNotEqual(before, after)
        self.assertTrue(os.path.exists(os.path.join(self.lessons, "approved", f"{proposed[0]['id']}.json")))
        with self.assertRaises(ValueError):  # the knowledge moved on: the report no longer applies
            evaluate.approve(proposed[0], report, "owner", self.k, self.lessons)

    def test_an_approved_alias_restores_a_renamed_primary_in_the_next_run(self):
        case = os.path.join(self.cases, "synth-renamed-primary")
        proposed = lessons.propose_aliases(load(os.path.join(case, "scorecard.json")), load(os.path.join(case, "contract.json")),
                                           "synth-renamed-primary")
        self.assertEqual([p["change"] for p in proposed],
                         [{"scorecard_name": "Search Success Rate v2", "contract_name": "Search Success Rate"}])
        report = self.evaluate(proposed[0])
        self.assertTrue(report["pass"], report["status"])
        self.assertEqual(report["improved"], ["synth-renamed-primary-b"])
        evaluate.approve(proposed[0], report, "owner", self.k, self.lessons, self.cases, STATE["overlay"])
        result, _ = run_flight("synth-renamed-primary-b", knowledge_dir=self.k)
        self.assertEqual(result["counts"]["grades"], {"pin": "decision_grade"})
        self.assertEqual(evaluate.gold_score(evaluate.outcome(result), STATE["gold"]["synth-renamed-primary-b"]), 2)

    def test_the_gate_refuses_a_harmful_an_edited_a_vacuous_and_an_unreplayable_lesson(self):
        harmful = lessons.new("metric_mapping", {"family": "latency", "metric": "Typeahead CTR"}, ["synth-3p-boost-v2"], "test")
        report = self.evaluate(harmful)
        with self.subTest(lesson="harmful: a held-out case changed for the worse"):
            self.assertFalse(report["pass"])
            self.assertIn("synth-typeahead-regress", report["regressions"])
            with self.assertRaises(ValueError):
                evaluate.approve(harmful, report, "owner", self.k, self.lessons)
        with self.subTest(lesson="edited report: approval re-runs the evaluation"):
            forged = {**report, "pass": True, "regressions": [], "improved": ["synth-3p-pin"]}
            with self.assertRaises(ValueError):
                evaluate.approve(harmful, forged, "owner", self.k, self.lessons, self.cases, STATE["overlay"])
            self.assertFalse(os.path.exists(os.path.join(self.k, "metric_families.json")))
        with self.subTest(lesson="vacuous pattern that names every case as its target"):
            anything = {f: "any" for f in ("p1_long_click", "p1_binary_ctr", "success", "success_weighted", "typeahead_ctr",
                                           "answer_shown", "latency", "authorized_share", "fp_queries_per_user")}
            pattern = {"id": "more_3p_changes_things", "name": "More 3P clicks change many things", "when": {"p3_clicks": "up"},
                       "any_of": anything, "mechanism": "Other metrics move too.", "deep_dives": [], "metrics": []}
            vacuous = lessons.new("pattern", {"pattern": pattern}, ["synth-3p-boost-v2"], "test", target_cases=sorted(os.listdir(self.cases)))
            report = self.evaluate(vacuous)
            self.assertFalse(report["pass"], report["status"])
            self.assertTrue(report["regressions"])
        refused = {
            "no source flight": lessons.new("metric_mapping", {"family": "p3_placement", "metric": "3P Top-1 Placement Rate"}, [], "test"),
            "probing cut template": lessons.new("cut_template", {"id": "probe", "template": {
                "produces": "x", "filter": "1 = 1", "x": "CASE WHEN t.user_id = 'u1' THEN 1e6 ELSE 0 END", "y": "1"}},
                ["synth-null"], "test"),
            "pattern stub": lessons.propose_patterns([{"flight": f, "counts": {}, "claim_records": [
                {"metric": "X", "support_level": "none", "change": "rose 1%", "pair": "t"}]} for f in "abc"])[0],
            "readout rule (model-facing)": lessons.new("readout_rule", {"rule": "say the arm first"}, ["synth-null"], "test"),
        }
        for why, lesson in refused.items():
            with self.subTest(lesson=why):
                report = self.evaluate(lesson)
                self.assertFalse(report["pass"])
                self.assertNotIn("cases", report)  # refused at the gate, before any replay


class Session(unittest.TestCase):
    """What a person reaches: the API behind the chat tools, the command line, and the stdio MCP server."""

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(dir=STATE["tmp"])
        cls.files = tempfile.mkdtemp(dir=STATE["tmp"])
        cls.scorecard = os.path.join(cls.files, "scorecard.csv")  # the export a person attaches
        rows = t1_rows("synth-3p-boost-mid")
        with open(cls.scorecard, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def analyze(self, name, rows, inputs=True):
        """One scorecard variant in a workspace of its own; inputs=False gives no contract and no config."""
        home = tempfile.mkdtemp(dir=STATE["tmp"])
        path = os.path.join(home, f"{name}.json")
        save(path, rows)
        if not inputs:
            return api.analyze(scorecard=path, path=home)
        return api.analyze(scorecard=path, contract=contract_path(name), config=config_path(name), path=home)

    def test_a_person_analyzes_asks_why_reviews_and_proposes(self):
        out = api.analyze(scorecard=self.scorecard, contract=contract_path("synth-3p-boost-mid"), config=config_path("synth-3p-boost-mid"),
                          path=self.home)
        self.assertEqual(out["counts"]["grades"], {"boost_15": "directional_only"})
        self.assertEqual(out["counts"]["cuts_ran"], 0)  # a scorecard alone has no query-level table
        self.assertGreater(out["counts"]["access_gaps"], 0)
        self.assertGreater(out["counts"]["claims"], 0)  # M1 still ran
        self.assertTrue(out["text"] and os.path.exists(out["html"]))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "cases", "synth-3p-boost-mid")))  # every run is frozen
        self.assertIn("1P Long Click Rate", api.why("1P Long Click Rate", path=self.home)["text"])
        self.assertEqual(api.why("nothing like it", path=self.home)["run_dir"], out["run_dir"])
        api.review("wrong", metric="1P Long Click Rate", note="it is displacement", reviewer="owner", path=self.home)
        api.review("missing", note="say the arm first", reviewer="owner", path=self.home)
        with self.assertRaises(ValueError):
            api.review("maybe", metric="1P Long Click Rate", path=self.home)
        proposed = api.propose(path=self.home)
        rules = [i for i in proposed["lessons"] if i.startswith("readout_rule-")]
        self.assertEqual(len(rules), 2)
        report = api.evaluate_lesson(rules[0], path=self.home)["report"]
        self.assertFalse(report["pass"])
        self.assertNotIn("cases", report)  # a readout rule needs a model-facing evaluation, not a replay
        for approver in ("", "owner"):  # no name, and a lesson that didn't pass
            with self.assertRaises(ValueError):
                api.approve_lesson(rules[0], approver, path=self.home)
        self.assertTrue(os.path.exists(os.path.join(self.home, "lessons", "candidate", f"{rules[0]}.json")))
        self.assertEqual(api.report(part="details", path=self.home)["run_dir"], out["run_dir"])
        # Cards: a draft is related, never cited; a person's confirmation makes it citable and a later draft can't undo it.
        folder = os.path.join(self.home, "cards")
        card = cards.load(folder)[0]
        self.assertEqual(card["experiment_name"], "synth-3p-boost-mid")
        like = lambda who: cards.similar(cards.load(folder), who, card["layer"], card["tags"], card["families_moved"])  # noqa: E731
        self.assertEqual([[c["experiment_name"] for c in group] for group in like("another")], [[], ["synth-3p-boost-mid"]])
        with self.assertRaises(ValueError):
            api.confirm_card("synth-3p-boost-mid", "cause", "lesson", "", path=self.home)
        api.confirm_card("synth-3p-boost-mid", "cause", "lesson", "owner", path=self.home)
        api.analyze(scorecard=self.scorecard, contract=contract_path("synth-3p-boost-mid"), config=config_path("synth-3p-boost-mid"),
                    path=self.home)
        self.assertEqual([c["status"] for c in cards.load(folder)], ["confirmed"])
        self.assertEqual([[c["experiment_name"] for c in group] for group in like("another")], [["synth-3p-boost-mid"], []])
        self.assertEqual(like("synth-3p-boost-mid"), ([], []))  # never similar to itself
        # The warehouse door: a console URL resolves through the config id, and the cuts run.
        uuid = STATE["gold"]["synth-null"]["config_id"]
        out = api.analyze(experiment=f"https://console.example/experiments/{uuid}/results", warehouse=f"sqlite:{STATE['db']}",
                          configs=STATE["configs"], path=self.home)
        self.assertEqual(out["counts"]["grades"], {"test": "decision_grade"})
        self.assertGreater(out["counts"]["cuts_ran"], 0)
        with self.assertRaises(ValueError):
            api.analyze(experiment="no-such-flight", warehouse=f"sqlite:{STATE['db']}", configs=STATE["configs"], path=self.home)
        # The same session from the command line.
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            for argv in (["runs"], ["report", "--part", "details"], ["why", "1P Long Click Rate", "--run", "synth-3p-boost-mid"],
                         ["lessons", "list"], ["cards", "list"], ["setup"]):
                self.assertEqual(cli(["--home", self.home, *argv]), 0, argv)
            self.assertEqual(cli(["--home", self.home, "lessons", "approve", rules[0]]), 2)  # no --approver

    def test_m0_grades_a_scorecard_by_its_data(self):
        """NULL units are UNKNOWN (directional only); zero units BLOCK (not permitted, no deep dive planned); two rows for
        one metric keep one number and WARN; without the exposed-user count the vendor's balance check decides the split;
        a team metric name that contains an internal word is fine; a decision-grade report is approved by name, naming
        every warning."""
        rows = t1_rows("synth-3p-pin")
        latest = max(r["ds"] for r in rows)
        primary = lambda r: r["metric_name"] == "Search Success Rate"  # noqa: E731
        out = self.analyze("synth-3p-pin", rows)
        self.assertEqual(out["counts"]["grades"], {"pin": "decision_grade"})
        with self.subTest(case="no contract and no config lower the grade; M1 still runs"):
            bare = self.analyze("synth-3p-pin", rows, inputs=False)
            trace = load(os.path.join(bare["run_dir"], "trace.json"))
            self.assertEqual(bare["counts"]["grades"], {"pin": "directional_only"})
            self.assertTrue(any(c["candidate_id"] for c in trace["claim_records"]))  # causes are still proposed
            self.assertEqual(trace["claim_records"] and {c["pair"] for c in trace["claim_records"]}, {"pin"})
        with self.subTest(case="NULL units are unknown, not zero"):
            null = [{**r, "control_units": None, "test_units": None} if primary(r) else r for r in rows]
            self.assertEqual(self.analyze("synth-3p-pin", null)["counts"]["grades"], {"pin": "directional_only"})
        with self.subTest(case="zero units block"):
            zero = [{**r, "control_units": 0} if primary(r) and r["ds"] == latest else r for r in rows]
            blocked = self.analyze("synth-3p-pin", zero)["counts"]
            self.assertEqual((blocked["grades"], blocked["access_gaps"]), ({"pin": "not_permitted"}, 0))
        with self.subTest(case="duplicate rows keep one number and warn"):
            original = next(r for r in rows if primary(r) and r["ds"] == latest)
            twin = {**original, "p_value": None, "relative_delta": -10.0, "relative_ci_lower": -12.0, "relative_ci_upper": -8.0}
            dup = self.analyze("synth-3p-pin", [twin] + rows)  # the row without a p-value comes first and still loses
            comparison = load(os.path.join(dup["run_dir"], "case", "scorecard.json"))["comparisons"][0]
            kept = [m for m in comparison["metrics"] if m["metric"] == "Search Success Rate"]
            self.assertEqual((len(kept), comparison["duplicates"]), (1, ["search success rate"]))
            self.assertAlmostEqual(kept[0]["rel_delta"], original["relative_delta"] / 100)  # the row with a p-value
            warns = [w for g in load(os.path.join(dup["run_dir"], "trace.json"))["grades"] if g["after"] == "M0" for w in g["warns"]]
            self.assertTrue(any(w.startswith(m0.NAMES["M0-4"]) for w in warns), warns)
        with self.subTest(case="T1 with sequential and fixed-horizon rows reads one mode, in the scorecard and the series"):
            base = self.analyze("synth-3p-pin", rows)["run_dir"]
            shift = lambda v, d: None if v is None else v + d  # noqa: E731
            wide = lambda r: {**r, "use_sequential": 1, "relative_ci_lower": shift(r["relative_ci_lower"], -1.0),  # noqa: E731
                              "relative_ci_upper": shift(r["relative_ci_upper"], 1.0)}
            both = self.analyze("synth-3p-pin", rows + [wide(r) for r in rows])["run_dir"]
            comparison = load(os.path.join(both, "case", "scorecard.json"))
            kept = next(m for m in comparison["comparisons"][0]["metrics"] if m["metric"] == "Search Success Rate")
            original = next(r for r in rows if primary(r) and r["ds"] == latest)
            self.assertEqual((comparison["analysis"]["sequential"], comparison["comparisons"][0]["duplicates"]), (True, []))
            self.assertAlmostEqual(kept["rel_ci"][0], (original["relative_ci_lower"] - 1.0) / 100)  # the wider read
            self.assertEqual(len(load(os.path.join(both, "case", "series.json"))), len(load(os.path.join(base, "case", "series.json"))))
            home = tempfile.mkdtemp(dir=STATE["tmp"])  # the flight's own setting in its config wins over the default
            scorecard, registered = os.path.join(home, "both.json"), os.path.join(home, "config.json")
            save(scorecard, rows + [wide(r) for r in rows])
            save(registered, {**load(config_path("synth-3p-pin")), "sequential": False})
            fixed = api.analyze(scorecard=scorecard, contract=contract_path("synth-3p-pin"), config=registered, path=home)["run_dir"]
            self.assertIs(load(os.path.join(fixed, "case", "scorecard.json"))["analysis"]["sequential"], False)
        with self.subTest(case="a primary without a bar gets no call; a bar of 0 is the owner's choice and gets one"):
            home = tempfile.mkdtemp(dir=STATE["tmp"])
            scorecard, contract = os.path.join(home, "scorecard.json"), os.path.join(home, "contract.json")
            save(scorecard, rows)
            registered = load(contract_path("synth-3p-pin"))
            for bar, made in ((None, False), (0, True)):
                primaries = {**registered["primaries"], "metrics": [{**m, "bar": bar} for m in registered["primaries"]["metrics"]]}
                save(contract, {**registered, "primaries": primaries})
                calls = api.analyze(scorecard=scorecard, contract=contract, config=config_path("synth-3p-pin"),
                                    path=home)["counts"]["calls"]
                self.assertEqual(calls["pin"] != "not_made", made, (bar, calls))
        with self.subTest(case="without the exposed-user count, the vendor's balance check decides the split"):
            loss = [r for r in t1_rows("synth-split-loss") if r["metric_name"] != "Exposed Users"]
            self.assertEqual(self.analyze("synth-split-loss", loss)["counts"]["grades"], {"test": "not_permitted"})
        with self.subTest(case="a team metric name may contain an internal word"):
            renamed = [{**r, "metric_name": "3P Packet Clicks Per Query"} if r["metric_name"] == "3P Clicks Per Query" else r
                       for r in rows]
            self.assertEqual(self.analyze("synth-3p-pin", renamed)["counts"]["grades"], {"pin": "decision_grade"})
        with self.subTest(case="a decision-grade report is approved by name, naming every warning"):
            with self.assertRaises(ValueError):
                api.approve_report("owner", [], run=out["run_dir"])
            api.approve_report("owner", ["power"], run=out["run_dir"])
            stamp = load(os.path.join(out["run_dir"], "approval.json"))
            self.assertEqual((stamp["approver"], stamp["pairs"]), ("owner", ["pin"]))
            self.assertEqual(stamp["readout_sha"], load(os.path.join(out["run_dir"], "trace.json"))["readout_sha"])

    def test_the_mcp_server_speaks_json_rpc_on_stdout_only(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "analyze", "arguments": {
                    "scorecard": self.scorecard, "contract": contract_path("synth-3p-boost-mid"), "config": config_path("synth-3p-boost-mid")}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "approve_lesson", "arguments": {"lesson_id": "x"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}}]
        home = tempfile.mkdtemp(dir=STATE["tmp"])
        proc = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "kangaroo"), "mcp"], cwd=STATE["tmp"],
                              input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True,
                              env={**os.environ, "KANGAROO_HOME": home}, timeout=300)
        replies = {r["id"]: r for r in (json.loads(line) for line in proc.stdout.splitlines())}
        self.assertEqual(sorted(replies), [1, 2, 3, 4, 5])  # every line on stdout is a reply; the notification got none
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "kangaroo")
        self.assertIn("analyze", [t["name"] for t in replies[2]["result"]["tools"]])
        self.assertFalse(replies[3]["result"]["isError"])
        self.assertTrue(os.path.isdir(os.path.join(home, "cases", "synth-3p-boost-mid")))
        self.assertTrue(replies[4]["result"]["isError"])
        self.assertIn("approver", replies[4]["result"]["content"][0]["text"])
        self.assertEqual(replies[5]["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
