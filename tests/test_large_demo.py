"""The large demo procedure: ~2,000 lines, one planted bug, one real bug found on the way.

Steps are found by source line, not by label, so the tests survive edits to the generator.
"""
from __future__ import annotations

import importlib.util
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as h  # noqa: E402
from helpers import LARGE, direct_sources, output  # noqa: E402

PROC = LARGE / "usp_LoadFactRevenue.sql"
SCHEMA_SQL = LARGE / "schema.sql"


def line_of(text: str, needle: str) -> int:
    for i, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not in the demo procedure")


def step_at(p: dict, line: int) -> dict:
    """The innermost step whose lines cover a source line."""
    hits = [s for s in p["steps"] if s.get("lines") and s["lines"][0] <= line <= s["lines"][1]]
    if not hits:
        raise AssertionError(f"no step covers line {line}")
    return min(hits, key=lambda s: s["lines"][1] - s["lines"][0])


class GeneratorInSync(unittest.TestCase):
    def test_committed_files_match_the_generator(self):
        spec = importlib.util.spec_from_file_location("build_large_procedure", LARGE / "build_large_procedure.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(PROC.read_text(encoding="utf-8"), mod.PROC.replace("\r\n", "\n"),
                         "run examples/large/build_large_procedure.py")
        self.assertEqual(SCHEMA_SQL.read_text(encoding="utf-8"), mod.SCHEMA)


@h.requires_parser
class LargeDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PROC.read_text(encoding="utf-8")
        t = time.time()
        cls.p = h.document([PROC], schema=[SCHEMA_SQL])["etl.usp_LoadFactRevenue"]
        cls.seconds = time.time() - t

    def test_size(self):
        self.assertGreater(len(self.text.splitlines()), 1800)
        self.assertGreater(self.p["summary"]["counts"]["statements"], 300)
        self.assertLess(self.seconds, 120, "a 2,000-line procedure documents in well under two minutes")

    def test_planted_bug_is_the_one_high_issue(self):
        p = self.p
        bug = step_at(p, line_of(self.text, "o.NetAmountZAR = o.NetAmountZAR * fx.RateToZAR + ROUND("))
        highs = [i for i in p["issues"] if i["severity"] == "high"]
        self.assertEqual([i["rule"] for i in highs], ["factor-applied-twice"])
        issue = highs[0]
        self.assertIn(bug["id"], issue["steps"])
        self.assertIn("#Orders.NetAmountZAR", issue["title"])
        self.assertIn("RateToZAR", issue["title"])
        self.assertIn(f"step {bug['label']}", issue["title"])
        # the first conversion is an earlier step, and the issue names both
        first = [s for s in issue["steps"] if s != bug["id"]]
        self.assertTrue(first, "the issue points at the step that converted the amount the first time")
        self.assertLess(h.step_labels(p, first)[0].split(".")[0].zfill(4), bug["label"].split(".")[0].zfill(4))
        self.assertIn("@IncludeAdjustments = 1", [c["text"] for c in bug["conditions"]])

    def test_bonus_finding_loop_variable(self):
        p = self.p
        found = [i for i in p["issues"] if i["rule"] == "variable-kept-in-loop"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "medium")
        self.assertIn("@RegionTarget", found[0]["title"])
        assign = step_at(p, line_of(self.text, "SELECT @RegionTarget = t.TargetZAR"))
        self.assertIn(assign["id"], found[0]["steps"])

    def test_no_other_medium_or_high(self):
        serious = sorted(i["rule"] for i in self.p["issues"] if i["severity"] in ("high", "medium"))
        self.assertEqual(serious, ["factor-applied-twice", "variable-kept-in-loop"])

    def test_fact_column_trace(self):
        p = self.p
        o = output(p, "dbo.FactRevenue", "NetAmountZAR")
        self.assertEqual(o["status"], "resolved")
        src = direct_sources(p, "dbo.FactRevenue", "NetAmountZAR")
        for want in ("ref.FxRates.RateToZAR", "sales.ManualAdjustment.Amount", "sales.OrderLine.Quantity",
                     "sales.OrderLine.UnitPrice"):
            self.assertIn(want, src)
        self.assertIn("@IncludeAdjustments = 1", [c["text"] for c in o["conditions"]])
        bug = step_at(p, line_of(self.text, "o.NetAmountZAR = o.NetAmountZAR * fx.RateToZAR + ROUND("))
        self.assertIn(bug["id"], o["steps"], "the planted bug is on the fact column's path")

    def test_trace_walk_agrees_with_payload(self):
        """The Python walk (used by Word, agent and trace exports) finds the same steps as the payload."""
        from sqldocgen.view import View
        v = View(self.p)
        key = output(self.p, "dbo.FactRevenue", "NetAmountZAR")["key"]
        model = v.trace_model(key, indirect=False)
        steps = {e["sid"] for e in model["steps"]}
        self.assertTrue(set(output(self.p, "dbo.FactRevenue", "NetAmountZAR")["steps"]) <= steps | {None})
        self.assertIn("ref.FxRates.RateToZAR", model["bases"])

    def test_dynamic_sql_and_cursor_documented(self):
        p = self.p
        self.assertEqual(p["summary"]["counts"]["dynamic"], 1)
        self.assertTrue(all(d["status"] in ("resolved", "partial") for d in p["dynamic"]))
        self.assertIn("cursor", [i["rule"] for i in p["issues"]])


if __name__ == "__main__":
    unittest.main()
