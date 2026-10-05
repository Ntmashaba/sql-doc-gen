"""Command line: sql-doc-gen INPUT ... [options]

One self-contained HTML document per procedure, plus optional JSON, CSV, Word, agent
markdown and column-trace exports; a library home page (sql-home.html) when several
procedures are documented together.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__, scriptdom
from .analyzer import analyze
from .inputs import collect
from .payload import build
from .renderer import finalize, render_html

USAGE = """examples:
  sql-doc-gen usp_LoadFact.sql                          one procedure, procedure-only mode
  sql-doc-gen usp_LoadFact.sql --schema tables.sql      with table definitions (CREATE scripts, .dacpac or a column-list .csv)
  sql-doc-gen ./Database --output-dir docs              a folder or SSDT project: every procedure, calls expanded, plus sql-home.html
  sql-doc-gen usp_LoadFact.sql --trace dbo.FactRevenue.NetAmount   also export one column's trace (HTML + markdown)
  sql-doc-gen usp_LoadFact.sql --json --csv --word --agent
  sql-doc-gen --hub docs                                rebuild the library home page from the documents in a folder
  sql-doc-gen --doctor                                  what this machine needs (.NET, the ScriptDom helper)
"""


def _safe_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.\-]+", "_", name).strip("._") or "procedure"
    return s[:150]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sql-doc-gen", description="Living documentation and column lineage for T-SQL "
                                "stored procedures (static analysis with Microsoft ScriptDom).",
                                epilog=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="*", help=".sql files, folders, .sqlproj projects or .dacpac files")
    p.add_argument("-o", "--output", help="HTML output path (one procedure); default <schema.name>.html in --output-dir")
    p.add_argument("--output-dir", help="folder for every output (default: ./sql-docs)")
    p.add_argument("--schema", action="append", default=[], metavar="PATH",
                   help="table/view definitions: CREATE scripts, a folder, .sqlproj, .dacpac or column-list .csv (repeatable)")
    p.add_argument("--procedure", action="append", default=[], metavar="NAME",
                   help="document only these procedures (schema.name or name; repeatable)")
    p.add_argument("--title", help="document title (one procedure)")
    p.add_argument("--database", default="", help="database the code runs in, for three-part names")
    p.add_argument("--project", dest="project", action="store_true", default=None,
                   help="expand calls to procedures, views and functions found in the inputs (default for folders)")
    p.add_argument("--no-project", dest="project", action="store_false", help="never expand calls")
    p.add_argument("--json", action="store_true", help="also write the JSON payload")
    p.add_argument("--csv", nargs="?", const="", default=None, metavar="FOLDER",
                   help="also write columns, edges, steps and issues CSV files")
    p.add_argument("--word", action="store_true", help="also write a narrative Word (.docx) handover document")
    p.add_argument("--agent", action="store_true", help="also write compact markdown for an LLM agent (.agent.md)")
    p.add_argument("--trace", action="append", default=[], metavar="COLUMN",
                   help="also export one output column's trace as HTML and markdown, e.g. dbo.FactRevenue.Amount")
    p.add_argument("--details", metavar="FILE", help="procedure details JSON (owner, job, runbook...) to embed")
    p.add_argument("--parser", default="auto", help="ScriptDom parser, e.g. TSql160 (default: the newest available)")
    p.add_argument("--no-quoted-identifier", action="store_true", help='parse with QUOTED_IDENTIFIER OFF ("x" is a string)')
    p.add_argument("--hub", metavar="FOLDER", help="rebuild sql-home.html from the documents in a folder")
    p.add_argument("--doctor", action="store_true", help="check .NET and the ScriptDom helper")
    p.add_argument("--build-parser", action="store_true", help="build the ScriptDom helper now")
    p.add_argument("--dump-tree", metavar="FILE", help="print the ScriptDom JSON for one .sql file (debugging)")
    p.add_argument("--version", action="version", version=f"sql-doc-gen {__version__}")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.doctor:
        for check, result, advice in scriptdom.doctor():
            print(f"{check:<22} {result}" + (f"\n{'':<22} {advice}" if advice else ""))
        return 0
    if args.build_parser:
        try:
            path = scriptdom.helper_path(log=_log)
        except scriptdom.ParserUnavailable as exc:
            _log(str(exc))
            return 2
        print(f"ScriptDom helper ready: {path}")
        return 0
    if args.hub:
        from .hub import build_hub
        try:
            out = build_hub(args.hub)
        except ValueError as exc:
            _log(str(exc))
            return 2
        print(f"  wrote {out}")
        return 0
    if args.dump_tree:
        from .textutil import decode_sql_bytes
        text, _ = decode_sql_bytes(Path(args.dump_tree).read_bytes())
        res = scriptdom.parse([("tree", text)], parser=args.parser, quoted_identifier=not args.no_quoted_identifier)
        print(json.dumps(res["results"]["tree"], indent=1))
        return 0
    if not args.inputs:
        parse_args(["--help"])
        return 2
    return document(args)


def document(args: argparse.Namespace) -> int:
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out_dir = Path(args.output_dir or (Path(args.output).parent if args.output else "sql-docs"))
    quoted = not args.no_quoted_identifier

    def parse_fn(items):
        return scriptdom.parse(items, parser=args.parser, quoted_identifier=quoted, log=_log)

    t0 = time.time()
    try:
        inp = collect(args.inputs, args.schema, parse_fn, project=args.project, database=args.database)
    except scriptdom.ParserUnavailable as exc:
        _log(str(exc))
        return 2
    except FileNotFoundError as exc:
        _log(str(exc))
        return 2
    units = inp.units
    if args.procedure:
        wanted = {w.lower().strip("[]") for w in args.procedure}
        units = [(u, s) for u, s in units if u.display.lower() in wanted or u.name.lower() in wanted]
        if not units:
            _log("None of the procedures named with --procedure were found. Found: " +
                 ", ".join(u.display for u, _ in inp.units))
            return 2
    if not units:
        _log("No stored procedures or executable statements were found in the input.")
        return 2
    _log(f"Parsed {len(inp.sources)} file(s) with ScriptDom {inp.parser.get('scriptDom')} "
         f"({inp.parser.get('parser')}) in {time.time() - t0:.1f}s; mode: {inp.mode}; "
         f"{len(units)} procedure(s) to document")
    details = None
    if args.details:
        try:
            details = json.loads(Path(args.details).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            _log(f"Could not read --details: {exc}")
            return 2

    def inner_parse(items):
        return parse_fn(items)["results"]

    analyses = []
    results = []
    for unit, src in units:
        t = time.time()
        try:
            a = analyze(unit, inp.catalog, inner_parse, project=inp.mode == "project", default_db=args.database)
            analyses.append((a, src))
        except Exception as exc:  # one procedure failing must not stop the others
            _log(f"  FAILED {unit.display}: {type(exc).__name__}: {exc}")
            if "SQLDOCGEN_DEBUG" in __import__("os").environ:
                traceback.print_exc()
            results.append({"input": src.path, "procedure": unit.display, "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}"})
            continue
    # who calls whom (project mode)
    names = {}
    for a, src in analyses:
        names[a.unit.display.lower()] = a
        names[a.unit.name.lower()] = names.get(a.unit.name.lower(), a)
    callers: Dict[str, List[dict]] = {}
    files: Dict[str, str] = {}
    used_names = set()
    for a, src in analyses:
        base = _safe_name(a.unit.display if a.unit.kind == "procedure" else Path(src.path).stem)
        name = base
        k = 2
        while name.lower() in used_names:
            name = f"{base}-{k}"
            k += 1
        used_names.add(name.lower())
        files[id(a)] = name
    for a, src in analyses:
        for c in a.ctx.calls:
            if c["kind"] != "procedure":
                continue
            target = names.get(c["name"].lower()) or names.get(c["name"].split(".")[-1].lower())
            if target is not None and target is not a:
                lst = callers.setdefault(id(target), [])
                if not any(x["name"] == a.unit.display for x in lst):
                    lst.append({"name": a.unit.display, "file": src.path, "href": files[id(a)] + ".html"})
    single = len(analyses) == 1
    for a, src in analyses:
        unit = a.unit
        t = time.time()
        try:
            title = args.title if (args.title and single) else unit.display
            payload = build(a, title=title, mode=inp.mode, source_path=src.path, inputs_meta=inp.parser,
                            callers=callers.get(id(a), []), secrets=src.secrets, parse_errors=src.errors,
                            catalog_sources=inp.catalog.sources, catalog_size=len(inp.catalog.tables()))
            payload = finalize(payload)
            html_path = Path(args.output) if (args.output and single) else out_dir / f"{files[id(a)]}.html"
            render_html(payload, html_path, details=details)
            written = [html_path]
            stem = html_path.parent / html_path.name[: -len(html_path.suffix)] if html_path.suffix else html_path
            if args.json:
                jp = Path(str(stem) + ".json")
                jp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                written.append(jp)
            if args.csv is not None:
                from .csv_writer import write_csv
                folder = Path(args.csv) if args.csv else Path(str(stem) + "-csv")
                if args.csv and not single:
                    folder = folder / files[id(a)]
                written += write_csv(payload, folder)
            if args.word:
                from .word_writer import write_docx
                written.append(write_docx(payload, Path(str(stem) + ".docx")))
            if args.agent:
                from .agent_writer import write_agent
                ap, tokens = write_agent(payload, Path(str(stem) + ".agent.md"))
                written.append(ap)
                _log(f"  agent context ~{tokens:,} tokens")
            for want in args.trace:
                from .trace_export import find_output, write_trace
                key = find_output(payload, want)
                if key is None:
                    if single:
                        _log(f"  --trace {want}: no output column of {unit.display} matches "
                             f"(try relation.column, e.g. dbo.Table.Column)")
                    continue
                written += write_trace(payload, key, html_path.parent)
            for w in written:
                print(f"  wrote {w}")
            results.append({"input": src.path, "procedure": unit.display, "status": "ok", "output": html_path.name,
                            "mode": inp.mode, "seconds": round(time.time() - t, 2),
                            "issues": payload["summary"]["issues"]})
        except Exception as exc:
            _log(f"  FAILED {unit.display}: {type(exc).__name__}: {exc}")
            if "SQLDOCGEN_DEBUG" in __import__("os").environ:
                traceback.print_exc()
            results.append({"input": src.path, "procedure": unit.display, "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}"})
    if not single or len(units) > 1:
        from .hub import BATCH_RESULTS, build_hub
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / BATCH_RESULTS).write_text(json.dumps({"started": started, "generator": f"sql-doc-gen {__version__}",
                                                         "inputs": args.inputs, "mode": inp.mode,
                                                         "documents": results}, indent=1), encoding="utf-8")
        print(f"  wrote {build_hub(out_dir)}")
    failed = [r for r in results if r["status"] != "ok"]
    if failed:
        _log(f"{len(failed)} of {len(results)} procedure(s) could not be documented.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
