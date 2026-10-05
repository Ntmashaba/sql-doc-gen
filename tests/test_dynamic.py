"""Dynamic SQL: rebuilding the string, variants, placeholders and parameter binding."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as h  # noqa: E402
from helpers import direct_sources, output, step_labels  # noqa: E402

from sqldocgen.dynamic import parse_param_definitions  # noqa: E402

PROC = """
CREATE PROCEDURE dbo.usp_Dyn
    @Mode int, @Filter int = NULL, @TableName sysname, @Other sysname
AS
BEGIN
    DECLARE @sql nvarchar(max), @sql2 nvarchar(max), @n int, @count int, @stmt nvarchar(max);
    IF @Mode = 1
        SET @sql = N'INSERT INTO dbo.Archive (Id, Amount) SELECT o.Id, o.Amount FROM dbo.Orders AS o';
    ELSE
        SET @sql = N'INSERT INTO dbo.Archive (Id, Amount) SELECT r.Id, r.Amount * 2 FROM dbo.Returns AS r';
    SET @sql += N' WHERE 1 = 1';
    IF @Filter IS NOT NULL
        SET @sql = @sql + N' AND Id > @f';
    EXEC sp_executesql @sql, N'@f int', @f = @Filter;

    SET @sql2 = N'DELETE FROM ' + QUOTENAME(@TableName) + N' WHERE Id < 0';
    EXEC (@sql2);
    SET @sql2 = N'UPDATE ' + QUOTENAME(@Other) + N' SET Flag = 1';
    EXEC (@sql2);

    EXEC sp_executesql N'SELECT @n = COUNT(*) FROM dbo.Orders WHERE Amount > @min', N'@min money, @n int OUTPUT',
         @min = 10, @n = @count OUTPUT;
    INSERT INTO dbo.Counts (Total) VALUES (@count);

    SELECT @stmt = c.Body FROM dbo.Commands AS c WHERE c.Id = 1;
    EXEC (@stmt);
END
"""


@h.requires_parser
class Rebuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = h.document_sql(PROC)["dbo.usp_Dyn"]
        cls.dyn = {d["step"]: d for d in cls.p["dynamic"]}
        cls.by_label = {s["label"]: s for s in cls.p["steps"]}

    def step_of(self, needle):
        hits = [s for s in self.p["steps"] if s["kind"] == "exec-dynamic" and needle in h.step_source(self.p, s)]
        self.assertEqual(len(hits), 1, needle)
        return hits[0]["label"]

    def test_branches_and_appends_give_variants(self):
        d = self.dyn[self.step_of("@sql, N'@f int'")]
        self.assertEqual(d["status"], "resolved")
        self.assertEqual(len(d["variants"]), 4, "two IF/ELSE starts x optional filter")
        self.assertTrue(all(v.endswith(("WHERE 1 = 1", "AND Id > @f")) for v in d["variants"]))

    def test_variant_lineage_covers_both_sources(self):
        p = self.p
        self.assertEqual(direct_sources(p, "dbo.Archive", "Amount"), {"dbo.Orders.Amount", "dbo.Returns.Amount"})
        self.assertEqual(output(p, "dbo.Archive", "Amount")["status"], "resolved")

    def test_placeholders_named_per_statement(self):
        labels = sorted(l for l, d in self.dyn.items() if d["status"] == "partial")
        self.assertEqual(len(labels), 2)
        self.assertEqual([self.dyn[l]["placeholders"] for l in labels], [["@TableName"], ["@Other"]])
        names = {r["name"] for r in self.p["relations"] if r["kind"] == "dynamic"}
        self.assertEqual(names, {"(object named by @TableName)", "(object named by @Other)"},
                         "each run-time table name keeps its own label")

    def test_output_parameter_copied_back(self):
        p = self.p
        o = output(p, "dbo.Counts", "Total")
        labels = step_labels(p, o["steps"])
        self.assertEqual(len(labels), 3)
        self.assertTrue(labels[0].endswith(".1") and labels[1].endswith(".2"),
                        "the nested SELECT, then the copy back to @count, then the INSERT")
        self.assertIn("output parameters are copied back", self.by_label[labels[1]]["summary"])

    def test_string_read_from_table_is_unresolved(self):
        d = self.dyn[self.step_of("EXEC (@stmt)")]
        self.assertEqual(d["status"], "unresolved")
        self.assertIn("dynamic-sql-unresolved", [i["rule"] for i in self.p["issues"]])

    def test_append_described(self):
        self.assertTrue(any(s["summary"].startswith("Appends N' WHERE 1 = 1' to") for s in self.p["steps"]))


GENERATED = """
CREATE PROCEDURE dbo.usp_BuildTriggers
AS
BEGIN
    DECLARE @SQL nvarchar(max) = N'', @CrLf nchar(2) = NCHAR(13) + NCHAR(10), @Indent nvarchar(4) = N'    ';
    DECLARE @TableName sysname, @Audit sysname, @Cols nvarchar(max);
    SET @TableName = N'Orders';
    SET @Audit = N'';
    SET @Cols = N' [OrderId], [Amount],';
    SET @SQL = N'INSERT INTO dbo.' + QUOTENAME(@TableName + N'_Archive') + @CrLf
             + @Indent + N'(' + @Cols + CASE WHEN COALESCE(@Audit, N'') <> N'' THEN QUOTENAME(@Audit) + N', ' ELSE N'' END
             + N' [ArchivedAt])' + @CrLf
             + @Indent + N'SELECT' + @Cols + CASE WHEN COALESCE(@Audit, N'') <> N'' THEN N'o.' + QUOTENAME(@Audit) + N', ' ELSE N'' END
             + N' SYSDATETIME()' + @CrLf
             + @Indent + N'FROM dbo.' + QUOTENAME(@TableName) + N' AS o' + @CrLf
             + @Indent + N'WHERE o.[Amount] > 0' + @CrLf
             + @Indent + N'  AND o.[OrderId] > 0' + @CrLf
             + @Indent + N'  AND o.[OrderId] < 1000000' + @CrLf
             + @Indent + N'  AND o.[Amount] < 1000000' + @CrLf
             + @Indent + N'  AND 1 = 1' + @CrLf
             + @Indent + N'  AND 2 = 2' + @CrLf
             + @Indent + N'  AND 3 = 3' + @CrLf
             + @Indent + N'  AND 4 = 4' + @CrLf
             + @Indent + N'  AND 5 = 5' + @CrLf
             + N';';
    EXECUTE (@SQL);
END
"""


@h.requires_parser
class GeneratedDdl(unittest.TestCase):
    """The way admin procedures generate SQL: long + chains, CHAR(13), CASE on configuration variables."""

    @classmethod
    def setUpClass(cls):
        cls.p = h.document_sql(GENERATED)["dbo.usp_BuildTriggers"]

    def test_long_chain_is_rebuilt_exactly(self):
        d = self.p["dynamic"][0]
        self.assertEqual(d["status"], "resolved")
        self.assertEqual(len(d["variants"]), 1, "the CASE condition is decided by SET @Audit = N''")
        v = d["variants"][0]
        self.assertTrue(v.startswith("INSERT INTO dbo.[Orders_Archive]\r\n    ( [OrderId], [Amount], [ArchivedAt])"), v[:80])
        self.assertIn("AND 5 = 5", v, "more than 40 pieces are joined without a placeholder")

    def test_lineage_through_the_generated_statement(self):
        self.assertEqual(direct_sources(self.p, "dbo.Orders_Archive", "Amount"), {"dbo.Orders.Amount"})
        self.assertNotIn("dynamic-sql-unresolved", [i["rule"] for i in self.p["issues"]])


@h.requires_parser
class VariantsThatDoNotParse(unittest.TestCase):
    def test_bad_variant_dropped_good_one_kept(self):
        sql = """CREATE PROCEDURE dbo.usp_TwoWays @Flag bit AS
BEGIN
    DECLARE @sql nvarchar(max);
    IF @Flag = 1 SET @sql = N'UPDATE dbo.T SET Amount = 0 WHERE';      -- broken on purpose
    ELSE SET @sql = N'UPDATE dbo.T SET Amount = 1';
    EXEC (@sql);
END"""
        p = h.document_sql(sql)["dbo.usp_TwoWays"]
        d = p["dynamic"][0]
        self.assertEqual(d["status"], "partial", "one of the two strings cannot be parsed")
        self.assertTrue(d["parsed"])
        self.assertEqual(d["variantsParsed"], 1)
        self.assertTrue(d["errors"])
        self.assertNotIn("dynamic-sql-unresolved", [i["rule"] for i in p["issues"]])
        self.assertIn("Amount", {o["col"] for o in p["outputs"] if h.rel_name(p, o["rel"]) == "dbo.T"})


UNSAFE = r"""CREATE PROCEDURE dbo.usp_Search @Name nvarchar(100), @Table sysname, @Id int AS
BEGIN
    DECLARE @sql nvarchar(max) = N'SELECT * FROM dbo.' + @Table + N' WHERE Name = ''' + @Name + N''' AND Id = '
                                 + CAST(@Id AS nvarchar(20));
    EXEC (@sql);
END"""

SAFE = r"""CREATE PROCEDURE dbo.usp_SafeSearch @Name nvarchar(100), @Table sysname AS
BEGIN
    DECLARE @sql nvarchar(max) = N'SELECT * FROM dbo.' + QUOTENAME(@Table) + N' WHERE Name = @n';
    EXEC sp_executesql @sql, N'@n nvarchar(100)', @n = @Name;
    SET @sql = N'SELECT * FROM dbo.T WHERE Name = N''' + REPLACE(@Name, '''', '''''') + N'''';
    EXEC (@sql);
END"""


@h.requires_parser
class Injection(unittest.TestCase):
    def test_parameter_pasted_into_dynamic_sql(self):
        p = h.document_sql(UNSAFE)["dbo.usp_Search"]
        found = [i for i in p["issues"] if i["rule"] == "sql-injection-risk"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "high")
        self.assertIn("@Table and @Name", found[0]["title"])
        self.assertNotIn("@Id", found[0]["title"], "a number cannot carry SQL")

    def test_safe_forms_are_not_flagged(self):
        p = h.document_sql(SAFE)["dbo.usp_SafeSearch"]
        self.assertNotIn("sql-injection-risk", [i["rule"] for i in p["issues"]])


class ParamDefinitions(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_param_definitions("@from date, @n int OUTPUT, @name nvarchar(50) = N'x', "
                                                 "@x decimal(18, 2) OUT, @t dbo.IdList READONLY"),
                         [{"name": "@from", "type": "date", "output": False, "default": ""},
                          {"name": "@n", "type": "int", "output": True, "default": ""},
                          {"name": "@name", "type": "nvarchar(50)", "output": False, "default": "N'x'"},
                          {"name": "@x", "type": "decimal(18, 2)", "output": True, "default": ""},
                          {"name": "@t", "type": "dbo.IdList", "output": False, "default": ""}])


if __name__ == "__main__":
    unittest.main()
