"""Every output format, end to end through the command line, and no secret in any of them."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import re
import sys
import tempfile
import unittest
import xml.dom.minidom
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as h  # noqa: E402
from helpers import LARGE, SCENARIOS  # noqa: E402

from sqldocgen import SCHEMA_VERSION  # noqa: E402
from sqldocgen.cli import main  # noqa: E402
from sqldocgen.details import DETAILS_ID, read_details  # noqa: E402

SECRETS_SQL = """CREATE PROCEDURE ops.usp_Secrets
AS
BEGIN
    -- old login: CREATE LOGIN etl WITH PASSWORD = 'C0mmentPw!'
    INSERT INTO dbo.RemoteCopy (Id, Name)
    SELECT r.Id, r.Name
    FROM OPENROWSET('SQLNCLI', 'Server=prod01;UID=etl;PWD=Op3nR0wsetPw;', 'SELECT Id, Name FROM db.dbo.T') AS r;
    EXEC sp_addlinkedsrvlogin 'REMOTE', 'false', NULL, 'remoteuser', 'L1nkedPw#';
    DECLARE @sql nvarchar(max) = N'CREATE LOGIN app WITH PASSWORD = ''Dyn4micPw''';
    EXEC (@sql);
    SELECT 'Bearer abcdefghijklmnopqrstuvwxyz123456' AS hdr;
END
"""
SECRET_VALUES = ["C0mmentPw!", "Op3nR0wsetPw", "L1nkedPw#", "Dyn4micPw", "abcdefghijklmnopqrstuvwxyz123456"]


def run_cli(*argv) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def embedded_payload(html: str) -> dict:
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.S)
    if not m:
        raise AssertionError("payload not found in the page")
    return json.loads(m.group(1))


def all_text(path: Path) -> str:
    """Readable text of an output file (a .docx is unzipped)."""
    if path.suffix == ".docx":
        with zipfile.ZipFile(path) as z:
            return "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
    return path.read_text(encoding="utf-8-sig")


@h.requires_parser
class AllOutputs(unittest.TestCase):
    """The large demo with every export switched on."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "docs"
        cls.code, cls.stdout, cls.stderr = run_cli(
            LARGE / "usp_LoadFactRevenue.sql", "--schema", LARGE / "schema.sql", "--output-dir", cls.out,
            "--json", "--csv", "--word", "--agent", "--trace", "dbo.FactRevenue.NetAmountZAR")
        cls.html_path = cls.out / "etl.usp_LoadFactRevenue.html"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_exit_code_and_files(self):
        self.assertEqual(self.code, 0, self.stderr)
        names = sorted(p.name for p in self.out.rglob("*") if p.is_file())
        for want in ("etl.usp_LoadFactRevenue.html", "etl.usp_LoadFactRevenue.json", "etl.usp_LoadFactRevenue.docx",
                     "etl.usp_LoadFactRevenue.agent.md", "columns.csv", "edges.csv", "steps.csv", "issues.csv",
                     "trace-etl.usp_LoadFactRevenue-dbo.FactRevenue.NetAmountZAR.html",
                     "trace-etl.usp_LoadFactRevenue-dbo.FactRevenue.NetAmountZAR.md"):
            self.assertIn(want, names)
        self.assertFalse((self.out / "sql-home.html").exists(), "no home page for a single procedure")

    def test_html_is_self_contained(self):
        html = self.html_path.read_text(encoding="utf-8")
        self.assertNotRegex(html, r"<script[^>]+src=", "no external scripts: the page works offline")
        self.assertNotRegex(html, r"<link[^>]+href=[\"']https?:", "no external stylesheets")
        self.assertIn("<title>etl.usp_LoadFactRevenue", html)
        p = embedded_payload(html)
        self.assertEqual(p["schemaVersion"], SCHEMA_VERSION)
        self.assertEqual(p["documentationFilename"], "etl.usp_LoadFactRevenue.html")
        self.assertIn('protocol: "bi-doc-viewer", version: 1', html, "the library shell protocol is kept")
        self.assertIn(f'id="{DETAILS_ID}"', html)
        self.assertNotIn("</script><", json.dumps(p), "the payload cannot close its script block")

    def test_json_matches_page(self):
        p = json.loads((self.out / "etl.usp_LoadFactRevenue.json").read_text(encoding="utf-8"))
        page = embedded_payload(self.html_path.read_text(encoding="utf-8"))
        self.assertEqual(p["summary"], page["summary"])
        self.assertEqual(len(p["steps"]), len(page["steps"]))

    def test_csv(self):
        folder = self.out / "etl.usp_LoadFactRevenue-csv"
        with (folder / "columns.csv").open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        net = [r for r in rows if r["output"] == "dbo.FactRevenue" and r["column"] == "NetAmountZAR"]
        self.assertEqual(len(net), 1)
        self.assertIn("ref.FxRates.RateToZAR", net[0]["direct_sources"])
        with (folder / "issues.csv").open(encoding="utf-8-sig", newline="") as f:
            issues = list(csv.DictReader(f))
        self.assertEqual(issues[0]["severity"], "high")
        self.assertIn("factor-applied-twice", issues[0]["rule"])
        for name in ("edges.csv", "steps.csv"):
            with (folder / name).open(encoding="utf-8-sig", newline="") as f:
                self.assertGreater(len(list(csv.reader(f))), 10, name)
        with (folder / "steps.csv").open(encoding="utf-8-sig", newline="") as f:
            steps = list(csv.DictReader(f))
        self.assertEqual({r["role"] for r in steps}, {"logic", "housekeeping"})
        self.assertIn("row count", {r["housekeeping_reason"] for r in steps})
        self.assertTrue(any(r["section"].startswith("5 Conversion to rand") for r in steps))

    def test_word_document_is_valid(self):
        with zipfile.ZipFile(self.out / "etl.usp_LoadFactRevenue.docx") as z:
            names = set(z.namelist())
            self.assertTrue({"[Content_Types].xml", "word/document.xml", "_rels/.rels"} <= names)
            for n in names:
                if n.endswith(".xml") or n.endswith(".rels"):
                    xml.dom.minidom.parseString(z.read(n))          # every part is well-formed XML
            body = z.read("word/document.xml").decode("utf-8")
        self.assertIn("etl.usp_LoadFactRevenue", body)
        self.assertIn("multiplied by #FxDaily.RateToZAR again", body, "the review issues are in the handover")
        self.assertIn("housekeeping statements (row counts, log entries", body, "the step list leaves bookkeeping out")
        self.assertNotIn("Keeps the row count in", body)

    def test_agent_markdown(self):
        text = (self.out / "etl.usp_LoadFactRevenue.agent.md").read_text(encoding="utf-8")
        for heading in ("## Claims and limits (read first)", "## Review issues (ranked)", "## Output column lineage"):
            self.assertIn(heading, text)
        self.assertIn("| housekeeping: row count |", text, "agents see every statement, with its role")
        self.assertIn("agent context ~", self.stderr)

    def test_trace_export(self):
        md = (self.out / "trace-etl.usp_LoadFactRevenue-dbo.FactRevenue.NetAmountZAR.md").read_text(encoding="utf-8")
        self.assertIn("ref.FxRates.RateToZAR", md)
        self.assertIn("@IncludeAdjustments = 1", md)
        html = (self.out / "trace-etl.usp_LoadFactRevenue-dbo.FactRevenue.NetAmountZAR.html").read_text(encoding="utf-8")
        self.assertNotRegex(html, r"<script[^>]+src=")


@h.requires_parser
class Secrets(unittest.TestCase):
    """Connection strings, linked-server logins, passwords in dynamic SQL and comments never leave masked."""

    def test_no_secret_in_any_output(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "usp_Secrets.sql"
            src.write_text(SECRETS_SQL, encoding="utf-8")
            out = Path(d) / "docs"
            code, _, err = run_cli(src, "--output-dir", out, "--json", "--csv", "--word", "--agent",
                                   "--trace", "dbo.RemoteCopy.Name")
            self.assertEqual(code, 0, err)
            files = [p for p in out.rglob("*") if p.is_file()]
            self.assertGreaterEqual(len(files), 9)
            for p in files:
                text = all_text(p)
                for secret in SECRET_VALUES:
                    self.assertNotIn(secret, text, f"{secret} leaked into {p.name}")
            page = embedded_payload((out / "ops.usp_Secrets.html").read_text(encoding="utf-8"))
            self.assertGreaterEqual(page["redactions"], 5)
            secrets_row = [c for c in page["coverage"] if "ecret" in c["check"]]
            self.assertTrue(secrets_row, "the coverage table says secrets were masked")


@h.requires_parser
class DetailsAndLibrary(unittest.TestCase):
    def test_details_survive_regeneration_and_refuse_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            details = d / "details.json"
            details.write_text(json.dumps({"owner": "Revenue data team", "job": "Nightly 02:00"}), encoding="utf-8")
            src = SCENARIOS / "01_temp_table_chain.sql"
            code, _, err = run_cli(src, "--output-dir", d / "docs", "--details", details)
            self.assertEqual(code, 0, err)
            page = d / "docs" / "etl.usp_TempTableChain.html"
            self.assertEqual(read_details(page.read_text(encoding="utf-8"))["owner"], "Revenue data team")
            code, _, err = run_cli(src, "--output-dir", d / "docs")
            self.assertEqual(code, 0, err)
            self.assertEqual(read_details(page.read_text(encoding="utf-8"))["job"], "Nightly 02:00",
                             "regenerating keeps the maintained details")
            details.write_text(json.dumps({"notes": "server=x;password=hunter2"}), encoding="utf-8")
            code, _, err = run_cli(src, "--output-dir", d / "docs", "--details", details)
            self.assertEqual(code, 2)
            self.assertIn("secret", err)

    def test_library_home_page(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "docs"
            code, stdout, err = run_cli(SCENARIOS / "14_nested_calls.sql", SCENARIOS / "11_try_catch_transactions.sql",
                                        "--output-dir", out)
            self.assertEqual(code, 0, err)
            home = (out / "sql-home.html").read_text(encoding="utf-8")
            m = re.search(r"const DOCS = (\[.*?\]);\n", home, re.S)
            docs = json.loads(m.group(1))
            self.assertEqual(sorted(x["title"] for x in docs),
                             ["crm.usp_BandFor", "crm.usp_ScoreAll", "crm.usp_ScoreOne", "crm.usp_SnapshotCustomers",
                              "crm.usp_UnsafeTransaction"])
            batch = json.loads((out / "sql-batch-results.json").read_text(encoding="utf-8"))
            self.assertEqual({x["status"] for x in batch["documents"]}, {"ok"})
            self.assertEqual(batch["mode"], "project")
            self.assertIn('${pl(c.statements, "statement")}', home)
            # rebuild the home page alone
            (out / "sql-home.html").unlink()
            code, _, err = run_cli("--hub", out)
            self.assertEqual(code, 0, err)
            self.assertTrue((out / "sql-home.html").exists())

    def test_doctor_and_errors(self):
        code, out, _ = run_cli("--doctor")
        self.assertEqual(code, 0)
        self.assertIn("ScriptDom", out)
        code, _, err = run_cli("no-such-file.sql")
        self.assertEqual(code, 2)
        self.assertIn("not found", err)


if __name__ == "__main__":
    unittest.main()
