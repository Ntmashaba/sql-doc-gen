"""Collect the inputs of a run: SQL files, folders, SSDT projects, DACPACs and column lists.

Every SQL text is read once, secrets are masked in place, and all texts are parsed with one
call to the ScriptDom helper. Definitions from every file feed the catalog; procedures are
documented only from the inputs (not from the --schema files).
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .analyzer import Unit, find_units
from .catalog import Catalog, collect_from_tree, load_columns_csv, load_dacpac
from .syntax import Text
from .textutil import decode_sql_bytes, mask_secrets

SKIP_DIRS = {".git", ".vs", ".vscode", "bin", "obj", "node_modules", ".venv", "__pycache__", "documentation"}
SQL_SUFFIXES = {".sql"}
COLUMN_LIST_SUFFIXES = {".csv", ".tsv"}


@dataclass
class SourceText:
    id: str
    path: str                     # as shown in documents (relative where possible)
    text: Text
    encoding: str = "utf-8"
    secrets: int = 0
    role: str = "input"           # input | schema
    tree: Optional[dict] = None
    errors: List[dict] = field(default_factory=list)


@dataclass
class Inputs:
    sources: List[SourceText]
    catalog: Catalog
    units: List[Tuple[Unit, SourceText]]
    mode: str                     # procedure | schema | project
    input_kinds: List[str]
    parser: Dict[str, str]
    unreadable: List[Tuple[str, str]] = field(default_factory=list)
    root: Optional[Path] = None

    @property
    def secrets(self) -> int:
        return sum(s.secrets for s in self.sources)


def _walk_sql(folder: Path) -> List[Path]:
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(files):
            if Path(f).suffix.lower() in SQL_SUFFIXES:
                out.append(Path(root) / f)
    return out


def _sqlproj_files(proj: Path) -> List[Path]:
    """Files an SSDT project builds (<Build Include="...">), or every .sql under it."""
    try:
        root = ET.parse(proj).getroot()
    except ET.ParseError:
        return _walk_sql(proj.parent)
    files = []
    for el in root.iter():
        if el.tag.split("}")[-1] == "Build" and el.get("Include"):
            inc = el.get("Include").replace("\\", "/")
            if any(ch in inc for ch in "*?"):
                files += [p for p in proj.parent.glob(inc) if p.suffix.lower() == ".sql"]
            else:
                p = proj.parent / inc
                if p.suffix.lower() == ".sql" and p.exists():
                    files.append(p)
    # SDK-style projects include every .sql by default
    if not files:
        files = _walk_sql(proj.parent)
    return sorted(set(files))


def _read(path: Path) -> Tuple[str, str]:
    return decode_sql_bytes(path.read_bytes())


def collect(inputs: List[str], schema: List[str], parse_fn: Callable, project: Optional[bool] = None,
            database: str = "") -> Inputs:
    catalog = Catalog()
    if database:
        catalog.database = database
    sources: List[SourceText] = []
    unreadable: List[Tuple[str, str]] = []
    kinds: List[str] = []
    folder_like = False
    root: Optional[Path] = None

    def add_sql(path: Path, role: str, base: Optional[Path]):
        try:
            raw, enc = _read(path)
        except OSError as exc:
            unreadable.append((str(path), str(exc)))
            return
        masked, n = mask_secrets(raw)
        shown = str(path.relative_to(base)) if base and base in path.parents else path.name
        sources.append(SourceText(id=f"f{len(sources) + 1}", path=shown.replace("\\", "/"), text=Text(masked),
                                  encoding=enc, secrets=n, role=role))

    dacpac_scripts: List[Tuple[str, str, str, str]] = []

    def add_path(p: str, role: str):
        nonlocal folder_like, root
        path = Path(p)
        if not path.exists():
            raise FileNotFoundError(f"Input not found: {p}")
        suffix = path.suffix.lower()
        if path.is_dir():
            projs = sorted(path.glob("*.sqlproj"))
            files = _sqlproj_files(projs[0]) if projs else _walk_sql(path)
            if role == "input":
                folder_like = True
                root = root or path
                kinds.append("SSDT project" if projs else "folder")
            for f in files:
                add_sql(f, role, path)
            for f in sorted(path.glob("*.dacpac")):
                add_path(str(f), "schema")
            for f in sorted(list(path.glob("*.csv")) + list(path.glob("*.tsv"))):
                if role == "schema":
                    add_path(str(f), "schema")
        elif suffix == ".sqlproj":
            if role == "input":
                folder_like = True
                root = root or path.parent
                kinds.append("SSDT project")
            for f in _sqlproj_files(path):
                add_sql(f, role, path.parent)
        elif suffix == ".dacpac":
            scripts = load_dacpac(path, catalog)
            if role == "input":
                folder_like = True
                kinds.append("DACPAC")
            for sid, text, src in scripts:
                masked, n = mask_secrets(text)
                dacpac_scripts.append((sid, masked, f"{path.name}: {sid.split(':', 1)[1]}", role))
        elif suffix in COLUMN_LIST_SUFFIXES:
            load_columns_csv(path, catalog)
            if role == "input":
                kinds.append("column list")
        else:
            if role == "input":
                kinds.append("SQL file")
            add_sql(path, role, path.parent)

    for p in inputs:
        add_path(p, "input")
    for p in schema or []:
        add_path(p, "schema")
    for sid, text, shown, role in dacpac_scripts:
        sources.append(SourceText(id=f"f{len(sources) + 1}", path=shown, text=Text(text), role=role))
    parsed = parse_fn([(s.id, s.text.text) for s in sources]) if sources else {"results": {}}
    results = parsed.get("results", parsed)
    for s in sources:
        r = results.get(s.id) or {}
        s.tree, s.errors = r.get("tree"), r.get("errors") or []
        collect_from_tree(catalog, s.tree, s.text, s.path)
    units: List[Tuple[Unit, SourceText]] = []
    for s in sources:
        if s.role != "input":
            continue
        name = Path(s.path).stem
        for u in find_units(s.tree, s.text, s.path, s.errors, name):
            units.append((u, s))
    has_schema = any(o.kind in ("table", "view", "table-type") for o in catalog.objects)
    if project is None:
        project = folder_like or len([u for u, _ in units if u.kind == "procedure"]) > 1
    mode = "project" if project else ("schema" if has_schema else "procedure")
    if catalog.sources == [] and has_schema:
        catalog.sources.append("CREATE scripts")
    return Inputs(sources=sources, catalog=catalog, units=units, mode=mode, input_kinds=list(dict.fromkeys(kinds)),
                  parser={k: v for k, v in parsed.items() if k != "results"}, unreadable=unreadable, root=root)
