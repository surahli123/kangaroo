"""The agent end to end on the synthetic warehouse, with unittest only (the work laptop has
no pytest). Run from :

    python3 -m unittest discover -s tests
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "synth"))

import make_warehouse  # noqa: E402

from learning import cards, evaluate, knowledge, lessons  # noqa: E402
from kangaroo import run as runner  # noqa: E402
from kangaroo.m0 import checks as m0  # noqa: E402
from kangaroo.m0.grade import grade  # noqa: E402
from kangaroo.m1 import cuts  # noqa: E402
from kangaroo.sources import config as cfgsrc  # noqa: E402
from kangaroo.sources import resolver  # noqa: E402
from kangaroo.sources import t1 as t1src  # noqa: E402
from kangaroo.sources.warehouse import QueryRefused, SqliteWarehouse, bind, guard  # noqa: E402

FLIGHTS = ["synth-3p-boost-mid", "synth-typeahead-regress", "synth-auth-requery", "synth-answer-shift", "synth-null",
           "synth-split-loss", "synth-3p-boost-v2", "synth-3p-pin", "synth-split-one-arm", "synth-renamed-primary",
           "synth-renamed-primary-b"]
STATE = {}


def setUpModule():
    tmp = tempfile.mkdtemp(prefix="agent-tests-")
    synth = os.path.join(tmp, "synth")
    make_warehouse.main(["--out", synth, "--flights", *FLIGHTS])
    overlay = os.path.join(tmp, "overlay")
    shutil.copytree(os.path.join(ROOT, "overlay.example"), overlay, ignore=shutil.ignore_patterns("contracts"))
    shutil.copytree(os.path.join(synth, "contracts"), os.path.join(overlay, "contracts"))
    STATE.update(tmp=tmp, synth=synth, overlay=overlay, gold=load(os.path.join(synth, "gold.json")),
                 db=os.path.join(synth, "warehouse.sqlite"))


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def tearDownModule():
    shutil.rmtree(STATE["tmp"], ignore_errors=True)


def run_flight(name, overlay=None, knowledge_dir=None, cards_dir=None, out=None):
    out = out or tempfile.mkdtemp(dir=STATE["tmp"])
    return runner.run(name, overlay or STATE["overlay"], SqliteWarehouse(STATE["db"]),
                      cfgsrc.JsonDirConfig(os.path.join(STATE["synth"], "configs")), out, knowledge_dir, cards_dir), out


def check(cid, status, affects=True):
    return {"id": cid, "name": m0.NAMES[cid], "status": status, "reason": f"{cid} {status}", "source": "test",
            "affects_grade": affects, "diagnosed_by": [], "details": {}}


class Grade(unittest.TestCase):
    """Every grade is reachable (this replaces the work repo's test that pinned the flags false)."""

    def test_four_grades(self):
        ok = [check("M0-1", "PASS"), check("M0-2", "PASS"), check("duration", "PASS")]
        self.assertEqual(grade(ok + [check("M0-2", "BLOCK")], False)["grade"], "not_permitted")
        self.assertEqual(grade(ok + [check("M0-3", "UNKNOWN")], False)["grade"], "directional_only")
        self.assertEqual(grade(ok, True)["grade"], "directional_only")
        self.assertEqual(grade(ok + [check("stratification", "UNKNOWN", affects=False)], False)["grade"], "decision_grade")
        warned = grade(ok + [check("M0-5", "WARN")], False)
        self.assertEqual((warned["grade"], len(warned["warns"])), ("decision_grade", 1))

    def test_stamp_needs_every_warning(self):
        result, out = run_flight("synth-3p-pin")
        self.assertIn("not yet approved", result["readout"])
        with self.assertRaises(ValueError):
            evaluate.stamp_readout(out, "owner", [])
        evaluate.stamp_readout(out, "owner", ["power"])
        self.assertIn("approved by owner", read(os.path.join(out, "readout.md")))


class M0(unittest.TestCase):
    def test_split_never_uses_metric_units(self):
        comparison = {"metrics": [{"metric": "Search Success Rate", "control_units": 5000, "test_units": 4000}]}
        cfg = {"groups": [{"name": "control", "size": 50}, {"name": "test", "size": 50}]}
        res = m0.split_check(comparison, cfg, {}, "control", "test", {}, split_metric=None)
        self.assertEqual(res["status"], "UNKNOWN")
        res = m0.split_check(comparison, {**cfg, "health_checks": [{"name": "Exposures are balanced", "status": "WARNING"}]},
                             {}, "control", "test", {}, split_metric=None)
        self.assertEqual((res["status"], res["source"]), ("WARN", "vendor health check"))

    def test_missing_config_is_unknown_not_block(self):
        res = m0.config_check(None, {}, None, "control", "test", "the config CLI")
        self.assertEqual(res["status"], "UNKNOWN")
        self.assertIn("the config CLI", res["reason"])

    def test_a_pass_needs_a_source(self):
        with self.assertRaises(ValueError):
            m0.result("M0-1", "PASS", "fine", None)


class Sources(unittest.TestCase):
    def test_guard_refuses_anything_but_one_read(self):
        for bad in ("DELETE FROM t", "SELECT 1; DROP TABLE t", "INSERT INTO t VALUES (1)", "PRAGMA x"):
            with self.assertRaises(QueryRefused):
                guard(bad)
        guard("WITH a AS (SELECT 'drop' AS x) SELECT x FROM a")

    def test_spark_binding_quotes_literals(self):
        sql, params = bind("SELECT * FROM t WHERE a = :a AND b IN :b", {"a": "it's", "b": ["x", "y"]}, "spark")
        self.assertEqual(sql, "SELECT * FROM t WHERE a = 'it''s' AND b IN ('x', 'y')")
        self.assertIsNone(params)

    def test_resolver(self):
        wh = SqliteWarehouse(STATE["db"])
        configs = cfgsrc.JsonDirConfig(os.path.join(STATE["synth"], "configs"))
        table, cols = "t1_scorecard", t1src.columns({})
        by_name = resolver.resolve("synth-null", configs, wh, table, cols)
        uuid = by_name["config_id"]
        self.assertEqual(resolver.resolve(uuid, configs, wh, table, cols)["experiment_name"], "synth-null")
        url = f"https://console.example/experiments/{uuid}/results"
        self.assertEqual(resolver.resolve(url, configs, wh, table, cols)["experiment_name"], "synth-null")
        no_config = resolver.resolve("synth-answer-shift", configs, wh, table, cols)
        self.assertIsNone(no_config["config"])
        with self.assertRaises(resolver.Unresolved):
            resolver.resolve("no-such-flight", configs, wh, table, cols)


class Cuts(unittest.TestCase):
    def test_cut_ties_to_t1(self):
        """A template with T1's definition (1P long clicks per full-page query, all tenants) gives T1's number."""
        wh = SqliteWarehouse(STATE["db"])
        wh_cfg = {**load(os.path.join(STATE["overlay"], "warehouse.json")), "cohort": None}
        tpl = {"same_as_t1": {"produces": "1P long clicks per full-page query", "filter": "t.experience = 'surface_full'",
                              "x": "t.long_click_1p", "y": "1"}}
        as_of = wh.query("SELECT MAX(ds) AS d FROM t1_scorecard WHERE experiment_name = :e", {"e": "synth-null"})["rows"][0]["d"]
        res = cuts.run_cut(wh, "same_as_t1", tpl, wh_cfg, "synth-null", as_of, "control", ["test"], min_units=1)
        t1 = wh.query("SELECT relative_delta FROM t1_scorecard WHERE experiment_name = :e AND ds = :d AND "
                      "metric_name = '1P Long Click Rate'", {"e": "synth-null", "d": as_of})["rows"][0]["relative_delta"]
        self.assertAlmostEqual(100 * res["metrics"][0]["rel_delta"], t1, places=6)

    def test_spark_series_uses_datediff(self):
        templates = cuts.load_templates()
        wh_cfg = load(os.path.join(STATE["overlay"], "warehouse.json"))
        sql = cuts.dry_run_sql(templates["primary_by_exposure_day"], wh_cfg, "spark")
        self.assertIn("DATEDIFF(", sql)
        self.assertTrue(sql.endswith("LIMIT 0"))

    def test_unapproved_tables_are_access_gaps(self):
        overlay = os.path.join(STATE["tmp"], "overlay-no-t3")
        shutil.copytree(STATE["overlay"], overlay, dirs_exist_ok=True)
        cfg = load(os.path.join(overlay, "warehouse.json"))
        cfg["approved"] = ["t1"]
        with open(os.path.join(overlay, "warehouse.json"), "w", encoding="utf-8") as handle:
            json.dump(cfg, handle)
        result, _ = run_flight("synth-3p-boost-mid", overlay=overlay)
        self.assertEqual(result["counts"]["grades"], {"boost_15": "directional_only"})
        self.assertGreater(result["counts"]["access_gaps"], 0)
        self.assertEqual(result["counts"]["cuts_ran"], 0)
        self.assertIn("tables are approved", result["readout"])


class EndToEnd(unittest.TestCase):
    """Each synthetic flight gets its planted grade and mechanism, and every finding cites its data."""

    @classmethod
    def setUpClass(cls):
        cls.results = {name: run_flight(name) for name in FLIGHTS if name != "synth-3p-pin"}

    def test_grades_and_mechanisms(self):
        for name, (result, _) in self.results.items():
            gold = STATE["gold"][name]
            o = evaluate.outcome(result)
            with self.subTest(flight=name):
                if name.startswith("synth-renamed-primary"):  # the contract's primary is missing until an alias is approved
                    self.assertEqual({p["grade"] for p in o.values()}, {"directional_only"})
                elif gold.get("grades"):
                    self.assertEqual({t: p["grade"] for t, p in o.items()}, gold["grades"])
                else:
                    self.assertIn(gold["grade"], {p["grade"] for p in o.values()})
                if name == "synth-3p-boost-v2":
                    self.assertEqual(evaluate.gold_score(o, gold), 0)  # the renamed metric hides it until a lesson maps it
                else:
                    self.assertEqual(evaluate.gold_score(o, gold), 2)

    def test_findings_cite_statements_in_the_trace(self):
        for name, (result, _) in self.results.items():
            statements = {r["statement_id"] for r in result["trace"]["receipt_records"] if r["statement_id"]}
            for c in result["trace"]["claim_records"]:
                if c["support_level"] == "confirmed":
                    with self.subTest(flight=name, claim=c["claim_id"]):
                        self.assertTrue(c["statement_ids"])
                        self.assertTrue(set(c["statement_ids"]) <= statements)
            self.assertEqual(result["counts"]["unsupported"], 0)

    def test_readout_order_and_no_internal_names(self):
        for name, (result, _) in self.results.items():
            body = result["readout"].split("\n---\n")[0]
            with self.subTest(flight=name):
                for word in ("packet", "seal", "receipt", "M0-"):
                    self.assertNotIn(word, body)
                order = [body.find(h) for h in ("## Before triage", "## Ask the engineer", "## Similar experiments")]
                self.assertEqual(order, sorted(order))
                self.assertTrue(body.splitlines()[0].startswith("# "))

    def test_blocked_flight_reads_no_movements(self):
        result, _ = self.results["synth-split-loss"]
        self.assertIn("not interpretable", result["readout"])
        self.assertNotIn("What moved:", result["readout"])
        self.assertEqual(result["counts"]["cuts_ran"], 0)

    def test_trace_records_the_grade_after_each_action(self):
        result, _ = self.results["synth-3p-boost-mid"]
        actions = result["trace"]["action_records"]
        self.assertTrue(actions)
        self.assertTrue(all(a.get("grade_after") for a in actions))
        self.assertTrue(any(a.get("diagnoses") for a in actions))


class Learning(unittest.TestCase):
    """The loop: propose, evaluate with source flights held out, approve, version, cards."""

    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.mkdtemp(dir=STATE["tmp"])
        cls.cases = os.path.join(cls.shared, "cases")
        empty = os.path.join(cls.shared, "knowledge")
        os.makedirs(empty)
        for name in ("synth-3p-boost-v2", "synth-3p-pin", "synth-null", "synth-typeahead-regress"):
            _, out = run_flight(name, knowledge_dir=empty)
            evaluate.freeze(out, cls.cases, STATE["gold"][name])

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=STATE["tmp"])
        self.k = os.path.join(self.dir, "knowledge")
        os.makedirs(self.k)

    def mapping(self, metric, family, source):
        return lessons.new("metric_mapping", {"family": family, "metric": metric}, [source], "test")

    def test_mapping_lesson_generalises_to_a_held_out_flight(self):
        import explain
        from kangaroo import LIBRARY
        library = explain.load_library(self.k, base=LIBRARY)
        scorecard = load(os.path.join(self.cases, "synth-3p-boost-v2", "scorecard.json"))
        proposed = lessons.propose_mappings(scorecard, library, "synth-3p-boost-v2")
        self.assertEqual([p["change"] for p in proposed], [{"family": "p3_placement", "metric": "3P Top-1 Placement Rate"}])
        report = evaluate.evaluate(proposed[0], self.cases, STATE["overlay"], self.k, SqliteWarehouse(STATE["db"]))
        self.assertTrue(report["pass"], report["status"])
        self.assertEqual(report["improved"], ["synth-3p-pin"])
        self.assertEqual([c["case"] for c in report["fit_cases"]], ["synth-3p-boost-v2"])
        before = knowledge.version_hash(self.k)
        after = evaluate.approve(proposed[0], report, "owner", self.k, os.path.join(self.dir, "lessons"))
        self.assertNotEqual(before, after)
        with self.assertRaises(ValueError):
            evaluate.approve(proposed[0], report, "owner", self.k, os.path.join(self.dir, "lessons"))

    def test_a_harmful_lesson_regresses_and_fails(self):
        bad = self.mapping("Typeahead CTR", "latency", "synth-3p-boost-v2")
        report = evaluate.evaluate(bad, self.cases, STATE["overlay"], self.k, SqliteWarehouse(STATE["db"]))
        self.assertFalse(report["pass"])
        self.assertIn("synth-typeahead-regress", report["regressions"])
        with self.assertRaises(ValueError):
            evaluate.approve(bad, report, "owner", self.k, os.path.join(self.dir, "lessons"))

    def test_stubs_and_readout_rules_are_not_replayed(self):
        stub = lessons.propose_patterns([{"flight": f, "counts": {}, "claim_records": [
            {"metric": "X", "support_level": "none", "change": "rose 1%", "pair": "t"}]} for f in "abc"])
        self.assertEqual(len(stub), 1)
        self.assertFalse(evaluate.evaluate(stub[0], self.cases, STATE["overlay"], self.k)["pass"])
        rule = lessons.new("readout_rule", {"rule": "say the arm first"}, ["synth-null"], "test")
        self.assertIn("model-facing", evaluate.evaluate(rule, self.cases, STATE["overlay"], self.k)["status"])

    def test_only_confirmed_cards_are_cited(self):
        folder = os.path.join(self.dir, "cards")
        base = {"layer": "l2-ranking", "tags": ["3p"], "families_moved": ["p3_placement"], "name": "x"}
        cards.save(folder, {**base, "experiment_name": "a", "status": "drafted"})
        cards.save(folder, {**base, "experiment_name": "b", "status": "drafted"})
        cards.confirm(folder, "b", "cause", "lesson", "owner")
        cards.save(folder, {**base, "experiment_name": "b", "status": "drafted"})  # a draft never overwrites a confirmed card
        cited, drafts = cards.similar(cards.load(folder), "c", "l2-ranking", ["3p"], ["p3_placement"])
        self.assertEqual([c["experiment_name"] for c in cited], ["b"])
        self.assertEqual([d["experiment_name"] for d in drafts], ["a"])
        cited, _ = cards.similar(cards.load(folder), "b", "l2-ranking", ["3p"], ["p3_placement"])
        self.assertEqual(cited, [])


class PressureTest(unittest.TestCase):
    """Regressions for the adversarial review's findings, one test each."""

    @classmethod
    def setUpClass(cls):
        cls.runs = {name: run_flight(name) for name in ("synth-split-one-arm", "synth-auth-requery", "synth-null")}

    def test_one_bad_arm_never_decides_a_clean_arm(self):
        result, _ = self.runs["synth-split-one-arm"]
        pairs = {p["test"]: p for p in result["pairs"]}
        self.assertEqual(pairs["good"]["grade"]["grade"], "decision_grade")
        self.assertNotEqual(pairs["good"]["decision"]["call"], "cant_tell")  # the 3-arm split used to decide it
        self.assertEqual([g["name"] for g in pairs["good"]["config_for_decide"]["groups"]], ["control", "good"])
        minute = result["readout"].split("# Details")[0]
        self.assertIn("- good: Yes", minute)
        self.assertNotIn("(bad) Bring an answer", minute)  # a blocked arm's movements aren't interpreted
        self.assertIn("The bad arm has", minute)

    def test_one_mechanism_one_confirmation(self):
        result, _ = self.runs["synth-auth-requery"]
        p = result["pairs"][0]
        confirmed = {c["candidate_id"] for c in p["claims"] if c["support_level"] == "confirmed"}
        self.assertEqual(confirmed, {"authorized_struggle"})
        ruled_out = {i["id"] for i in p["explained"]["ruled_out"]}
        self.assertTrue({"first_party_displaced", "typeahead_funnel"} <= ruled_out)
        roles = {a["role"] for a in p["actions"] if a["status"] == "ran" and a.get("target_pattern") == "authorized_struggle"}
        self.assertEqual(roles, {"where", "control"})

    def test_unsupported_is_audited_from_the_actions(self):
        from kangaroo import LIBRARY
        from kangaroo.m1 import method
        import explain
        library = explain.load_library(None, base=LIBRARY)
        claim = {"claim_id": "c1", "metric": "m", "candidate_id": "first_party_displaced", "support_level": "confirmed",
                 "statement_ids": ["s1"]}
        where = {"status": "ran", "target_pattern": "first_party_displaced", "outcome": "confirms", "role": "where",
                 "exact_next_task": "1P long-click share on 3P-eligible queries, by arm"}
        control = {**where, "role": "control", "exact_next_task": "1P long-click share on queries with no 3P candidate, by arm"}
        self.assertEqual(method.unsupported([claim], [where, control], library), [])
        self.assertEqual(len(method.unsupported([claim], [where], library)), 1)  # no control cut behind it
        self.assertEqual(len(method.unsupported([claim], [{**where, "caveat": "share moved"}, control], library)), 1)

    def test_a_caveat_or_a_missing_control_stops_a_confirmation(self):
        import explain

        def inst():
            return {"id": "p", "name": "P", "evidence": [], "status": "untested", "label": "inferred",
                    "deep_dives": [{"cut": "where it acts", "sql": "w", "needs": {"metric": "f"}, "confirms_when": {"f": "down"}}]}
        fam = {"f": {"metric": "F", "rel_delta": -0.1, "rel_ci": [-0.15, -0.05]}}
        run = lambda i, ev, ctl: explain.apply_evidence([i], fam, lambda f: 0.01, [], None, ev, ctl)[0][0]["status"]  # noqa: E731
        self.assertEqual(run(inst(), {}, False), "confirmed by a cut")
        self.assertEqual(run(inst(), {"cuts": [{"sql": "w", "caveat": "the share moved"}]}, False), "fits a cut")
        self.assertEqual(run(inst(), {}, True), "fits a cut")  # needs a control cut, and the library has none

    def test_null_units_are_unknown_and_zero_units_block(self):
        contract = {"primaries": {"metrics": [{"metric": "S"}]}}
        unfilled = {"metrics": [{"metric": "S", "control_units": None, "test_units": None}]}
        self.assertEqual(m0.coverage_check(unfilled, contract)["status"], "UNKNOWN")
        empty = {"metrics": [{"metric": "S", "control_units": 0, "test_units": 10}]}
        self.assertEqual(m0.coverage_check(empty, contract)["status"], "BLOCK")

    def test_data_quality_reads_one_metric(self):
        rows = [{"test_group": "test", "metric": m, "snapshot": f"2026-01-0{d}", "control_units": base + 10 * d,
                 "test_units": base + 10 * d} for d in range(1, 6) for m, base in (("A", 1000), ("B", 50))]
        self.assertEqual(m0.quality_check(rows, "test", "A")["status"], "PASS")
        self.assertEqual(m0.quality_check(rows, "test", None)["status"], "UNKNOWN")

    def test_duplicate_rows_keep_one_number_and_warn(self):
        cols = t1src.columns({})
        base = {"experiment": "e", "control_group": "control", "test_group": "test", "snapshot": "2026-01-01", "cuped": 0,
                "sequential": 0, "type": "ratio", "direction": None, "control_mean": 1.0, "test_mean": 1.0, "abs_delta": 0.0,
                "control_units": 10, "test_units": 10}
        rows = [{**base, "metric": "S", "test_mean": 0.9, "abs_delta": -0.1, "rel_delta": -10.0, "rel_ci_lo": -12.0,
                 "rel_ci_hi": -8.0, "p_value": None},
                {**base, "metric": "S", "test_mean": 1.0009, "abs_delta": 0.0009, "rel_delta": 0.09, "rel_ci_lo": -1.0,
                 "rel_ci_hi": 1.2, "p_value": 0.8}]
        sc = t1src.scorecard_from_rows(rows, {**cols, "_percent_fields": ["rel_delta", "rel_ci_lo", "rel_ci_hi"]})
        comparison = sc["comparisons"][0]
        self.assertEqual(len(comparison["metrics"]), 1)
        self.assertAlmostEqual(comparison["metrics"][0]["rel_delta"], 0.0009)  # the row with a p-value
        res = m0.consistency_check(comparison, {"primaries": {"metrics": [{"metric": "S"}]}}, sc["analysis"])
        self.assertEqual(res["status"], "WARN")
        self.assertTrue(res["reason"].startswith("1 metric name appears twice"))

    def test_cards_of_null_flights_are_not_similar_to_everything(self):
        result, out = self.runs["synth-null"]
        card = load(os.path.join(out, "card_draft.json"))
        primaries = {"success", "success_weighted", "p3_clicks_auth", "answer_success"}  # none of them moved
        self.assertEqual(set(card["families_moved"]) & primaries, set())
        self.assertTrue(all(not m["change"].startswith("didn't move") for m in card["what_moved"]))

    def test_team_metric_names_never_trip_the_internal_word_check(self):
        from kangaroo.render import readout as rd
        result, _ = self.runs["synth-null"]
        pairs = copy.deepcopy(result["pairs"])
        for m in pairs[0]["metrics"]:
            if m["metric"] == "3P Clicks Per Query":
                m["metric"] = "3P Packet Clicks Per Query"
        for c in pairs[0]["claims"]:
            c["metric"] = c["metric"].replace("3P Clicks Per Query", "3P Packet Clicks Per Query")
        rd.render("x", "control", pairs, [], [], "p", {}, True)  # no ValueError


class LessonGate(unittest.TestCase):
    """The evaluation gate can't be talked into passing a lesson."""

    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.mkdtemp(dir=STATE["tmp"])
        cls.cases = os.path.join(cls.shared, "cases")
        empty = os.path.join(cls.shared, "knowledge")
        os.makedirs(empty)
        for name in ("synth-3p-boost-v2", "synth-3p-pin", "synth-null", "synth-typeahead-regress", "synth-auth-requery",
                     "synth-renamed-primary", "synth-renamed-primary-b"):
            _, out = run_flight(name, knowledge_dir=empty)
            evaluate.freeze(out, cls.cases, STATE["gold"][name])

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=STATE["tmp"])
        self.k = os.path.join(self.dir, "knowledge")
        os.makedirs(self.k)

    def test_a_vacuous_pattern_fails_even_when_it_names_its_targets(self):
        anything = {f: "any" for f in ("p1_long_click", "p1_binary_ctr", "success", "success_weighted", "typeahead_ctr",
                                       "answer_shown", "latency", "authorized_share", "fp_queries_per_user")}
        pattern = {"id": "more_3p_changes_things", "name": "More 3P clicks change many things", "when": {"p3_clicks": "up"},
                   "any_of": anything, "mechanism": "Other metrics move too.", "deep_dives": [], "metrics": []}
        cases = sorted(os.listdir(self.cases))
        lesson = lessons.new("pattern", {"pattern": pattern}, ["synth-3p-boost-v2"], "test", target_cases=cases)
        report = evaluate.evaluate(lesson, self.cases, STATE["overlay"], self.k)
        self.assertFalse(report["pass"], report["status"])
        self.assertTrue(report["regressions"])

    def test_an_edited_report_cannot_approve_a_lesson(self):
        bad = lessons.new("metric_mapping", {"family": "latency", "metric": "Typeahead CTR"}, ["synth-3p-boost-v2"], "test")
        report = evaluate.evaluate(bad, self.cases, STATE["overlay"], self.k)
        self.assertFalse(report["pass"])
        forged = {**report, "pass": True, "regressions": [], "improved": ["synth-3p-pin"]}
        with self.assertRaises(ValueError):
            evaluate.approve(bad, forged, "owner", self.k, os.path.join(self.dir, "lessons"), self.cases, STATE["overlay"])
        self.assertFalse(os.path.exists(os.path.join(self.k, "metric_families.json")))

    def test_a_lesson_names_its_source(self):
        orphan = lessons.new("metric_mapping", {"family": "p3_placement", "metric": "3P Top-1 Placement Rate"}, [], "test")
        self.assertIn("source flight", evaluate.evaluate(orphan, self.cases, STATE["overlay"], self.k)["status"])

    def test_a_probing_cut_template_is_refused(self):
        probe = lessons.new("cut_template", {"id": "probe", "template": {
            "produces": "x", "filter": "1 = 1", "x": "CASE WHEN t.user_id = 'u1' THEN 1e6 ELSE 0 END", "y": "1"}},
            ["synth-null"], "test")
        self.assertIn("refused", evaluate.evaluate(probe, self.cases, STATE["overlay"], self.k)["status"])

    def test_an_alias_restores_a_renamed_primary(self):
        scorecard = load(os.path.join(self.cases, "synth-renamed-primary", "scorecard.json"))
        contract = load(os.path.join(self.cases, "synth-renamed-primary", "contract.json"))
        proposed = lessons.propose_aliases(scorecard, contract, "synth-renamed-primary")
        self.assertEqual([p["change"] for p in proposed],
                         [{"scorecard_name": "Search Success Rate v2", "contract_name": "Search Success Rate"}])
        report = evaluate.evaluate(proposed[0], self.cases, STATE["overlay"], self.k)
        self.assertTrue(report["pass"], report["status"])
        self.assertEqual(report["improved"], ["synth-renamed-primary-b"])
        evaluate.approve(proposed[0], report, "owner", self.k, os.path.join(self.dir, "lessons"), self.cases, STATE["overlay"])
        result, _ = run_flight("synth-renamed-primary-b", knowledge_dir=self.k)
        self.assertEqual(result["counts"]["grades"], {"pin": "decision_grade"})
        self.assertIn("'Search Success Rate v2' as 'Search Success Rate'", result["readout"])


class Interactive(unittest.TestCase):
    """The functions a chat session reaches, and the stdio MCP server that serves them."""

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(dir=STATE["tmp"])
        wh = SqliteWarehouse(STATE["db"])
        rows = wh.query("SELECT * FROM t1_scorecard WHERE experiment_name = :e AND ds = (SELECT MAX(ds) FROM t1_scorecard "
                        "WHERE experiment_name = :e)", {"e": "synth-3p-boost-mid"})["rows"]
        wh.close()
        cls.scorecard = os.path.join(cls.home, "scorecard.json")
        with open(cls.scorecard, "w", encoding="utf-8") as handle:
            json.dump(rows, handle)
        cls.contract = os.path.join(STATE["overlay"], "contracts", "synth-3p-boost-mid.json")
        configs = os.path.join(STATE["synth"], "configs")
        cls.config = next(os.path.join(configs, f) for f in sorted(os.listdir(configs))
                          if load(os.path.join(configs, f)).get("experiment_name") == "synth-3p-boost-mid")

    def test_a_session_from_a_scorecard_file(self):
        from kangaroo import api
        out = api.analyze(scorecard=self.scorecard, contract=self.contract, config=self.config, path=self.home)
        self.assertTrue(out["text"].startswith("## In one minute"))
        self.assertIn("Can this go to triage?", out["text"])
        self.assertTrue(os.path.exists(out["html"]))
        self.assertIn("1P Long Click Rate", api.why("1P Long Click Rate", path=self.home)["text"])
        self.assertIn("has no question", api.why("nothing like it", path=self.home)["text"])
        api.review("wrong", metric="1P Long Click Rate", note="it is displacement", reviewer="owner", path=self.home)
        proposed = api.propose(path=self.home)
        self.assertTrue(any(i.startswith("readout_rule-") for i in proposed["lessons"]))
        self.assertIn("Questions triage will ask", api.report(part="triage_prep", path=self.home)["text"])
        with self.assertRaises(ValueError):
            api.approve_lesson(proposed["lessons"][0], "", path=self.home)

    def test_the_mcp_server_speaks_json_rpc_on_stdout_only(self):
        import subprocess
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "analyze", "arguments": {
                    "scorecard": self.scorecard, "contract": self.contract, "config": self.config}}},
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
        self.assertIn("In one minute", replies[3]["result"]["content"][0]["text"])
        self.assertTrue(replies[4]["result"]["isError"])
        self.assertIn("approver", replies[4]["result"]["content"][0]["text"])
        self.assertEqual(replies[5]["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
