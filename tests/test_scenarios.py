"""Expected lineage and findings for the synthetic scenarios (examples/scenarios).

Each scenario file states its expectation in its header comment; these tests hold the
tool to it in the three modes: procedure only, with table definitions, and project.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as h  # noqa: E402
from helpers import SCENARIOS, SCHEMA, direct_sources, output, rules, step_labels  # noqa: E402

from sqldocgen import SCHEMA_VERSION  # noqa: E402


def issues_by_rule(p, rule):
    return [i for i in p["issues"] if i["rule"] == rule]


def issue_steps(p, issue):
    return step_labels(p, [s for s in issue.get("steps", []) if any(st["id"] == s for st in p["steps"])])


@h.requires_parser
class ProcedureOnly(unittest.TestCase):
    """No table definitions, no call expansion: what the procedure text alone supports."""

    @classmethod
    def setUpClass(cls):
        cls.docs = {}
        for f in sorted(SCENARIOS.glob("*.sql")):
            cls.docs.update(h.document([f], project=False))

    def doc(self, name):
        return self.docs[name]

    def test_every_scenario_documented(self):
        expected = {"etl.usp_TempTableChain", "Ref.usp_GetRates", "etl.usp_RepriceProducts", "rpt.usp_Summaries",
                    "crm.usp_ScoreCustomers", "etl.usp_UpdatePrices", "etl.usp_MergeFactSales",
                    "etl.usp_BranchingRevenue", "rpt.usp_OrderTotals", "hr.usp_Commission",
                    "etl.usp_PurgeInBatches", "crm.usp_SnapshotCustomers", "crm.usp_UnsafeTransaction",
                    "etl.usp_DynamicReprice", "ops.usp_TruncateAndCount", "crm.usp_BandFor", "crm.usp_ScoreOne",
                    "crm.usp_ScoreAll", "crm.usp_CopyCustomers", "crm.usp_SearchCustomers",
                    "crm.usp_SearchCustomersSafe"}
        self.assertEqual(expected, set(self.docs))
        for name, p in self.docs.items():
            self.assertEqual(p["mode"], "procedure", name)

    def test_01_temp_table_chain(self):
        p = self.doc("etl.usp_TempTableChain")
        self.assertEqual(direct_sources(p, "dbo.DailyRevenue", "AmountZar"), {"Ref.FxRates.Rate", "Sales.Orders.Amount"})
        self.assertEqual(direct_sources(p, "dbo.DailyRevenue", "Region"), {"Sales.Customers.Region"})
        self.assertEqual(direct_sources(p, "dbo.DailyRevenue", "LoadDate"), {"@AsOfDate.value"})
        self.assertEqual(step_labels(p, output(p, "dbo.DailyRevenue", "AmountZar")["steps"]), ["2", "3", "5", "6"])
        self.assertEqual(p["issues"], [])
        self.assertEqual(p["summary"]["counts"]["temp"], 3)

    def test_02_insert_exec_without_expansion_is_partial(self):
        p = self.doc("etl.usp_RepriceProducts")
        o = output(p, "dbo.Prices", "PriceZar")
        self.assertEqual(o["status"], "partial", "the rates come from a procedure that is not expanded")
        self.assertIn("dbo.Prices.Price", direct_sources(p, "dbo.Prices", "PriceZar"))
        rates = self.doc("Ref.usp_GetRates")
        self.assertEqual(direct_sources(rates, "Result set 1", "Rate"), {"Ref.FxRates.Rate"})

    def test_03_stacked_and_recursive_ctes(self):
        p = self.doc("rpt.usp_Summaries")
        self.assertEqual(direct_sources(p, "dbo.ProductSummary", "Revenue"),
                         {"Sales.OrderLines.Quantity", "Sales.OrderLines.UnitPrice"})
        self.assertEqual(direct_sources(p, "dbo.ProductSummary", "TopProduct"), {"Ref.Products.ProductCode"})
        self.assertEqual(direct_sources(p, "dbo.OrgChart", "Level"), set(), "Level is a counter, not a copied value")
        self.assertEqual(direct_sources(p, "dbo.OrgChart", "Path"), {"Ref.Employees.FullName"})
        self.assertTrue(any(l["kind"] == "cte" for l in p["locals"]))

    def test_04_repeated_updates(self):
        p = self.doc("crm.usp_ScoreCustomers")
        o = output(p, "dbo.CustomerScore", "Score")
        self.assertEqual(direct_sources(p, "dbo.CustomerScore", "Score"), set())
        self.assertEqual(step_labels(p, o["steps"]), ["6", "8"], "the overwritten step 5 is not on the value path")
        found = issues_by_rule(p, "overwritten-before-read")
        self.assertEqual(len(found), 1)
        self.assertIn("#score.Score", found[0]["title"])
        self.assertEqual(issue_steps(p, found[0])[0], "5")

    def test_05_update_from_join(self):
        p = self.doc("etl.usp_UpdatePrices")
        self.assertEqual(direct_sources(p, "dbo.Prices", "PriceZar"),
                         {"dbo.Prices.Price", "Ref.FxRates.Rate", "Ref.Products.ListPrice"},
                         "two partial updates: both values can reach the column")

    def test_06_merge_with_output(self):
        p = self.doc("etl.usp_MergeFactSales")
        self.assertEqual(direct_sources(p, "dbo.FactSales", "NetAmount"), {"Sales.Orders.Amount", "Sales.Orders.Discount"})
        self.assertEqual(direct_sources(p, "dbo.FactSalesAudit", "OldNet"), {"dbo.FactSales.NetAmount"})
        self.assertEqual(direct_sources(p, "dbo.FactSalesAudit", "NewNet"), {"Sales.Orders.Amount", "Sales.Orders.Discount"})

    def test_07_branches(self):
        p = self.doc("etl.usp_BranchingRevenue")
        self.assertEqual(direct_sources(p, "dbo.DailyRevenue", "AmountZar"), {"Ref.FxRates.Rate", "Sales.Orders.Amount"})
        conds = [c["text"] for c in output(p, "dbo.DailyRevenue", "AmountZar")["conditions"]]
        self.assertIn("@Mode = 'FX'", conds)
        found = issues_by_rule(p, "left-join-made-inner")
        self.assertEqual([i["severity"] for i in found], ["high"])
        self.assertEqual(issue_steps(p, found[0]), ["4"])

    def test_08_table_variables(self):
        p = self.doc("rpt.usp_OrderTotals")
        self.assertEqual(direct_sources(p, "Result set 1", "Total"),
                         {"Sales.OrderLines.Quantity", "Sales.OrderLines.UnitPrice"})

    def test_09_cursor_loop(self):
        p = self.doc("hr.usp_Commission")
        self.assertEqual(direct_sources(p, "dbo.Commission", "Commission"), {"Sales.Orders.Amount"})
        self.assertEqual(direct_sources(p, "dbo.Commission", "EmployeeId"), {"Ref.Employees.EmployeeId"},
                         "the employee id travels through the cursor variable")
        self.assertIn("cursor", rules(p))

    def test_10_while_loop(self):
        p = self.doc("etl.usp_PurgeInBatches")
        o = output(p, "dbo.LoadLog", "RowsAffected")
        self.assertEqual(step_labels(p, o["steps"]), ["5", "7"])
        self.assertIn("@Rows > 0", [c["text"] for c in o["conditions"]])
        self.assertTrue(any(sc["kind"] == "loop" for sc in p["scopes"]))

    def test_11_transactions(self):
        snap = self.doc("crm.usp_SnapshotCustomers")
        self.assertEqual(rules(snap), ["nolock"])
        unsafe = self.doc("crm.usp_UnsafeTransaction")
        self.assertEqual([i["severity"] for i in issues_by_rule(unsafe, "open-transaction-on-exit")], ["high"])
        self.assertIn("transaction-without-try", rules(unsafe))
        self.assertIn("@@error", rules(unsafe))

    def test_12_dynamic_sql_resolved_with_parameters(self):
        p = self.doc("etl.usp_DynamicReprice")
        dyn = {d["step"]: d for d in p["dynamic"]}
        self.assertEqual(dyn["5"]["status"], "resolved")
        self.assertEqual(direct_sources(p, "dbo.Prices", "PriceZar"), {"dbo.Prices.Price", "@Factor.value"},
                         "the sp_executesql parameter is bound to the procedure's @Factor")
        self.assertEqual(step_labels(p, output(p, "dbo.Prices", "PriceZar")["steps"]), ["5", "5.1"])
        self.assertNotIn("dynamic-sql-unresolved", rules(p))

    def test_13_dynamic_sql_not_resolvable(self):
        p = self.doc("ops.usp_TruncateAndCount")
        dyn = {d["step"]: d for d in p["dynamic"]}
        self.assertEqual(dyn["4"]["status"], "partial")
        self.assertIn("@TableName", dyn["4"]["placeholders"])
        self.assertEqual(dyn["6"]["status"], "unresolved")
        self.assertIn("(object named by @TableName)", {r["name"] for r in p["relations"]})
        self.assertEqual([i["severity"] for i in issues_by_rule(p, "dynamic-sql-unresolved")], ["medium"])
        self.assertEqual([i["severity"] for i in issues_by_rule(p, "dynamic-sql-partial")], ["low"])
        self.assertNotIn("sql-injection-risk", rules(p), "QUOTENAME(@TableName) is the safe way to do it")
        cov = {c["check"]: c for c in p["coverage"]}
        self.assertEqual(cov["Dynamic SQL"]["level"], "bad", "the honesty table says what could not be read")
        self.assertIn("1 unreadable", cov["Dynamic SQL"]["result"])

    def test_16_sql_injection(self):
        unsafe = self.doc("crm.usp_SearchCustomers")
        found = issues_by_rule(unsafe, "sql-injection-risk")
        self.assertEqual([i["severity"] for i in found], ["high"])
        self.assertIn("@CustomerName", found[0]["title"])
        self.assertIn("@SortColumn", found[0]["title"])
        self.assertNotIn("@Top", found[0]["title"])
        self.assertEqual(rules(self.doc("crm.usp_SearchCustomersSafe")).count("sql-injection-risk"), 0)
        safe = self.doc("crm.usp_SearchCustomersSafe")
        self.assertEqual(direct_sources(safe, "Result set 1 (dynamic SQL at step 4)", "Region"),
                         {"Sales.Customers.Region"}, "the safe statement is rebuilt and traced like static SQL")

    def test_15_select_star_without_definitions(self):
        p = self.doc("crm.usp_CopyCustomers")
        o = output(p, "dbo.CustomerSnapshot", "*")
        self.assertEqual(o["status"], "partial")
        self.assertEqual(direct_sources(p, "dbo.CustomerSnapshot", "*"), {"Sales.Customers.*"})
        self.assertIn("select-star-into-table", rules(p))
        self.assertIn("insert-without-column-list", rules(p))

    def test_payload_invariants(self):
        for name, p in self.docs.items():
            with self.subTest(procedure=name):
                check_invariants(self, p)


@h.requires_parser
class WithSchema(unittest.TestCase):
    """Table definitions resolve SELECT * and enable the key-based checks."""

    @classmethod
    def setUpClass(cls):
        cls.copy = h.document([SCENARIOS / "15_select_star.sql"], schema=[SCHEMA])["crm.usp_CopyCustomers"]
        cls.prices = h.document([SCENARIOS / "05_update_from_join.sql"], schema=[SCHEMA])["etl.usp_UpdatePrices"]

    def test_select_star_expands_to_columns(self):
        p = self.copy
        self.assertEqual(p["mode"], "schema")
        cols = {o["col"] for o in p["outputs"] if h.rel_name(p, o["rel"]) == "dbo.CustomerSnapshot"} - {"(rows)"}
        self.assertEqual(cols, {"CustomerId", "CustomerName", "Region", "Segment", "CreditLimit"})
        o = output(p, "dbo.CustomerSnapshot", "CreditLimit")
        self.assertEqual(o["status"], "resolved")
        self.assertEqual(direct_sources(p, "dbo.CustomerSnapshot", "CreditLimit"), {"Sales.Customers.CreditLimit"})
        self.assertEqual(step_labels(p, o["steps"]), ["2", "3", "4"])

    def test_duplicate_join_needs_keys(self):
        found = issues_by_rule(self.prices, "possible-duplicate-join")
        self.assertEqual(len(found), 1)
        self.assertIn("Ref.FxRates", found[0]["title"])
        self.assertEqual(issue_steps(self.prices, found[0]), ["2"])

    def test_payload_invariants(self):
        check_invariants(self, self.copy)
        check_invariants(self, self.prices)


@h.requires_parser
class ProjectMode(unittest.TestCase):
    """Calls expanded inline: INSERT ... EXEC, nested procedures, OUTPUT parameters."""

    @classmethod
    def setUpClass(cls):
        cls.repr = h.document([SCENARIOS / "02_insert_exec.sql"], project=True)
        cls.nested = h.document([SCENARIOS / "14_nested_calls.sql"], project=True)

    def test_insert_exec_expanded(self):
        p = self.repr["etl.usp_RepriceProducts"]
        self.assertEqual(p["mode"], "project")
        o = output(p, "dbo.Prices", "PriceZar")
        self.assertEqual(o["status"], "resolved")
        self.assertEqual(direct_sources(p, "dbo.Prices", "PriceZar"), {"dbo.Prices.Price", "Ref.FxRates.Rate"})
        self.assertEqual(step_labels(p, o["steps"]), ["3.2", "3.3", "4"])
        self.assertTrue(any(c["name"].lower() == "ref.usp_getrates" for c in p["calls"]))

    def test_nested_output_parameters(self):
        one = self.nested["crm.usp_ScoreOne"]
        self.assertEqual(step_labels(one, output(one, "dbo.CustomerScore", "Band")["steps"]), ["4.1", "4.2", "5"])
        self.assertEqual(output(one, "dbo.CustomerScore", "Band")["status"], "resolved")
        allp = self.nested["crm.usp_ScoreAll"]
        self.assertEqual(step_labels(allp, output(allp, "dbo.CustomerScore", "Score")["steps"]), ["4.3", "4.5"])
        self.assertEqual(step_labels(allp, output(allp, "dbo.CustomerScore", "Band")["steps"]),
                         ["4.4.1", "4.4.2", "4.5"], "two levels of expansion")

    def test_callee_issues_stay_in_callee(self):
        allp = self.nested["crm.usp_ScoreAll"]
        sid = {s["id"]: s for s in allp["steps"]}
        for i in allp["issues"]:
            for s in i.get("steps", []):
                if s in sid:
                    self.assertNotEqual(sid[s].get("origin"), "call", f"{i['rule']} belongs to a callee")

    def test_payload_invariants(self):
        for docs in (self.repr, self.nested):
            for name, p in docs.items():
                with self.subTest(procedure=name):
                    check_invariants(self, p)


def check_invariants(t: unittest.TestCase, p: dict) -> None:
    """Structural promises every document keeps, whatever the procedure."""
    t.assertEqual(p["schemaVersion"], SCHEMA_VERSION)
    ids = [s["id"] for s in p["steps"]]
    t.assertEqual(len(ids), len(set(ids)), "step ids are unique")
    labels = [s["label"] for s in p["steps"]]
    t.assertEqual(len(labels), len(set(labels)), "step labels are unique")
    n = len(p["nodes"])
    for node in p["nodes"]:
        for u in node.get("uses", []):
            for d in u["defs"]:
                t.assertTrue(0 <= d < n)
    for s in p["steps"]:
        for u in s["uses"]:
            for d in u["defs"]:
                t.assertTrue(0 <= d < n)
        for c in s["conditions"]:
            if c.get("step"):
                t.assertIn(c["step"], ids)
    rels = {r["key"] for r in p["relations"]} | {l["key"] for l in p.get("locals", [])}
    for o in p["outputs"]:
        t.assertIn(o["status"], ("resolved", "partial", "unresolved"))
        for d in o["defs"]:
            t.assertTrue(0 <= d < n)
        for sid in o["steps"]:
            t.assertIn(sid, ids)
        t.assertIn(o["rel"], rels)
    for i in p["issues"]:
        t.assertIn(i["severity"], ("high", "medium", "low", "info"))
        t.assertTrue(i["title"] and i["why"] and i["next"], f"{i['rule']} explains why and what to do next")
    t.assertIn("coverage", p)
    t.assertEqual(sum(p["summary"]["issues"].values()), len(p["issues"]))


if __name__ == "__main__":
    unittest.main()
