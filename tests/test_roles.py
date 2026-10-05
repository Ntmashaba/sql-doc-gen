"""What each statement is for (logic or housekeeping) and the procedure's own section outline.

The Steps view folds housekeeping (row counts, log rows, debug output, declarations) so the
logic reads straight through, and groups statements under the authors' section comments.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as h  # noqa: E402
from helpers import LARGE  # noqa: E402

from sqldocgen.outline import headings  # noqa: E402

LOAD = r"""
CREATE PROCEDURE etl.usp_LoadTarget
    @Debug bit = 0
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @Rows int;
    DECLARE @Cutoff date = DATEADD(DAY, -7, CAST(SYSDATETIME() AS date));

    IF @Debug = 1
        SELECT @Cutoff AS Cutoff;

    INSERT INTO dbo.Target (Id, Amount)
    SELECT s.Id, s.Amount
    FROM dbo.Source AS s
    WHERE s.LoadedOn >= @Cutoff;
    SET @Rows = @@ROWCOUNT;

    INSERT INTO etl.LoadLog (Step, RowsAffected, LoggedAt)
    VALUES ('load target', @Rows, SYSDATETIME());

    PRINT 'done';
    RETURN 0;
END
"""

AUDIT_READ_BACK = r"""
CREATE PROCEDURE dbo.usp_Reconcile
AS
BEGIN
    INSERT INTO dbo.AuditTrail (Id, Amount)
    SELECT s.Id, s.Amount FROM dbo.Source AS s;

    UPDATE t SET t.Checked = 1
    FROM dbo.Target AS t
    JOIN dbo.AuditTrail AS a ON a.Id = t.Id;
END
"""

OUTLINE = r"""
CREATE PROCEDURE dbo.usp_Outline
AS
BEGIN
    /* ==== 1. Stage ==== */
    -- 1.1 Pull the rows
    SELECT s.Id, s.Amount INTO #s FROM dbo.Source AS s;   -- 2.9 not a heading: it trails code
    -- 1.2 Drop empty amounts
    -- 1 row per order line is kept
    DELETE FROM #s WHERE Amount IS NULL;

    /* ==== 2. Publish ==== */
    INSERT INTO dbo.Target (Id, Amount) SELECT Id, Amount FROM #s;
END
"""

ONE_HEADING = r"""
CREATE PROCEDURE dbo.usp_Short
AS
BEGIN
    -- 1. Only section
    INSERT INTO dbo.Target (Id) SELECT Id FROM dbo.Source;
END
"""


def step_with(p: dict, needle: str) -> dict:
    hits = [s for s in p["steps"] if needle.lower() in h.step_source(p, s).lower()]
    if not hits:
        raise AssertionError(f"no step contains {needle!r}")
    return min(hits, key=lambda s: len(h.step_source(p, s)))


def relation(p: dict, name: str) -> dict:
    for r in p["relations"]:
        if r["name"].lower() == name.lower():
            return r
    raise AssertionError(f"no relation {name}")


@h.requires_parser
class Roles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = h.document_sql(LOAD)["etl.usp_LoadTarget"]

    def role(self, needle):
        st = step_with(self.p, needle)
        return st["role"], st["why"]

    def test_logic_statements(self):
        for needle in ("INSERT INTO dbo.Target", "DECLARE @Cutoff", "RETURN 0"):
            with self.subTest(needle):
                self.assertEqual(self.role(needle)[0], "logic")

    def test_housekeeping_with_reasons(self):
        expect = {"SET NOCOUNT ON": "setting", "DECLARE @Rows int": "declaration",
                  "SELECT @Cutoff AS Cutoff": "debug output", "SET @Rows = @@ROWCOUNT": "row count",
                  "INSERT INTO etl.LoadLog": "log entry", "PRINT 'done'": "message"}
        for needle, why in expect.items():
            with self.subTest(needle):
                self.assertEqual(self.role(needle), ("housekeeping", why))

    def test_log_table_is_not_a_business_output(self):
        self.assertTrue(relation(self.p, "etl.LoadLog")["logging"])
        self.assertFalse(relation(self.p, "dbo.Target")["logging"])
        counts = self.p["counts"]
        self.assertEqual(counts["logging"], 1)
        self.assertEqual(counts["logic"] + counts["housekeeping"], counts["statements"])
        self.assertIn("logs its progress to `etl.LoadLog`", self.p["what"])

    def test_folded_statements_get_plain_summaries(self):
        self.assertEqual(step_with(self.p, "SET @Rows = @@ROWCOUNT")["summary"], "Keeps the row count in `@Rows`.")
        self.assertEqual(step_with(self.p, "INSERT INTO etl.LoadLog")["summary"], "Logs “load target” to `etl.LoadLog`.")

    def test_a_table_read_back_is_not_a_log(self):
        """Named like a log, but filled from data and joined later: that is business data."""
        p = h.document_sql(AUDIT_READ_BACK)["dbo.usp_Reconcile"]
        self.assertFalse(relation(p, "dbo.AuditTrail")["logging"])
        self.assertEqual(step_with(p, "INSERT INTO dbo.AuditTrail")["role"], "logic")
        self.assertEqual(p["counts"]["housekeeping"], 0)


class Headings(unittest.TestCase):
    def test_numbered_and_banner_headings(self):
        text = OUTLINE
        found = headings(text, text.index("BEGIN"), len(text))
        self.assertEqual([(x["number"], x["title"], x["level"]) for x in found],
                         [("1", "Stage", 1), ("1.1", "Pull the rows", 2), ("1.2", "Drop empty amounts", 2),
                          ("2", "Publish", 1)])

    def test_prose_and_trailing_comments_are_not_headings(self):
        text = "BEGIN\n  -- 1 row per order line is kept\n  SELECT 1; -- 2.1 trailing\n  -- Step 3: merge\nEND"
        self.assertEqual([x["number"] for x in headings(text, 0, len(text))], ["3"])


@h.requires_parser
class Outline(unittest.TestCase):
    def test_steps_belong_to_their_sections(self):
        p = h.document_sql(OUTLINE)["dbo.usp_Outline"]
        secs = {s["id"]: s for s in p["sections"]}
        self.assertEqual([(s["number"], s["title"], s["level"]) for s in p["sections"]],
                         [("1", "Stage", 1), ("1.1", "Pull the rows", 2), ("1.2", "Drop empty amounts", 2),
                          ("2", "Publish", 1)])
        by_number = {s["number"]: s["id"] for s in p["sections"]}
        self.assertEqual(secs[by_number["1.1"]]["parent"], by_number["1"])
        self.assertIsNone(secs[by_number["2"]]["parent"])
        self.assertEqual(step_with(p, "INTO #s FROM")["section"], by_number["1.1"])
        self.assertEqual(step_with(p, "DELETE FROM #s")["section"], by_number["1.2"])
        self.assertEqual(step_with(p, "INSERT INTO dbo.Target")["section"], by_number["2"])

    def test_one_heading_is_no_outline(self):
        p = h.document_sql(ONE_HEADING)["dbo.usp_Short"]
        self.assertEqual(p["sections"], [])
        self.assertTrue(all(s["section"] is None for s in p["steps"]))


@h.requires_parser
class LargeDemoRoles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = h.document([LARGE / "usp_LoadFactRevenue.sql"], schema=[LARGE / "schema.sql"])["etl.usp_LoadFactRevenue"]

    def test_bookkeeping_is_folded(self):
        c = self.p["counts"]
        self.assertGreater(c["housekeeping"], 100, "row counts, log rows and debug output after every section")
        self.assertGreater(c["logic"], c["housekeeping"])
        whys = {s["why"] for s in self.p["steps"] if s["role"] == "housekeeping"}
        self.assertTrue({"row count", "log entry", "debug output", "declaration"} <= whys, whys)

    def test_log_tables(self):
        logs = sorted(r["name"] for r in self.p["relations"] if r["logging"])
        self.assertEqual(logs, ["etl.ErrorLog", "etl.LoadBatch", "etl.LoadLog"])
        self.assertFalse(relation(self.p, "dbo.FactRevenue")["logging"])

    def test_planted_bug_is_logic_under_its_section(self):
        bug = next(i for i in self.p["issues"] if i["rule"] == "factor-applied-twice")
        secs = {s["id"]: s for s in self.p["sections"]}
        for sid in bug["steps"]:
            st = next(s for s in self.p["steps"] if s["id"] == sid)
            self.assertEqual(st["role"], "logic")
            self.assertIsNotNone(st["section"])
        self.assertTrue(any(s["title"].startswith("Conversion to rand") for s in secs.values()))


if __name__ == "__main__":
    unittest.main()
