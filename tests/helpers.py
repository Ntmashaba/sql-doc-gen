"""Shared test setup: parse once per text, document inputs, find columns and issues."""
from __future__ import annotations

import hashlib
import os
import sys
import unittest
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqldocgen import scriptdom  # noqa: E402
from sqldocgen.analyzer import analyze  # noqa: E402
from sqldocgen.inputs import collect  # noqa: E402
from sqldocgen.payload import build  # noqa: E402
from sqldocgen.renderer import finalize  # noqa: E402

SCENARIOS = ROOT / "examples" / "scenarios"
SCHEMA = ROOT / "examples" / "schema"
LARGE = ROOT / "examples" / "large"
_CACHE: Dict[str, dict] = {}
_META: Dict[str, str] = {}
# CI sets this: a missing parser or browser is then a failure, never a silent skip.
STRICT = os.environ.get("SQLDOCGEN_STRICT_TESTS", "") not in ("", "0")


def parser_available() -> bool:
    try:
        scriptdom.helper_path(log=lambda m: None)
        return True
    except scriptdom.ParserUnavailable:
        return False


def requires_parser(cls):
    if STRICT:
        return cls
    return unittest.skipUnless(parser_available(), "the ScriptDom helper cannot be built here (.NET SDK missing?)")(cls)


def parse(items):
    """scriptdom.parse with a per-process cache keyed by text."""
    todo = []
    out = {}
    for iid, text in items:
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if h in _CACHE:
            out[iid] = _CACHE[h]
        else:
            todo.append((iid, text, h))
    if todo:
        res = scriptdom.parse([(i, t) for i, t, _ in todo])
        _META.update({k: v for k, v in res.items() if k != "results"})
        for iid, text, h in todo:
            _CACHE[h] = res["results"][iid]
            out[iid] = res["results"][iid]
    return {**_META, "results": out}


def document(paths: List[Path], schema: Optional[List[Path]] = None, project: Optional[bool] = None) -> Dict[str, dict]:
    """Payloads by procedure name."""
    inp = collect([str(p) for p in paths], [str(s) for s in (schema or [])], parse, project=project)
    out = {}
    for unit, src in inp.units:
        a = analyze(unit, inp.catalog, lambda items: parse(items)["results"], project=inp.mode == "project")
        p = build(a, title=unit.display, mode=inp.mode, source_path=src.path, inputs_meta=inp.parser, callers=[],
                  secrets=src.secrets, parse_errors=src.errors, catalog_sources=inp.catalog.sources,
                  catalog_size=len(inp.catalog.tables()))
        out[unit.display] = finalize(p)
        out[unit.display]["_analysis"] = a
    return out


def rel_name(p: dict, key: str) -> str:
    for r in p["relations"]:
        if r["key"] == key:
            return r["name"]
    return key


def output(p: dict, relation: str, column: str) -> dict:
    for o in p["outputs"]:
        if rel_name(p, o["rel"]).lower() == relation.lower() and o["col"].lower() == column.lower():
            return o
    raise AssertionError(f"no output column {relation}.{column}; have "
                         f"{sorted(rel_name(p, o['rel']) + '.' + o['col'] for o in p['outputs'])}")


def direct_sources(p: dict, relation: str, column: str) -> set:
    return {f"{rel_name(p, r)}.{c}" for r, c in output(p, relation, column)["direct"]}


def step_labels(p: dict, ids) -> List[str]:
    by = {s["id"]: s["label"] for s in p["steps"]}
    return [by[i] for i in ids]


def rules(p: dict) -> List[str]:
    return [i["rule"] for i in p["issues"]]


def document_sql(sql: str, schema_sql: Optional[str] = None, project: Optional[bool] = None,
                 name: str = "test.sql") -> Dict[str, dict]:
    """Document SQL text given inline (written to a temporary folder first)."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / name
        path.write_text(sql, encoding="utf-8")
        schema = None
        if schema_sql is not None:
            schema = [Path(d) / "schema_defs.sql"]
            schema[0].write_text(schema_sql, encoding="utf-8")
        return document([path], schema=schema, project=project)


def step_source(p: dict, step: dict) -> str:
    """The source text of a step (spans are UTF-16 offsets into the step's text slice)."""
    from sqldocgen.textutil import Utf16Map
    if not step.get("span"):
        return ""
    text = p["texts"][step["text"]]["text"]
    m = Utf16Map(text)
    a, b = step["span"]
    return text[m.to_cp(a):m.to_cp(b)]
