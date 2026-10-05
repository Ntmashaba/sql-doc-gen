"""The objects a document may know about besides the procedure itself.

Table, view, function, procedure, synonym and table-type definitions come from CREATE scripts
(parsed with ScriptDom), from a DACPAC's model.xml, or from an exported column list (CSV).
Names match without regard to letter case, which is how almost every SQL Server database
collation compares object names.
"""
from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .model import CatalogObject, Column
from .syntax import Text, data_type_text, ident, obj_name, parts, typ, walk

TABLE_STATEMENTS = ("CreateTableStatement",)
VIEW_STATEMENTS = ("CreateViewStatement", "CreateOrAlterViewStatement", "AlterViewStatement")
FUNCTION_STATEMENTS = ("CreateFunctionStatement", "CreateOrAlterFunctionStatement", "AlterFunctionStatement")
PROCEDURE_STATEMENTS = ("CreateProcedureStatement", "CreateOrAlterProcedureStatement", "AlterProcedureStatement")


def _k(*names) -> Tuple[str, ...]:
    return tuple((n or "").lower() for n in names)


class Catalog:
    def __init__(self):
        self.objects: List[CatalogObject] = []
        self._by_name: Dict[Tuple[str, str], List[CatalogObject]] = {}
        self.database: str = ""          # the database the inputs describe, when known
        self.sources: List[str] = []     # where definitions came from (for the coverage table)

    # ------------------------------------------------------------------ building
    def add(self, obj: CatalogObject) -> CatalogObject:
        key = _k(obj.schema or "dbo", obj.name)
        existing = [o for o in self._by_name.get(key, []) if o.database.lower() == obj.database.lower()]
        for o in existing:
            if o.kind == obj.kind or {o.kind, obj.kind} <= {"table", "view"}:
                # a later definition of the same object (ALTER, a second script) replaces the earlier
                if obj.columns or not o.columns:
                    o.columns = obj.columns or o.columns
                o.keys = obj.keys or o.keys
                o.node = obj.node or o.node
                o.text = obj.text or o.text
                o.source = obj.source or o.source
                o.params = obj.params or o.params
                o.function_kind = obj.function_kind or o.function_kind
                return o
        self.objects.append(obj)
        self._by_name.setdefault(key, []).append(obj)
        return obj

    # ------------------------------------------------------------------ lookup
    def find(self, server: str, database: str, schema: str, name: str, default_db: str = "",
             schemas: Iterable[str] = ("dbo",), kinds: Optional[Iterable[str]] = None) -> Optional[CatalogObject]:
        if server:
            return None                    # linked-server objects are never in the inputs
        kinds = set(kinds) if kinds else None
        default_db = default_db or self.database
        cand_schemas = [schema] if schema else list(dict.fromkeys(list(schemas) + ["dbo"]))
        for sch in cand_schemas:
            for obj in self._by_name.get(_k(sch, name), []):
                if kinds and obj.kind not in kinds:
                    continue
                odb, rdb = obj.database.lower(), (database or "").lower()
                if not rdb or rdb == odb or (not odb and default_db and rdb == default_db.lower()):
                    return obj
        return None

    def procedures(self) -> List[CatalogObject]:
        return [o for o in self.objects if o.kind == "procedure"]

    def tables(self) -> List[CatalogObject]:
        return [o for o in self.objects if o.kind in ("table", "view", "table-type")]

    def __len__(self):
        return len(self.objects)


# ---------------------------------------------------------------------------- from parsed scripts

def table_definition_columns(defn, text: Text) -> Tuple[List[Column], List[List[str]]]:
    """Columns and keys of a TableDefinition (CREATE TABLE, DECLARE @t TABLE, table types)."""
    cols: List[Column] = []
    keys: List[List[str]] = []
    if not isinstance(defn, dict):
        return cols, keys
    for cd in defn.get("ColumnDefinitions", []) or []:
        name = ident(cd.get("ColumnIdentifier")) or ""
        col = Column(name=name, type=data_type_text(cd.get("DataType")))
        if cd.get("ComputedColumnExpression"):
            col.computed = text.squeeze(cd["ComputedColumnExpression"], 200)
        if cd.get("IdentityOptions") is not None:
            col.identity = True
            col.nullable = False
        if cd.get("DefaultConstraint"):
            col.default = text.squeeze(cd["DefaultConstraint"].get("Expression"), 80)
        for c in cd.get("Constraints", []) or []:
            if c.get("$") == "NullableConstraintDefinition":
                col.nullable = bool(c.get("Nullable"))
            elif c.get("$") == "UniqueConstraintDefinition":
                keys.append([name])
                if c.get("IsPrimaryKey"):
                    col.nullable = False
        cols.append(col)
    for c in defn.get("TableConstraints", []) or []:
        if c.get("$") == "UniqueConstraintDefinition":
            kcols = [parts(cw.get("Column", {}).get("MultiPartIdentifier"))[-1:]
                     for cw in c.get("Columns", []) or []]
            kcols = [k[0] for k in kcols if k]
            if kcols:
                keys.append(kcols)
                if c.get("IsPrimaryKey"):
                    for col in cols:
                        if col.name.lower() in {k.lower() for k in kcols}:
                            col.nullable = False
    for idx in defn.get("Indexes", []) or []:
        if idx.get("Unique"):
            kcols = [parts(cw.get("Column", {}).get("MultiPartIdentifier"))[-1:] for cw in idx.get("Columns", []) or []]
            kcols = [k[0] for k in kcols if k]
            if kcols:
                keys.append(kcols)
    return cols, keys


def _params(node, text: Text) -> List[dict]:
    out = []
    for p in node.get("Parameters", []) or []:
        out.append({
            "name": ident(p.get("VariableName")) or "",
            "type": data_type_text(p.get("DataType")),
            "default": text.squeeze(p["Value"], 80) if p.get("Value") else "",
            "output": p.get("Modifier") == "Output",
            "readonly": p.get("Modifier") == "ReadOnly",
        })
    return out


def _select_columns(select, text: Text) -> List[Column]:
    """Output column names of a view or inline function body, as far as they can be read."""
    q = select.get("QueryExpression") if isinstance(select, dict) else None
    while typ(q) in ("BinaryQueryExpression", "QueryParenthesisExpression"):
        q = q.get("FirstQueryExpression") or q.get("QueryExpression")
    cols = []
    if typ(q) != "QuerySpecification":
        return cols
    for el in q.get("SelectElements", []) or []:
        if el.get("$") == "SelectScalarExpression":
            cn = el.get("ColumnName")
            name = ident(cn.get("Identifier")) if cn and cn.get("Identifier") else (cn.get("Value") if cn else None)
            if not name and typ(el.get("Expression")) == "ColumnReferenceExpression":
                name = (parts(el["Expression"].get("MultiPartIdentifier")) or [""])[-1]
            if name:
                cols.append(Column(name=name))
        else:
            return []          # SELECT *: unknown until the base tables are known
    return cols


def collect_from_tree(catalog: Catalog, tree, text: Text, source: str, include_procedures: bool = True) -> None:
    """Add every CREATE TABLE / VIEW / FUNCTION / PROCEDURE / SYNONYM / TYPE in a parsed script."""
    if not tree:
        return
    current_db = ""
    for batch in tree.get("Batches", []) or []:
        for st in batch.get("Statements", []) or []:
            t = st.get("$")
            if t == "UseStatement":
                current_db = ident(st.get("DatabaseName")) or current_db
                if current_db and not catalog.database:
                    catalog.database = current_db
                continue
            if t in TABLE_STATEMENTS:
                on = obj_name(st.get("SchemaObjectName"))
                if on.name.startswith("#"):
                    continue
                cols, keys = table_definition_columns(st.get("Definition"), text)
                catalog.add(CatalogObject("table", on.database or current_db, on.schema or "dbo", on.name,
                                          cols, keys, source, st, text))
            elif t == "AlterTableAddTableElementStatement":
                on = obj_name(st.get("SchemaObjectName"))
                obj = catalog.find("", on.database, on.schema, on.name, kinds=("table",))
                if obj:
                    cols, keys = table_definition_columns(st.get("Definition"), text)
                    known = {c.name.lower() for c in obj.columns}
                    obj.columns += [c for c in cols if c.name.lower() not in known]
                    obj.keys += keys
            elif t == "CreateTypeTableStatement":
                on = obj_name(st.get("Name"))
                cols, keys = table_definition_columns(st.get("Definition"), text)
                catalog.add(CatalogObject("table-type", on.database or current_db, on.schema or "dbo", on.name,
                                          cols, keys, source, st, text))
            elif t in VIEW_STATEMENTS:
                on = obj_name(st.get("SchemaObjectName"))
                cols = [Column(ident(c) or "") for c in st.get("Columns", []) or []] or \
                    _select_columns(st.get("SelectStatement"), text)
                catalog.add(CatalogObject("view", on.database or current_db, on.schema or "dbo", on.name,
                                          cols, [], source, st, text))
            elif t in FUNCTION_STATEMENTS:
                on = obj_name(st.get("Name"))
                rt = st.get("ReturnType") or {}
                fk, cols = "scalar", []
                if rt.get("$") == "SelectFunctionReturnType":
                    fk, cols = "inline", _select_columns(rt.get("SelectStatement"), text)
                elif rt.get("$") == "TableValuedFunctionReturnType":
                    fk = "multi-statement"
                    cols, _ = table_definition_columns((rt.get("DeclareTableVariableBody") or {}).get("Definition"), text)
                obj = CatalogObject("function", on.database or current_db, on.schema or "dbo", on.name,
                                    cols, [], source, st, text, params=_params(st, text), function_kind=fk)
                catalog.add(obj)
            elif t in PROCEDURE_STATEMENTS and include_procedures:
                on = obj_name((st.get("ProcedureReference") or {}).get("Name"))
                catalog.add(CatalogObject("procedure", on.database or current_db, on.schema or "dbo", on.name,
                                          [], [], source, st, text, params=_params(st, text)))
            elif t == "CreateSynonymStatement":
                on = obj_name(st.get("Name"))
                tgt = obj_name(st.get("ForName"))
                catalog.add(CatalogObject("synonym", on.database or current_db, on.schema or "dbo", on.name,
                                          [], [], source, st, text, target=tuple(tgt)))


# ---------------------------------------------------------------------------- DACPAC

_NS = "{http://schemas.microsoft.com/sqlserver/dac/Serialization/2012/02}"


def _split_name(name: str) -> List[str]:
    return [p.replace("]]", "]") for p in re.findall(r"\[((?:[^\]]|\]\])*)\]", name or "")]


def _prop(el, name) -> Optional[str]:
    for p in el.findall(f"{_NS}Property"):
        if p.get("Name") == name:
            if p.get("Value") is not None:
                return p.get("Value")
            v = p.find(f"{_NS}Value")
            return v.text if v is not None else None
    return None


def _rel(el, name):
    for r in el.findall(f"{_NS}Relationship"):
        if r.get("Name") == name:
            return r
    return None


def _type_spec(el) -> str:
    rel = _rel(el, "TypeSpecifier") or _rel(el, "Type")
    if rel is None:
        return ""
    spec = rel.find(f"{_NS}Entry/{_NS}Element")
    if spec is None:
        ref = rel.find(f"{_NS}Entry/{_NS}References")
        return ".".join(_split_name(ref.get("Name"))) if ref is not None else ""
    tref = spec.find(f"{_NS}Relationship[@Name='Type']/{_NS}Entry/{_NS}References")
    base = ".".join(_split_name(tref.get("Name"))) if tref is not None else ""
    if _prop(spec, "IsMax") == "True":
        return f"{base}(max)"
    length, prec, scale = _prop(spec, "Length"), _prop(spec, "Precision"), _prop(spec, "Scale")
    if length:
        return f"{base}({length})"
    if prec and base.lower() in ("decimal", "numeric"):
        return f"{base}({prec},{scale or 0})"
    return base


def load_dacpac(path: Path, catalog: Catalog) -> List[Tuple[str, str, str]]:
    """Read tables, views, functions and procedures from a DACPAC.

    Tables (columns, types, nullability, keys) go straight into the catalog. Views, functions and
    procedures are returned as (id, CREATE text, source) to be parsed like any other script.
    """
    scripts: List[Tuple[str, str, str]] = []
    with zipfile.ZipFile(path) as z:
        names = {n.lower(): n for n in z.namelist()}
        model = ET.fromstring(z.read(names["model.xml"]))
        if "dacmetadata.xml" in names:
            meta = ET.fromstring(z.read(names["dacmetadata.xml"]))
            nm = meta.find("{http://schemas.microsoft.com/sqlserver/dac/Serialization/2012/02}Name")
            if nm is not None and nm.text and not catalog.database:
                catalog.database = nm.text
    if not catalog.database:
        catalog.database = path.stem
    src = str(path)
    tables: Dict[str, CatalogObject] = {}
    root_model = model.find(f"{_NS}Model")
    if root_model is None:
        return scripts
    for el in root_model.findall(f"{_NS}Element"):
        etype, name = el.get("Type"), el.get("Name")
        np = _split_name(name)
        if etype in ("SqlTable", "SqlView", "SqlTableType") and len(np) >= 2:
            cols = []
            crel = _rel(el, "Columns")
            if crel is not None:
                for ce in crel.findall(f"{_NS}Entry/{_NS}Element"):
                    cname = _split_name(ce.get("Name"))[-1:]
                    col = Column(name=cname[0] if cname else "", type=_type_spec(ce))
                    nullable = _prop(ce, "IsNullable")
                    col.nullable = None if nullable is None else nullable == "True"
                    col.identity = _prop(ce, "IsIdentity") == "True"
                    if ce.get("Type") == "SqlComputedColumn":
                        col.computed = (_prop(ce, "ExpressionScript") or "").strip()
                    cols.append(col)
            kind = {"SqlTable": "table", "SqlView": "view", "SqlTableType": "table-type"}[etype]
            obj = CatalogObject(kind, catalog.database, np[0], np[1], cols, [], src)
            if etype == "SqlTable":
                tables[".".join(np).lower()] = catalog.add(obj)
            elif etype == "SqlView":
                q = _prop(el, "QueryScript")
                if q:
                    scripts.append((f"dacpac:{name}", f"CREATE VIEW [{np[0]}].[{np[1]}] AS\n{q}", src))
                catalog.add(obj)
            else:
                catalog.add(obj)
        elif etype in ("SqlPrimaryKeyConstraint", "SqlUniqueConstraint"):
            dt = el.find(f"{_NS}Relationship[@Name='DefiningTable']/{_NS}Entry/{_NS}References")
            cols = [".".join(_split_name(r.get("Name"))[-1:]) for r in
                    el.findall(f"{_NS}Relationship[@Name='ColumnSpecifications']/{_NS}Entry/{_NS}Element/"
                               f"{_NS}Relationship[@Name='Column']/{_NS}Entry/{_NS}References")]
            if dt is not None and cols:
                tbl = tables.get(".".join(_split_name(dt.get("Name"))).lower())
                if tbl:
                    tbl.keys.append(cols)
                    if etype == "SqlPrimaryKeyConstraint":
                        for c in tbl.columns:
                            if c.name in cols:
                                c.nullable = False
        elif etype in ("SqlProcedure", "SqlScalarFunction", "SqlInlineTableValuedFunction",
                       "SqlMultiStatementTableValuedFunction") and len(np) >= 2:
            body = _prop(el, "BodyScript") or ""
            params = []
            prel = _rel(el, "Parameters")
            if prel is not None:
                for pe in prel.findall(f"{_NS}Entry/{_NS}Element"):
                    pname = _split_name(pe.get("Name"))[-1]
                    ptxt = f"{pname} {_type_spec(pe)}"
                    dflt = _prop(pe, "DefaultExpressionScript")
                    if dflt:
                        ptxt += f" = {dflt.strip()}"
                    if _prop(pe, "IsOutput") == "True":
                        ptxt += " OUTPUT"
                    if _prop(pe, "IsReadOnly") == "True":
                        ptxt += " READONLY"
                    params.append(ptxt)
            if etype == "SqlProcedure":
                head = f"CREATE PROCEDURE [{np[0]}].[{np[1]}]\n" + (",\n".join("    " + p for p in params))
                scripts.append((f"dacpac:{name}", f"{head}\nAS\n{body}", src))
            else:
                ret = _prop(el, "ReturnsScript") or ""
                header = _prop(el, "HeaderContents")
                if header:
                    scripts.append((f"dacpac:{name}", f"{header}\n{body}", src))
                elif etype == "SqlInlineTableValuedFunction":
                    scripts.append((f"dacpac:{name}",
                                    f"CREATE FUNCTION [{np[0]}].[{np[1]}]({', '.join(params)})\nRETURNS TABLE\nAS\nRETURN {body}",
                                    src))
                elif ret:
                    scripts.append((f"dacpac:{name}",
                                    f"CREATE FUNCTION [{np[0]}].[{np[1]}]({', '.join(params)})\nRETURNS {ret}\nAS\n{body}",
                                    src))
    catalog.sources.append(f"DACPAC {path.name}")
    return scripts


# ---------------------------------------------------------------------------- column list CSV

_CSV_ALIASES = {
    "database": ("table_catalog", "database", "db", "database_name"),
    "schema": ("table_schema", "schema", "schema_name"),
    "table": ("table_name", "table", "object", "object_name", "view_name"),
    "column": ("column_name", "column", "name"),
    "position": ("ordinal_position", "position", "column_id", "ordinal"),
    "type": ("data_type", "type", "type_name"),
    "length": ("character_maximum_length", "max_length", "length"),
    "precision": ("numeric_precision", "precision"),
    "scale": ("numeric_scale", "scale"),
    "nullable": ("is_nullable", "nullable"),
}


def load_columns_csv(path: Path, catalog: Catalog) -> int:
    """An exported column list, for example INFORMATION_SCHEMA.COLUMNS saved as CSV."""
    raw = path.read_bytes()
    from .textutil import decode_sql_bytes
    text, _ = decode_sql_bytes(raw)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    if not rows:
        return 0
    header = {h.strip().lower(): h for h in rows[0].keys() if h}
    col_of = {}
    for want, names in _CSV_ALIASES.items():
        for n in names:
            if n in header:
                col_of[want] = header[n]
                break
    if "table" not in col_of or "column" not in col_of:
        raise ValueError(f"{path.name}: needs at least table and column headings "
                         f"(for example an INFORMATION_SCHEMA.COLUMNS export)")
    grouped: Dict[Tuple[str, str, str], List[Tuple[int, Column]]] = {}
    for i, r in enumerate(rows):
        g = lambda k: (r.get(col_of[k]) or "").strip() if k in col_of else ""
        tkey = (g("database"), g("schema") or "dbo", g("table"))
        typ_ = g("type")
        length = g("length")
        if length and length not in ("NULL", "0"):
            typ_ = f"{typ_}({'max' if length == '-1' else length})"
        elif g("precision") and typ_.lower() in ("decimal", "numeric"):
            typ_ = f"{typ_}({g('precision')},{g('scale') or 0})"
        nullable = g("nullable").upper()
        col = Column(name=g("column"), type=typ_,
                     nullable=None if not nullable else nullable in ("YES", "1", "TRUE", "Y"))
        try:
            pos = int(g("position") or i)
        except ValueError:
            pos = i
        grouped.setdefault(tkey, []).append((pos, col))
    for (db, sch, tbl), cols in grouped.items():
        cols.sort(key=lambda x: x[0])
        if db and not catalog.database:
            catalog.database = db
        catalog.add(CatalogObject("table", db, sch, tbl, [c for _, c in cols], [], str(path)))
    catalog.sources.append(f"column list {path.name}")
    return len(grouped)
