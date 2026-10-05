"""The page in a real browser: every view renders without script errors or sideways scrolling,
the in-page trace walk agrees with the Python one, and edited details download correctly.

Skipped when Playwright or Chromium is not available.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers as h  # noqa: E402
from helpers import LARGE, SCENARIOS  # noqa: E402

from sqldocgen.details import read_details  # noqa: E402
from sqldocgen.renderer import render_html  # noqa: E402
from sqldocgen.view import View  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:          # pragma: no cover
    sync_playwright = None

WALKS = """() => {
  const out = {};
  for (const o of DATA.outputs) {
    for (const ind of [true, false]) {
      const m = backward(o.defs, ind);
      out[o.key + "|" + ind] = [...m.entries()].map(([id, i]) => [id, i.direct, i.depth]).sort((a, b) => a[0] - b[0]);
    }
  }
  return out;
}"""


@unittest.skipUnless(sync_playwright, "Playwright is not installed")
@h.requires_parser
class InBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.pages = {}
        docs = {}
        docs.update(h.document([LARGE / "usp_LoadFactRevenue.sql"], schema=[LARGE / "schema.sql"]))
        docs.update(h.document([SCENARIOS / "12_dynamic_sql_resolvable.sql"]))
        docs.update(h.document([SCENARIOS / "13_dynamic_sql_unresolvable.sql"]))
        docs.update(h.document([SCENARIOS / "14_nested_calls.sql"], project=True))
        docs.update(h.document([SCENARIOS / "15_select_star.sql"]))
        cls.payloads = {}
        for name, p in docs.items():
            p = {k: v for k, v in p.items() if k != "_analysis"}
            cls.payloads[name] = p
            cls.pages[name] = render_html(p, root / f"{name}.html")
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:       # pragma: no cover
            cls.pw.stop()
            raise unittest.SkipTest(f"Chromium cannot start here: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.tmp.cleanup()

    def open(self, name, width=1400, height=900):
        page = self.browser.new_page(viewport={"width": width, "height": height}, accept_downloads=True)
        errors = []
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
        page.goto(self.pages[name].as_uri())
        page.wait_for_function("document.getElementById('main').innerHTML.length > 0")
        return page, errors

    def check_views(self, name, width):
        page, errors = self.open(name, width=width, height=900 if width > 600 else 844)
        try:
            views = page.evaluate("TABS.filter(t => t.avail()).map(t => t.id)")
            self.assertGreaterEqual(len(views), 12, views)
            for v in views:
                page.evaluate("v => switchTab(v)", v)
                text = page.evaluate("document.getElementById('main').innerText")
                self.assertGreater(len(text.strip()), 40, f"{name}: view {v} is empty")
                overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                self.assertLessEqual(overflow, 1, f"{name}: view {v} scrolls sideways at {width}px")
            self.assertEqual(errors, [], f"{name} at {width}px")
        finally:
            page.close()

    def test_every_view_desktop(self):
        for name in self.pages:
            with self.subTest(procedure=name):
                self.check_views(name, 1400)

    def test_every_view_phone(self):
        for name in ("etl.usp_LoadFactRevenue", "crm.usp_ScoreAll"):
            with self.subTest(procedure=name):
                self.check_views(name, 390)

    def test_trace_walk_parity(self):
        """The page and the Python exports (Word, CSV, agent, trace) walk lineage identically."""
        for name, p in self.payloads.items():
            with self.subTest(procedure=name):
                page, errors = self.open(name)
                try:
                    js = page.evaluate(WALKS)
                finally:
                    page.close()
                v = View(p)
                for o in p["outputs"]:
                    for ind in (True, False):
                        py = sorted([nid, i["direct"], i["depth"]] for nid, i in v.backward(o["defs"], ind).items())
                        self.assertEqual(js[f"{o['key']}|{str(ind).lower()}"], py, f"{o['key']} indirect={ind}")
                self.assertEqual(errors, [])

    def test_trace_view_opens_on_the_planted_bug(self):
        page, errors = self.open("etl.usp_LoadFactRevenue")
        try:
            page.evaluate("switchTab('trace')")
            text = page.evaluate("document.getElementById('main').innerText")
            self.assertIn("NetAmountZAR", text)
            self.assertIn("RateToZAR", text)
            self.assertIn("Check first", text, "the column's own review issue is surfaced on its trace")
            self.assertEqual(errors, [])
        finally:
            page.close()

    def test_details_edit_and_download(self):
        page, errors = self.open("etl.usp_TempTableChain" if "etl.usp_TempTableChain" in self.pages
                                 else "etl.usp_DynamicReprice")
        try:
            page.evaluate("switchTab('details')")
            page.fill("#det-owner", "Revenue data team")
            page.fill("#det-runbook", "https://wiki.example.com/runbooks/reprice")
            with page.expect_download() as dl:
                page.click("#det-save")
            path = dl.value.path()
            saved = Path(path).read_text(encoding="utf-8")
            details = read_details(saved)
            self.assertEqual(details["owner"], "Revenue data team")
            self.assertEqual(details["runbook"], "https://wiki.example.com/runbooks/reprice")
            self.assertIn("const DATA = ", saved, "the downloaded file is the whole document again")
            # a secret is refused in the page too
            page.fill("#det-notes", "server=prod;password=hunter2")
            status = page.evaluate("document.getElementById('det-status').innerText")
            disabled = page.evaluate("document.getElementById('det-save').disabled")
            self.assertTrue(disabled or "secret" in status.lower(), status)
            self.assertEqual(errors, [])
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
