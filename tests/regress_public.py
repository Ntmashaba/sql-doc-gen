#!/usr/bin/env python3
"""Regression run over public T-SQL (a development tool, not part of the unit tests).

    python tests/regress_public.py                 # clone into ./.public-sql, document, print a table
    python tests/regress_public.py --docs out      # also write the HTML documents to ./out

Pinned commits of MIT-licensed public repositories are fetched (shallow, sparse) into a cache
folder, every procedure is documented, and a table is printed: procedures, failures, output
columns that are not fully resolved, dynamic SQL that could not be read, review issues by
severity and seconds. Exits 1 if any procedure fails to document.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqldocgen import scriptdom  # noqa: E402
from sqldocgen.analyzer import analyze  # noqa: E402
from sqldocgen.inputs import collect  # noqa: E402
from sqldocgen.payload import build  # noqa: E402
from sqldocgen.renderer import finalize, render_html  # noqa: E402

REPOS = {
    "sql-server-samples": ("https://github.com/microsoft/sql-server-samples",
                           "beaab06ef72831089ca80e5355d65e661fd19b26"),
    "first-responder-kit": ("https://github.com/BrentOzarULTD/SQL-Server-First-Responder-Kit",
                            "f04cf16b570536f8b0901ddd8947b148a17c3319"),
    "maintenance-solution": ("https://github.com/olahallengren/sql-server-maintenance-solution",
                             "41617578643e722b790ea4d57ef80f8de38ae747"),
}
# (name, repository, inputs inside it, folders to check out)
TARGETS = [
    ("WideWorldImporters (SSDT project)", "sql-server-samples",
     ["samples/databases/wide-world-importers/wwi-ssdt/wwi-ssdt/WideWorldImporters.sqlproj"],
     ["samples/databases/wide-world-importers/wwi-ssdt"]),
    ("AdventureWorks install script", "sql-server-samples",
     ["samples/databases/adventure-works/oltp-install-script/instawdb.sql"],
     ["samples/databases/adventure-works/oltp-install-script"]),
    ("Ola Hallengren maintenance solution", "maintenance-solution", ["MaintenanceSolution.sql"], None),
    ("sp_Blitz", "first-responder-kit", ["sp_Blitz.sql"], None),
    ("sp_BlitzCache", "first-responder-kit", ["sp_BlitzCache.sql"], None),
    ("sp_BlitzIndex", "first-responder-kit", ["sp_BlitzIndex.sql"], None),
]


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def fetch(cache: Path, repo: str, sparse) -> Path:
    url, sha = REPOS[repo]
    dest = cache / repo
    marker = dest / ".sqldocgen-commit"
    if marker.exists() and marker.read_text().strip() == sha:
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        git("init", "-q", cwd=dest)
        git("remote", "add", "origin", url, cwd=dest)
    folders = sorted({f for _, r, _, s in TARGETS if r == repo and s for f in s})
    if folders:
        git("sparse-checkout", "set", *folders, cwd=dest)
    print(f"fetching {url} @ {sha[:10]} ...", file=sys.stderr)
    git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", sha, cwd=dest)
    git("checkout", "-q", "FETCH_HEAD", cwd=dest)
    marker.write_text(sha)
    return dest


def run(name, paths, docs_dir=None):
    def parse_fn(items):
        return scriptdom.parse(items)

    t0 = time.time()
    inp = collect([str(p) for p in paths], [], parse_fn)
    stats = Counter()
    failures = []
    for unit, src in inp.units:
        try:
            a = analyze(unit, inp.catalog, lambda items: parse_fn(items)["results"], project=inp.mode == "project")
            p = finalize(build(a, title=unit.display, mode=inp.mode, source_path=src.path, inputs_meta=inp.parser,
                               callers=[], secrets=src.secrets, parse_errors=src.errors,
                               catalog_sources=inp.catalog.sources, catalog_size=len(inp.catalog.tables())))
        except Exception as exc:
            failures.append((unit.display, f"{type(exc).__name__}: {exc}"))
            traceback.print_exc()
            continue
        stats["procedures"] += 1
        stats["statements"] += p["summary"]["counts"]["statements"]
        for o in p["outputs"]:
            if o["col"] == "(rows)":
                continue
            stats["columns"] += 1
            stats["not resolved"] += o["status"] != "resolved"
        stats["dynamic"] += len(p["dynamic"])
        stats["dynamic unread"] += sum(1 for d in p["dynamic"] if not d["parsed"])
        for sev, n in p["summary"]["issues"].items():
            stats[sev] += n
        stats["check failures"] += sum(1 for i in p["issues"] if i["rule"] == "check-failed")
        if docs_dir:
            render_html(p, Path(docs_dir) / name.split(" (")[0].replace(" ", "_") / f"{unit.display}.html")
    stats["seconds"] = round(time.time() - t0, 1)
    return stats, failures


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / ".public-sql"), help="where the public repositories are fetched")
    ap.add_argument("--docs", help="also write the HTML documents here")
    ap.add_argument("--only", action="append", default=[], help="run only targets whose name contains this text")
    args = ap.parse_args(argv)
    cache = Path(args.cache)
    cols = ["procedures", "statements", "columns", "not resolved", "dynamic", "dynamic unread", "high", "medium",
            "low", "info", "check failures", "seconds"]
    print("| target | " + " | ".join(cols) + " | failures |")
    print("|---|" + "---:|" * len(cols) + "---|")
    failed = 0
    for name, repo, inputs, sparse in TARGETS:
        if args.only and not any(o.lower() in name.lower() for o in args.only):
            continue
        base = fetch(cache, repo, sparse)
        stats, failures = run(name, [base / i for i in inputs], args.docs)
        failed += len(failures)
        print(f"| {name} | " + " | ".join(str(stats.get(c, 0)) for c in cols) + f" | {len(failures)} |")
        for proc, err in failures:
            print(f"|   FAILED {proc}: {err} |", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
