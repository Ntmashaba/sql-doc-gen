"""Name resolution, expression analysis and query analysis.

``Ctx`` holds everything known about one procedure while it is analysed: relations (tables,
temp tables, table variables, variables, cursors, result sets...), column versions
(``ColNode``) and steps. ``Analyzer`` reads the queries and expressions of one step and
reports, for every value it produces, which columns feed it directly (the value is computed
from them) and indirectly (they decide which rows, or which branch of a CASE, apply).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .catalog import Catalog
from .model import ROWS, VALUE, CatalogObject, ColNode, Column, Relation, Step, Use
from .syntax import Text, data_type_text, ident, literal_value, obj_name, parts, typ, unparen, walk

LITERALS = {"IntegerLiteral", "StringLiteral", "NumericLiteral", "RealLiteral", "MoneyLiteral", "BinaryLiteral",
            "NullLiteral", "DefaultLiteral", "MaxLiteral", "OdbcLiteral", "IdentifierLiteral"}
AGGREGATES = {"SUM", "AVG", "MIN", "MAX", "COUNT", "COUNT_BIG", "STDEV", "STDEVP", "VAR", "VARP", "STRING_AGG",
              "CHECKSUM_AGG", "GROUPING", "GROUPING_ID", "APPROX_COUNT_DISTINCT", "APPROX_PERCENTILE_CONT",
              "APPROX_PERCENTILE_DISC", "JSON_ARRAYAGG", "JSON_OBJECTAGG"}
WINDOW_ONLY = {"ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE",
               "CUME_DIST", "PERCENT_RANK", "PERCENTILE_CONT", "PERCENTILE_DISC"}
NULL_FUNCS = {"ISNULL", "COALESCE", "NULLIF", "IIF"}
STRING_FUNCS = {"LEFT", "RIGHT", "SUBSTRING", "LTRIM", "RTRIM", "TRIM", "UPPER", "LOWER", "REPLACE", "CONCAT",
                "CONCAT_WS", "STUFF", "LEN", "DATALENGTH", "CHARINDEX", "PATINDEX", "REPLICATE", "REVERSE",
                "FORMAT", "QUOTENAME", "STR", "SPACE", "TRANSLATE", "STRING_ESCAPE", "SOUNDEX", "CHAR", "NCHAR",
                "ASCII", "UNICODE", "STRING_SPLIT"}
DATE_FUNCS = {"DATEADD", "DATEDIFF", "DATEDIFF_BIG", "DATEPART", "DATENAME", "YEAR", "MONTH", "DAY", "EOMONTH",
              "DATEFROMPARTS", "DATETIMEFROMPARTS", "DATETRUNC", "DATE_BUCKET", "SWITCHOFFSET", "TODATETIMEOFFSET",
              "ISDATE"}
SYSTEM_FUNCS = {"GETDATE", "GETUTCDATE", "SYSDATETIME", "SYSUTCDATETIME", "SYSDATETIMEOFFSET", "NEWID",
                "NEWSEQUENTIALID", "SCOPE_IDENTITY", "IDENT_CURRENT", "@@IDENTITY", "SUSER_SNAME", "SUSER_NAME",
                "USER_NAME", "HOST_NAME", "APP_NAME", "DB_NAME", "DB_ID", "OBJECT_ID", "OBJECT_NAME", "ERROR_MESSAGE",
                "ERROR_NUMBER", "ERROR_SEVERITY", "ERROR_STATE", "ERROR_LINE", "ERROR_PROCEDURE", "XACT_STATE",
                "RAND", "CURRENT_TIMESTAMP", "ORIGINAL_LOGIN", "SESSION_USER", "SYSTEM_USER", "USER",
                "CONTEXT_INFO", "SESSION_CONTEXT", "@@SPID", "SERVERPROPERTY", "DATABASEPROPERTYEX"}
MATH_FUNCS = {"ROUND", "FLOOR", "CEILING", "ABS", "POWER", "SQRT", "SQUARE", "EXP", "LOG", "LOG10", "SIGN",
              "PI", "SIN", "COS", "TAN", "ATAN", "ASIN", "ACOS", "ATN2", "DEGREES", "RADIANS", "GREATEST", "LEAST"}
SYSTEM_SCHEMAS = {"sys", "information_schema"}

_SYSTEM_PREFIX = re.compile(r"^(sys|dm_|fn_)", re.I)
_PLACEHOLDER = re.compile(r"__sqldocgen_\d+__", re.I)


def norm(s: str) -> str:
    return (s or "").lower()


@dataclass
class LocalRel:
    """A statement-local relation: CTE, derived table, VALUES list, table function result."""
    key: str
    kind: str
    name: str
    cols: List[Tuple[str, int]] = field(default_factory=list)      # (column name, node id)
    rows: Optional[int] = None                                      # node id of the (rows) version
    star: bool = False                                              # has an unexpanded *

    def node_for(self, col: str) -> Optional[int]:
        lc = norm(col)
        for n, nid in self.cols:
            if norm(n) == lc:
                return nid
        return None

    @property
    def names(self) -> List[str]:
        return [n for n, _ in self.cols]


@dataclass
class Source:
    """One table reference in a FROM clause (or the target of an UPDATE / MERGE)."""
    alias: Optional[str]
    names: Set[str]                       # lower-case names the source can be qualified by
    rel: Optional[Relation] = None
    local: Optional[LocalRel] = None
    nullable: bool = False                # outer side of a LEFT / RIGHT / FULL join
    join: str = ""                        # how it was joined
    span: Optional[Tuple[int, int]] = None

    @property
    def key(self) -> str:
        return self.local.key if self.local else self.rel.key

    @property
    def label(self) -> str:
        return self.alias or (self.local.name if self.local else self.rel.name)

    def has(self, col: str) -> str:
        """'yes', 'no' or 'unknown'."""
        if self.local is not None:
            if self.local.node_for(col) is not None:
                return "yes"
            return "unknown" if self.local.star else "no"
        if self.rel.find(col):
            return "yes"
        return "no" if self.rel.complete else "unknown"


class QueryScope:
    def __init__(self, parent: Optional["QueryScope"] = None, ctes: Optional[Dict[str, LocalRel]] = None):
        self.parent = parent
        self.sources: List[Source] = []
        self.ctes: Dict[str, LocalRel] = dict(ctes or {})
        self.aliases: Dict[str, "OutCol"] = {}
        self.special: Dict[str, Source] = {}

    def cte(self, name: str) -> Optional[LocalRel]:
        s = self
        while s is not None:
            if norm(name) in s.ctes:
                return s.ctes[norm(name)]
            s = s.parent
        return None


@dataclass
class OutCol:
    name: Optional[str]
    uses: List[Use]
    expr: str = ""
    span: Optional[Tuple[int, int]] = None
    transforms: List[str] = field(default_factory=list)
    assign_var: Optional[str] = None       # SELECT @x = ...
    assign_kind: str = "Equals"
    star: Optional[Source] = None          # unexpanded * from this source
    node: Optional[dict] = None


@dataclass
class QueryOut:
    cols: List[OutCol] = field(default_factory=list)
    row_uses: List[Use] = field(default_factory=list)
    sources: List[Source] = field(default_factory=list)
    reads: Set[str] = field(default_factory=set)
    top: bool = False
    distinct: bool = False
    has_from: bool = False
    aggregate_only: bool = False           # aggregates without GROUP BY: always exactly one row
    order_by: bool = False


class Ctx:
    """Analysis state for one procedure (or script)."""

    def __init__(self, catalog: Catalog, default_db: str = "", default_schema: str = "dbo", project: bool = False):
        self.catalog = catalog
        self.default_db = default_db or catalog.database or ""
        self.default_schema = default_schema or "dbo"
        self.project = project
        self.texts: Dict[str, Text] = {}
        self.relations: Dict[str, Relation] = {}
        self.nodes: List[ColNode] = []
        self.steps: List[Step] = []
        self.step_by_id: Dict[str, Step] = {}
        self.results = 0
        self.calls: List[dict] = []
        self.unresolved: List[dict] = []
        self.functions: Dict[str, dict] = {}
        self.hints: List[dict] = []
        self.star_expanded = 0
        self.star_unexpanded = 0
        self.local_count = 0
        self.keys: Dict[str, List[List[str]]] = {}         # unique keys of temp tables / table variables
        self.cursor_defs: Dict[str, tuple] = {}            # cursor -> (SELECT node, declaring step)
        self.opened_cursors: Set[str] = set()
        self.nested: Dict[str, dict] = {}                  # EXEC step -> bindings copied back at its end
        self.expanded: Dict[str, dict] = {}                # EXEC step -> expansion info (namespace, params...)
        self.expanded_objects: Dict[str, dict] = {}        # views / inline functions read through their definition
        self.placeholders: Dict[str, str] = {}             # dynamic SQL token -> where its value comes from
        self.comparisons: List[dict] = []                  # predicate comparisons, for the review checks
        self.locals: Dict[str, dict] = {}                  # statement-local relations (CTEs, derived tables...)
        self.not_in: List[dict] = []                       # NOT IN (subquery) predicates

    def nested_expanded(self, step_id: str) -> Optional[dict]:
        return self.expanded.get(step_id)

    # ------------------------------------------------------------------ nodes
    def node(self, **kw) -> ColNode:
        n = ColNode(id=len(self.nodes), **kw)
        self.nodes.append(n)
        return n

    # ------------------------------------------------------------------ relations
    def relation(self, key: str, kind: str, name: str, **kw) -> Relation:
        rel = self.relations.get(key)
        if rel is None:
            rel = Relation(key=key, kind=kind, name=name, **kw)
            self.relations[key] = rel
        return rel

    def var_key(self, name: str, namespace: str = "") -> str:
        return f"var:{namespace}{norm(name)}"

    def variable(self, name: str, namespace: str = "", kind: str = "variable", data_type: str = "") -> Relation:
        key = self.var_key(name, namespace)
        rel = self.relations.get(key)
        if rel is None:
            label = name if not namespace else f"{name} ({namespace.rstrip('/')})"
            rel = self.relation(key, kind, label, columns=[Column(VALUE, data_type)], complete=True,
                                data_type=data_type)
        elif data_type and not rel.data_type:
            rel.data_type = data_type
            rel.columns[0].type = data_type
        return rel

    def table_variable(self, name: str, namespace: str = "") -> Relation:
        key = f"tvar:{namespace}{norm(name)}"
        label = name if not namespace else f"{name} ({namespace.rstrip('/')})"
        return self.relation(key, "table-variable", label)

    def table_relation(self, on, step: Optional[Step] = None) -> Relation:
        """The relation for a table name as written (temp table, permanent, remote, system...)."""
        server, database, schema, name = on
        token = _PLACEHOLDER.search(".".join(p for p in on if p))
        if token:
            label = self.placeholders.get(token.group(0), "a value known only at run time")
            key = f"dynamic:{norm(token.group(0))}"
            return self.relation(key, "dynamic", f"(object named by {label})",
                                 note="the name is built at run time from " + label)
        if name.startswith("##"):
            return self.relation(f"temp:{norm(name)}", "global-temp", name)
        if name.startswith("#"):
            return self.relation(f"temp:{norm(name)}", "temp", name)
        if server:
            key = f"table:{norm(server)}.{norm(database)}.{norm(schema or 'dbo')}.{norm(name)}"
            disp = ".".join(p for p in (server, database, schema or "dbo", name) if p)
            return self.relation(key, "remote", disp, endpoint=self.endpoint(server, database, schema or "dbo", name))
        obj = None
        if not (schema and norm(schema) in SYSTEM_SCHEMAS):
            schemas = [self.default_schema, "dbo"]
            obj = self.catalog.find("", database, schema, name, self.default_db, schemas)
            if obj and obj.kind == "synonym" and obj.target:
                tgt = obj.target
                rel = self.table_relation(tuple(tgt), step)
                if rel.note == "":
                    rel.note = f"through synonym {obj.display}"
                return rel
        db = database or (obj.database if obj else "") or self.default_db
        sch = schema or (obj.schema if obj else "") or "dbo"
        key = f"table:{norm(db)}.{norm(sch)}.{norm(name)}"
        if key in self.relations:
            return self.relations[key]
        same_db = not database or (self.default_db and norm(database) == norm(self.default_db))
        disp = f"{sch}.{name}" if same_db else f"{database}.{sch}.{name}"
        if norm(sch) in SYSTEM_SCHEMAS or (not schema and _SYSTEM_PREFIX.match(name) and not obj
                                           and name.lower().startswith("sys")):
            kind = "system"
        elif obj:
            kind = {"table": "table", "view": "view", "function": "function", "table-type": "table"}.get(obj.kind, "table")
        else:
            kind = "table"
        rel = self.relation(key, kind, disp, endpoint=self.endpoint(None, db or None, sch, name), catalog=obj)
        if obj and obj.columns:
            rel.columns = [Column(c.name, c.type, c.nullable, c.identity, c.computed, c.default) for c in obj.columns]
            rel.complete = True
        if not obj and kind == "table":
            rel.note = "definition not supplied"
        return rel

    @staticmethod
    def endpoint(server, database, schema, name) -> dict:
        return {"system": "SQL Server", "server": server or None, "port": None, "database": database or None,
                "schema": schema or None, "object": name or None, "path": None, "url": None, "container": None}


class Analyzer:
    """Analyses the expressions and queries of one step."""

    def __init__(self, ctx: Ctx, step: Step):
        self.ctx = ctx
        self.step = step
        self.text_id = step.text_id
        self.text: Text = ctx.texts[step.text_id]
        self.ns = step.namespace
        self.reads: Set[str] = set()
        self.param_map: Dict[str, List[Use]] = {}     # inline function parameters -> argument uses
        self._expanding: List[str] = []                # views / functions being expanded (cycle guard)

    # ------------------------------------------------------------------ small helpers
    def span(self, n):
        return self.text.span(n)

    def sq(self, n, limit=160) -> str:
        return self.text.squeeze(n, limit)

    def var_use(self, name: str, role: str, direct: bool, n=None) -> Use:
        name = name or ""
        if name.startswith("@@"):
            return Use(rel=f"system:{norm(name)}", col=VALUE, role=role, direct=direct, span=self.span(n),
                       text=name)
        rel = self.ctx.relations.get(self.ctx.var_key(name, self.ns))
        if rel is None:
            rel = self.ctx.variable(name, self.ns)
            rel.note = "not declared in this code"
        return Use(rel=rel.key, col=VALUE, role=role, direct=direct, span=self.span(n), text=name)

    def new_local(self, kind: str, name: str) -> str:
        self.ctx.local_count += 1
        key = f"{kind}:{self.step.id}:{self.ctx.local_count}:{norm(name)}"
        self.ctx.locals[key] = {"kind": kind, "name": name, "step": self.step.id, "text": self.text_id}
        return key

    # ------------------------------------------------------------------ column resolution
    def _match_qualified(self, scope: QueryScope, qual: List[str]) -> Optional[Source]:
        q = [norm(x) for x in qual if x is not None]
        last = q[-1] if q else ""
        if last in scope.special:
            return scope.special[last]
        exact = [s for s in scope.sources if s.alias and norm(s.alias) == last and len(q) == 1]
        if exact:
            return exact[0]
        by_name = []
        for s in scope.sources:
            names = s.names
            dotted = ".".join(q)
            if dotted in names or (len(q) == 1 and last in names and not s.alias):
                by_name.append(s)
        if by_name:
            return by_name[0]
        # qualified by table name although an alias exists: SQL Server rejects it, but read on
        for s in scope.sources:
            if ".".join(q) in s.names or last in s.names:
                return s
        return None

    def resolve(self, cre: dict, scope: Optional[QueryScope], role: str, direct: bool) -> Optional[Use]:
        ctype = cre.get("ColumnType", "Regular")
        sp = self.span(cre)
        if ctype == "Wildcard":
            return None
        ids = parts(cre.get("MultiPartIdentifier"))
        if ctype in ("PseudoColumnAction",) or (ids and ids[-1].lower() == "$action"):
            return Use(rel="system:$action", col=VALUE, role=role, direct=direct, span=sp, text="$action")
        if ctype in ("IdentityCol", "RowGuidCol", "PseudoColumnIdentity", "PseudoColumnRowGuid"):
            ids = ids + ["$" + ("identity" if "Identity" in ctype else "rowguid")]
        if not ids:
            return None
        col, qual = ids[-1], ids[:-1]
        text = ".".join(i for i in ids if i)
        s = scope
        while s is not None:
            if qual:
                src = self._match_qualified(s, qual)
                if src:
                    return self._use_from(src, col, role, direct, sp, text)
            else:
                known = [x for x in s.sources if x.has(col) == "yes"]
                if len(known) == 1:
                    return self._use_from(known[0], col, role, direct, sp, text)
                if len(known) > 1:
                    u = self._use_from(known[0], col, role, direct, sp, text)
                    u.status = "partial"
                    u.candidates = [k.label for k in known]
                    return u
                unknown = [x for x in s.sources if x.has(col) == "unknown"]
                if len(unknown) == 1:
                    return self._use_from(unknown[0], col, role, direct, sp, text)
                if len(unknown) > 1:
                    learned = [x for x in unknown if x.rel is not None and x.rel.find(col)]
                    pick = learned[0] if len(learned) == 1 else unknown[0]
                    u = self._use_from(pick, col, role, direct, sp, text, learn=False)
                    u.status = "partial"
                    u.candidates = [k.label for k in unknown]
                    return u
                # ORDER BY may name a select-list alias
                if norm(col) in s.aliases:
                    return None
            s = s.parent
        self.ctx.unresolved.append({"step": self.step.id, "text": text, "span": sp})
        return Use(rel="unresolved:" + norm(text), col=col, role=role, direct=direct, span=sp,
                   status="unresolved", text=text)

    def _use_from(self, src: Source, col: str, role: str, direct: bool, sp, text, learn=True) -> Use:
        if src.local is not None:
            nid = src.local.node_for(col)
            if nid is None:
                if src.local.star:
                    star = src.local.node_for("*")
                    return Use(rel=src.local.key, col=col, role=role, direct=direct, span=sp, status="partial",
                               local=star, text=text)
                return Use(rel=src.local.key, col=col, role=role, direct=direct, span=sp, status="unresolved",
                           text=text)
            return Use(rel=src.local.key, col=col, role=role, direct=direct, span=sp, local=nid, text=text)
        rel = src.rel
        c = rel.find(col)
        status = "resolved"
        if c is None:
            if rel.complete and rel.kind not in ("system",):
                status = "unresolved"
                self.ctx.unresolved.append({"step": self.step.id, "text": text, "span": sp,
                                            "note": f"{col} is not a column of {rel.name}"})
            elif learn:
                c = rel.learn(col)
        name = c.name if c else col
        self.reads.add(rel.key)
        return Use(rel=rel.key, col=name, role=role, direct=direct, span=sp, status=status, text=text)

    # ------------------------------------------------------------------ expressions
    def expr(self, e, scope: Optional[QueryScope], role: str = "value", direct: bool = True,
             out: Optional[List[Use]] = None, tf: Optional[List[str]] = None) -> List[Use]:
        out = [] if out is None else out
        tf = [] if tf is None else tf
        t = typ(e)
        if t is None:
            return out
        if t == "ColumnReferenceExpression":
            u = self.resolve(e, scope, role, direct)
            if u:
                out.append(u)
        elif t == "VariableReference":
            mapped = self.param_map.get(norm(e.get("Name")))
            if mapped is not None:
                out.extend(self._as(u, role if not direct or not u.direct else role, direct and u.direct) for u in mapped)
            else:
                out.append(self.var_use(e.get("Name"), role, direct, e))
        elif t == "GlobalVariableExpression":
            out.append(self.var_use(e.get("Name"), role, direct, e))
            if direct:
                tf.append(f"system value {e.get('Name')}")
        elif t in LITERALS:
            if direct and role == "value":
                tf.append("constant")
        elif t == "BinaryExpression":
            op = e.get("BinaryExpressionType", "")
            if direct:
                tf.append("concatenation" if op == "Add" and self._stringy(e) else f"arithmetic ({op.lower()})")
            self.expr(e.get("FirstExpression"), scope, role, direct, out, tf)
            self.expr(e.get("SecondExpression"), scope, role, direct, out, tf)
        elif t in ("UnaryExpression", "ParenthesisExpression"):
            self.expr(e.get("Expression"), scope, role, direct, out, tf)
        elif t in ("CastCall", "TryCastCall", "ConvertCall", "TryConvertCall"):
            if direct:
                fn = "CAST" if "Cast" in t else "CONVERT"
                if t.startswith("Try"):
                    fn = "TRY_" + fn
                tf.append(f"{fn} to {data_type_text(e.get('DataType'))}")
            self.expr(e.get("Parameter"), scope, role, direct, out, tf)
            if e.get("Style") is not None:
                self.expr(e.get("Style"), scope, role, False, out, tf)
        elif t in ("ParseCall", "TryParseCall"):
            if direct:
                tf.append(f"PARSE to {data_type_text(e.get('DataType'))}")
            self.expr(e.get("StringValue"), scope, role, direct, out, tf)
        elif t == "FunctionCall":
            self._function(e, scope, role, direct, out, tf)
        elif t in ("LeftFunctionCall", "RightFunctionCall"):
            if direct:
                tf.append("string function " + ("LEFT" if t.startswith("Left") else "RIGHT"))
            for p in e.get("Parameters", []) or []:
                self.expr(p, scope, role, direct, out, tf)
        elif t == "CoalesceExpression":
            if direct:
                tf.append("COALESCE default")
            for x in e.get("Expressions", []) or []:
                self.expr(x, scope, role, direct, out, tf)
        elif t == "NullIfExpression":
            if direct:
                tf.append("NULLIF")
            self.expr(e.get("FirstExpression"), scope, role, direct, out, tf)
            self.expr(e.get("SecondExpression"), scope, "case" if direct else role, False, out, tf)
        elif t == "IIfCall":
            if direct:
                tf.append("IIF")
            self.bexpr(e.get("Predicate"), scope, "case" if direct else role, out)
            self.expr(e.get("ThenExpression"), scope, role, direct, out, tf)
            self.expr(e.get("ElseExpression"), scope, role, direct, out, tf)
        elif t == "SearchedCaseExpression":
            if direct:
                tf.append("CASE")
            for wc in e.get("WhenClauses", []) or []:
                self.bexpr(wc.get("WhenExpression"), scope, "case" if direct else role, out)
                self.expr(wc.get("ThenExpression"), scope, role, direct, out, tf)
            self.expr(e.get("ElseExpression"), scope, role, direct, out, tf)
        elif t == "SimpleCaseExpression":
            if direct:
                tf.append("CASE")
            self.expr(e.get("InputExpression"), scope, "case" if direct else role, False, out, tf)
            for wc in e.get("WhenClauses", []) or []:
                self.expr(wc.get("WhenExpression"), scope, "case" if direct else role, False, out, tf)
                self.expr(wc.get("ThenExpression"), scope, role, direct, out, tf)
            self.expr(e.get("ElseExpression"), scope, role, direct, out, tf)
        elif t == "ScalarSubquery":
            sub = self.query(e.get("QueryExpression"), QueryScope(parent=scope, ctes=scope.ctes if scope else None))
            if direct:
                tf.append("subquery")
            if sub.cols:
                for u in sub.cols[0].uses:
                    out.append(self._as(u, role if not u.direct or not direct else role, direct and u.direct))
            for u in sub.row_uses:
                out.append(self._as(u, "subquery" if direct else role, False))
        elif t == "AtTimeZoneCall":
            if direct:
                tf.append("AT TIME ZONE")
            self.expr(e.get("DateValue"), scope, role, direct, out, tf)
            self.expr(e.get("TimeZone"), scope, role, False, out, tf)
        elif t == "ParameterlessCall":
            if direct:
                tf.append(f"system value {e.get('ParameterlessCallType', '').upper()}")
        elif t == "NextValueForExpression":
            if direct:
                tf.append("sequence value")
        elif t == "IdentityFunctionCall":
            if direct:
                tf.append("IDENTITY()")
        else:
            for k in self._children(e):
                self.expr(k, scope, role, direct, out, tf)
        return out

    @staticmethod
    def _children(e):
        for k, v in e.items():
            if k in ("$", "@"):
                continue
            if isinstance(v, dict):
                yield v
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, dict):
                        yield x

    def _stringy(self, e) -> bool:
        for x in walk(e):
            if typ(x) == "StringLiteral":
                return True
        return False

    @staticmethod
    def _as(u: Use, role: str, direct: bool) -> Use:
        return Use(rel=u.rel, col=u.col, role=role, direct=direct, span=u.span, status=u.status,
                   candidates=list(u.candidates), local=u.local, text=u.text, same_step=u.same_step)

    def _function(self, e, scope, role, direct, out, tf):
        name = (ident(e.get("FunctionName")) or "").upper()
        target = e.get("CallTarget")
        params = e.get("Parameters", []) or []
        if target is not None:
            tparts = parts(target.get("MultiPartIdentifier")) if typ(target) == "MultiPartIdentifierCallTarget" else []
            if tparts:
                fq = ".".join(tparts + [ident(e.get("FunctionName")) or ""])
                obj = self.ctx.catalog.find("", "", tparts[-1], ident(e.get("FunctionName")) or "",
                                            self.ctx.default_db, kinds=("function",)) if len(tparts) == 1 else None
                self.ctx.functions.setdefault(fq.lower(), {"name": fq, "steps": [], "supplied": obj is not None})
                self.ctx.functions[fq.lower()]["steps"].append(self.step.id)
                if direct:
                    tf.append(f"function {fq}")
            else:
                # method call on a column or variable (xml .value(), geography .STDistance())
                if typ(target) == "ExpressionCallTarget":
                    self.expr(target.get("Expression"), scope, role, direct, out, tf)
                elif typ(target) == "UserDefinedTypeCallTarget":
                    pass
                if direct:
                    tf.append(f"method {name.lower()}()")
            for p in params:
                self.expr(p, scope, role, direct, out, tf)
            return
        over = e.get("OverClause")
        if direct:
            if over is not None:
                tf.append(f"window function {name}")
            elif name in AGGREGATES:
                tf.append(f"aggregate {name}")
            elif name in NULL_FUNCS:
                tf.append(f"{name} default")
            elif name in STRING_FUNCS:
                tf.append(f"string function {name}")
            elif name in DATE_FUNCS:
                tf.append(f"date function {name}")
            elif name in MATH_FUNCS:
                tf.append(f"math function {name}")
            elif name in SYSTEM_FUNCS:
                tf.append(f"system value {name}()")
            else:
                tf.append(f"function {name}")
        datepart_first = name in ("DATEADD", "DATEDIFF", "DATEDIFF_BIG", "DATEPART", "DATENAME", "DATETRUNC", "DATE_BUCKET")
        for i, p in enumerate(params):
            if datepart_first and i == 0 and typ(p) == "ColumnReferenceExpression":
                continue            # the date part keyword (day, month...) is not a column
            if name == "ISNULL" and i == 1:
                self.expr(p, scope, role, direct, out, tf)
            else:
                self.expr(p, scope, role, direct, out, tf)
        if over is not None:
            r = "window" if direct else role
            for p in over.get("Partitions", []) or []:
                self.expr(p, scope, r, False, out, tf)
            for ob in ((over.get("OrderByClause") or {}).get("OrderByElements") or []):
                self.expr(ob.get("Expression"), scope, r, False, out, tf)
        wg = e.get("WithinGroupClause")
        if wg:
            for ob in ((wg.get("OrderByClause") or {}).get("OrderByElements") or []):
                self.expr(ob.get("Expression"), scope, "window" if direct else role, False, out, tf)

    def bexpr(self, b, scope: Optional[QueryScope], role: str, out: List[Use]) -> List[Use]:
        """All column references in a predicate, as indirect uses with ``role``."""
        t = typ(b)
        if t is None:
            return out
        if t == "BooleanComparisonExpression":
            left, right = b.get("FirstExpression"), b.get("SecondExpression")
            lu = self.expr(left, scope, role, False, [])
            ru = self.expr(right, scope, role, False, [])
            out.extend(lu)
            out.extend(ru)
            self.ctx.comparisons.append({
                "step": self.step.id, "role": role, "span": self.span(b), "text": self.sq(b, 160),
                "op": b.get("ComparisonType", ""), "text_id": self.text_id,
                "left": self._operand(left, lu), "right": self._operand(right, ru)})
            return out
        if t == "InPredicate" and b.get("NotDefined") and b.get("Subquery"):
            sub = self.query((b["Subquery"] or {}).get("QueryExpression"), QueryScope(parent=scope))
            self.expr(b.get("Expression"), scope, role, False, out)
            first = sub.cols[0].uses if sub.cols else []
            self.ctx.not_in.append({"step": self.step.id, "span": self.span(b), "text": self.sq(b, 160),
                                    "uses": [(u.rel, u.col) for u in first if u.direct], "text_id": self.text_id})
            for c in sub.cols:
                out.extend(self._as(u, role, False) for u in c.uses)
            out.extend(self._as(u, role, False) for u in sub.row_uses)
            return out
        if t in ("BooleanBinaryExpression", "DistinctPredicate"):
            for k in ("FirstExpression", "SecondExpression"):
                x = b.get(k)
                if typ(x) and typ(x).startswith("Boolean"):
                    self.bexpr(x, scope, role, out)
                else:
                    self.expr(x, scope, role, False, out)
        elif t in ("BooleanNotExpression", "BooleanParenthesisExpression"):
            self.bexpr(b.get("Expression"), scope, role, out)
        elif t == "BooleanIsNullExpression":
            self.expr(b.get("Expression"), scope, role, False, out)
        elif t == "BooleanTernaryExpression":
            for k in ("FirstExpression", "SecondExpression", "ThirdExpression"):
                self.expr(b.get(k), scope, role, False, out)
        elif t == "LikePredicate":
            for k in ("FirstExpression", "SecondExpression", "EscapeExpression"):
                self.expr(b.get(k), scope, role, False, out)
        elif t == "InPredicate":
            self.expr(b.get("Expression"), scope, role, False, out)
            for v in b.get("Values", []) or []:
                self.expr(v, scope, role, False, out)
            if b.get("Subquery"):
                sub = self.query((b["Subquery"] or {}).get("QueryExpression"), QueryScope(parent=scope))
                for c in sub.cols:
                    out.extend(self._as(u, role, False) for u in c.uses)
                out.extend(self._as(u, role, False) for u in sub.row_uses)
        elif t == "ExistsPredicate":
            sub = self.query((b.get("Subquery") or {}).get("QueryExpression"), QueryScope(parent=scope))
            for c in sub.cols:
                out.extend(self._as(u, role, False) for u in c.uses)
            out.extend(self._as(u, role, False) for u in sub.row_uses)
        elif t == "SubqueryComparisonPredicate":
            self.expr(b.get("Expression"), scope, role, False, out)
            sub = self.query((b.get("Subquery") or {}).get("QueryExpression"), QueryScope(parent=scope))
            for c in sub.cols:
                out.extend(self._as(u, role, False) for u in c.uses)
            out.extend(self._as(u, role, False) for u in sub.row_uses)
        elif t == "FullTextPredicate":
            for c in b.get("Columns", []) or []:
                self.expr(c, scope, role, False, out)
            self.expr(b.get("Value"), scope, role, False, out)
        else:
            for k in self._children(b):
                if typ(k) and typ(k).startswith("Boolean"):
                    self.bexpr(k, scope, role, out)
                else:
                    self.expr(k, scope, role, False, out)
        return out

    def _operand(self, n, uses: List[Use]) -> dict:
        """What one side of a comparison is, for the implicit-conversion and NULL checks."""
        t = typ(unparen(n))
        n = unparen(n)
        d = {"node": t or "", "text": self.sq(n, 80)}
        if t == "ColumnReferenceExpression" and len(uses) == 1:
            d.update(kind="column", rel=uses[0].rel, col=uses[0].col)
        elif t in ("VariableReference", "GlobalVariableExpression") and len(uses) == 1:
            d.update(kind="variable", rel=uses[0].rel, col=uses[0].col)
        elif t == "StringLiteral":
            d.update(kind="literal", type="nvarchar" if n.get("IsNational") else "varchar", value=n.get("Value", ""))
        elif t in ("IntegerLiteral",):
            d.update(kind="literal", type="int", value=n.get("Value", ""))
        elif t in ("NumericLiteral", "MoneyLiteral", "RealLiteral"):
            d.update(kind="literal", type="decimal", value=n.get("Value", ""))
        elif t == "NullLiteral":
            d.update(kind="null")
        elif t in ("FunctionCall", "CastCall", "ConvertCall", "TryCastCall", "TryConvertCall", "LeftFunctionCall",
                   "RightFunctionCall", "BinaryExpression", "CoalesceExpression"):
            cols = [u for u in uses if u.rel.startswith(("table:", "temp:", "tvar:"))]
            d.update(kind="expression", wraps_column=bool(cols), wraps=sorted({u.rel for u in cols}),
                     fn=(ident(n.get("FunctionName")) or t.replace("Call", "").replace("Expression", "")).upper())
            if t in ("CastCall", "ConvertCall", "TryCastCall", "TryConvertCall"):
                d["type"] = data_type_text(n.get("DataType"))
        else:
            d.update(kind="other")
        return d

    # ------------------------------------------------------------------ queries
    def with_ctes(self, wc, scope: Optional[QueryScope]) -> Dict[str, LocalRel]:
        """Analyse the CTEs of a statement; later CTEs may use earlier ones (and themselves)."""
        ctes: Dict[str, LocalRel] = dict(scope.ctes) if scope else {}
        if not wc:
            return ctes
        for cte in wc.get("CommonTableExpressions", []) or []:
            name = ident(cte.get("ExpressionName")) or "cte"
            colnames = [ident(c) for c in cte.get("Columns", []) or []]
            key = self.new_local("cte", name)
            local = LocalRel(key=key, kind="cte", name=name)
            # recursive CTEs: analyse the anchor first, then the whole query with the CTE visible
            q = cte.get("QueryExpression")
            anchor = q
            while typ(anchor) == "BinaryQueryExpression":
                anchor = anchor.get("FirstQueryExpression")
            recursive = any(typ(x) == "NamedTableReference" and norm(obj_name(x.get("SchemaObject")).name) == norm(name)
                            and not obj_name(x.get("SchemaObject")).schema for x in walk(q))
            if recursive:
                first = self.query(anchor, QueryScope(ctes=ctes))
                names = colnames or [c.name for c in first.cols]
                for i, n in enumerate(names):
                    nn = self.ctx.node(rel=key, col=n or f"column{i + 1}", step=self.step.id, op="local",
                                       text_id=self.text_id)
                    local.cols.append((n or f"column{i + 1}", nn.id))
                local.rows = self.ctx.node(rel=key, col=ROWS, step=self.step.id, op="local",
                                           text_id=self.text_id).id
                ctes[norm(name)] = local
                out = self.query(q, QueryScope(ctes=ctes))
                for i, (n, nid) in enumerate(local.cols):
                    if i < len(out.cols):
                        node = self.ctx.nodes[nid]
                        node.uses = list(out.cols[i].uses) + [self._as(u, u.role, False) for u in out.row_uses]
                        node.expr = out.cols[i].expr
                        node.span = out.cols[i].span
                        node.transforms = list(dict.fromkeys(out.cols[i].transforms + ["recursive CTE"]))
                self.ctx.nodes[local.rows].uses = list(out.row_uses)
                self.ctx.nodes[local.rows].note = "recursive"
            else:
                out = self.query(q, QueryScope(ctes=ctes))
                self._fill_local(local, out, colnames)
                ctes[norm(name)] = local
        return ctes

    def _fill_local(self, local: LocalRel, out: QueryOut, colnames=None) -> None:
        names = list(colnames or [])
        for i, c in enumerate(out.cols):
            if c.star is not None and not (names and i < len(names)):
                local.star = True
                nn = self.ctx.node(rel=local.key, col="*", step=self.step.id, op="local", status="partial",
                                   uses=list(c.uses), expr=c.expr, span=c.span, text_id=self.text_id,
                                   note=f"all columns of {c.star.label}, not expanded")
                local.cols.append(("*", nn.id))
                continue
            name = names[i] if i < len(names) and names[i] else (c.name or f"column{i + 1}")
            nn = self.ctx.node(rel=local.key, col=name, step=self.step.id, op="local", uses=list(c.uses),
                               expr=c.expr, span=c.span, transforms=list(dict.fromkeys(c.transforms)),
                               text_id=self.text_id)
            local.cols.append((name, nn.id))
        local.rows = self.ctx.node(rel=local.key, col=ROWS, step=self.step.id, op="local", uses=list(out.row_uses),
                                   text_id=self.text_id).id

    def query(self, q, scope: QueryScope) -> QueryOut:
        t = typ(q)
        if t == "QueryParenthesisExpression":
            return self.query(q.get("QueryExpression"), scope)
        if t == "BinaryQueryExpression":
            a = self.query(q.get("FirstQueryExpression"), QueryScope(parent=scope.parent, ctes=scope.ctes))
            b = self.query(q.get("SecondQueryExpression"), QueryScope(parent=scope.parent, ctes=scope.ctes))
            kind = q.get("BinaryQueryExpressionType", "Union")
            out = QueryOut(sources=a.sources + b.sources, reads=a.reads | b.reads, has_from=a.has_from or b.has_from)
            label = {"Union": "UNION ALL" if q.get("All") else "UNION", "Except": "EXCEPT",
                     "Intersect": "INTERSECT"}.get(kind, kind.upper())
            for i, ca in enumerate(a.cols):
                uses = list(ca.uses)
                if i < len(b.cols):
                    if kind == "Union":
                        uses += b.cols[i].uses
                    else:
                        uses += [self._as(u, "filter", False) for u in b.cols[i].uses]
                out.cols.append(OutCol(name=ca.name, uses=uses, expr=ca.expr, span=ca.span,
                                       transforms=ca.transforms + [label], star=ca.star))
            out.row_uses = a.row_uses + ([self._as(u, "filter", False) for u in b.row_uses] if kind != "Union"
                                         else b.row_uses)
            if kind != "Union" or not q.get("All"):
                out.distinct = True
            return out
        out = QueryOut()
        if t != "QuerySpecification":
            return out
        fc = q.get("FromClause")
        if fc:
            out.has_from = True
            for tr in fc.get("TableReferences", []) or []:
                self.table_ref(tr, scope, out)
        out.sources = list(scope.sources)
        wh = q.get("WhereClause")
        if wh and wh.get("SearchCondition"):
            self.bexpr(wh.get("SearchCondition"), scope, "filter", out.row_uses)
        gb = q.get("GroupByClause")
        if gb:
            for g in walk(gb):
                if typ(g) == "ExpressionGroupingSpecification":
                    self.expr(g.get("Expression"), scope, "group", False, out.row_uses)
        hv = q.get("HavingClause")
        if hv:
            self.bexpr(hv.get("SearchCondition"), scope, "having", out.row_uses)
        top = q.get("TopRowFilter")
        if top:
            out.top = True
            self.expr(top.get("Expression"), scope, "top", False, out.row_uses)
        if q.get("UniqueRowFilter") == "Distinct":
            out.distinct = True
        for el in q.get("SelectElements", []) or []:
            et = typ(el)
            if et == "SelectScalarExpression":
                tf: List[str] = []
                uses = self.expr(el.get("Expression"), scope, "value", True, None, tf)
                cn = el.get("ColumnName")
                name = None
                if cn:
                    name = ident(cn.get("Identifier")) if cn.get("Identifier") else cn.get("Value")
                if not name and typ(el.get("Expression")) == "ColumnReferenceExpression":
                    ids = parts(el["Expression"].get("MultiPartIdentifier"))
                    name = ids[-1] if ids else None
                oc = OutCol(name=name, uses=uses, expr=self.sq(el.get("Expression"), 400),
                            span=self.span(el), transforms=tf, node=el)
                if name:
                    scope.aliases[norm(name)] = oc
                out.cols.append(oc)
            elif et == "SelectSetVariable":
                tf = []
                uses = self.expr(el.get("Expression"), scope, "value", True, None, tf)
                var = (el.get("Variable") or {}).get("Name") or ""
                out.cols.append(OutCol(name=var, uses=uses, expr=self.sq(el.get("Expression"), 400),
                                       span=self.span(el), transforms=tf, assign_var=var,
                                       assign_kind=el.get("AssignmentKind", "Equals"), node=el))
            elif et == "SelectStarExpression":
                self._star(el, scope, out)
        if q.get("OrderByClause") and (out.top or q.get("OffsetClause")):
            out.order_by = True
            for ob in q["OrderByClause"].get("OrderByElements", []) or []:
                ex = ob.get("Expression")
                if typ(ex) == "ColumnReferenceExpression" and len(parts(ex.get("MultiPartIdentifier"))) == 1 \
                        and norm(parts(ex.get("MultiPartIdentifier"))[0]) in scope.aliases:
                    oc = scope.aliases[norm(parts(ex.get("MultiPartIdentifier"))[0])]
                    out.row_uses.extend(self._as(u, "order", False) for u in oc.uses)
                else:
                    self.expr(ex, scope, "order", False, out.row_uses)
        oc_ = q.get("OffsetClause")
        if oc_:
            self.expr(oc_.get("OffsetExpression"), scope, "top", False, out.row_uses)
            self.expr(oc_.get("FetchExpression"), scope, "top", False, out.row_uses)
        # every source's row set decides which rows exist
        for src in scope.sources:
            if src.local is not None:
                if src.local.rows is not None:
                    out.row_uses.append(Use(rel=src.local.key, col=ROWS, role="rows", direct=False,
                                            local=src.local.rows, span=src.span, text=src.label))
            else:
                out.row_uses.append(Use(rel=src.rel.key, col=ROWS, role="rows", direct=False, span=src.span,
                                        text=src.label))
                self.reads.add(src.rel.key)
        out.reads |= {s.rel.key for s in scope.sources if s.rel is not None}
        if not gb and out.cols and all(any(x.startswith("aggregate") for x in c.transforms) or
                                       not c.uses for c in out.cols if c.star is None) \
                and any(any(x.startswith("aggregate") for x in c.transforms) for c in out.cols):
            out.aggregate_only = True
        return out

    def _star(self, el, scope: QueryScope, out: QueryOut) -> None:
        qual = parts(el.get("Qualifier")) if el.get("Qualifier") else []
        sources = [self._match_qualified(scope, qual)] if qual else list(scope.sources)
        for src in sources:
            if src is None:
                continue
            if src.local is not None:
                for name, nid in src.local.cols:
                    if name == ROWS:
                        continue
                    if name == "*":
                        out.cols.append(OutCol(name="*", uses=[Use(rel=src.local.key, col="*", role="value", direct=True,
                                                                   local=nid, status="partial", text=f"{src.label}.*")],
                                               expr=f"{src.label}.*", span=self.span(el), star=src))
                        continue
                    out.cols.append(OutCol(name=name, uses=[Use(rel=src.local.key, col=name, role="value", direct=True,
                                                                local=nid, span=self.span(el),
                                                                text=f"{src.label}.{name}")],
                                           expr=f"{src.label}.{name}", span=self.span(el)))
                if not src.local.star:
                    self.ctx.star_expanded += 1
                else:
                    self.ctx.star_unexpanded += 1
            elif src.rel.complete and src.rel.columns:
                self.ctx.star_expanded += 1
                for c in src.rel.columns:
                    out.cols.append(OutCol(name=c.name, uses=[Use(rel=src.rel.key, col=c.name, role="value",
                                                                  direct=True, span=self.span(el),
                                                                  text=f"{src.label}.{c.name}")],
                                           expr=f"{src.label}.{c.name}", span=self.span(el)))
                self.reads.add(src.rel.key)
            else:
                self.ctx.star_unexpanded += 1
                self.reads.add(src.rel.key)
                out.cols.append(OutCol(name="*", uses=[Use(rel=src.rel.key, col="*", role="value", direct=True,
                                                           span=self.span(el), status="partial",
                                                           text=f"{src.label}.*")],
                                       expr=f"{src.label}.*", span=self.span(el), star=src))

    # ------------------------------------------------------------------ FROM clause
    def named_source(self, tr: dict, scope: QueryScope) -> Source:
        on = obj_name(tr.get("SchemaObject"))
        alias = ident(tr.get("Alias"))
        sp = self.span(tr)
        if not on.schema and not on.database and not on.server:
            local = scope.cte(on.name)
            if local is not None:
                return Source(alias=alias, names={norm(on.name)}, local=local, span=sp)
        rel = self.ctx.table_relation(on, self.step)
        names = {norm(on.name)}
        if on.schema:
            names.add(f"{norm(on.schema)}.{norm(on.name)}")
        if on.database:
            names.add(f"{norm(on.database)}.{norm(on.schema)}.{norm(on.name)}")
        for h in tr.get("TableHints", []) or []:
            kind = h.get("HintKind", "")
            if kind:
                self.ctx.hints.append({"step": self.step.id, "hint": kind, "relation": rel.key, "span": sp})
        expanded = self._expand_view(rel, on, alias, sp, scope)
        if expanded is not None:
            return expanded
        return Source(alias=alias, names=names, rel=rel, span=sp)

    def _expand_view(self, rel: Relation, on, alias, sp, scope) -> Optional[Source]:
        """Project mode: a view is read through its definition, so lineage reaches its base tables."""
        obj = rel.catalog
        if not (self.ctx.project and obj is not None and obj.kind == "view" and obj.node is not None
                and obj.text is not None) or obj.full.lower() in self._expanding or len(self._expanding) > 4:
            return None
        sel = obj.node.get("SelectStatement") or {}
        local = self._expand_definition(obj, sel, {}, "view")
        if local is None:
            return None
        names = {norm(on.name)}
        if on.schema:
            names.add(f"{norm(on.schema)}.{norm(on.name)}")
        self.reads.add(rel.key)
        self.ctx.expanded_objects.setdefault(obj.full.lower(), {"name": obj.display, "kind": "view", "steps": []})
        self.ctx.expanded_objects[obj.full.lower()]["steps"].append(self.step.id)
        return Source(alias=alias, names=names, local=local, span=sp)

    def _expand_definition(self, obj, select, param_map, kind) -> Optional[LocalRel]:
        """Analyse a view's or inline function's SELECT in its own file, as a statement-local relation."""
        text_id = f"{kind}:{obj.full.lower()}"
        self.ctx.texts.setdefault(text_id, obj.text)
        saved = (self.text, self.text_id, self.param_map)
        self.text, self.text_id, self.param_map = obj.text, text_id, param_map
        self._expanding.append(obj.full.lower())
        try:
            ctes = self.with_ctes(select.get("WithCtesAndXmlNamespaces"), None)
            out = self.query(select.get("QueryExpression"), QueryScope(ctes=ctes))
            local = LocalRel(key=self.new_local(kind, obj.display), kind=kind, name=obj.display)
            names = [c.name for c in obj.columns] if kind == "view" and obj.columns else None
            self._fill_local(local, out, names)
            for _, nid in local.cols:
                self.ctx.nodes[nid].note = f"through {kind} {obj.display}"
            return local
        finally:
            self._expanding.pop()
            self.text, self.text_id, self.param_map = saved

    def table_ref(self, tr: dict, scope: QueryScope, out: QueryOut, nullable: bool = False, join: str = "") -> None:
        t = typ(tr)
        sp = self.span(tr)
        if t == "NamedTableReference":
            src = self.named_source(tr, scope)
            src.nullable, src.join = nullable, join
            scope.sources.append(src)
            if src.rel is not None:
                self.reads.add(src.rel.key)
        elif t == "QualifiedJoin":
            jt = tr.get("QualifiedJoinType", "Inner")
            left_null = jt in ("RightOuter", "FullOuter")
            right_null = jt in ("LeftOuter", "FullOuter")
            before = len(scope.sources)
            self.table_ref(tr.get("FirstTableReference"), scope, out, nullable or left_null, join)
            if left_null:
                for s in scope.sources[before:]:
                    s.nullable = True
            self.table_ref(tr.get("SecondTableReference"), scope, out, nullable or right_null,
                           {"Inner": "INNER JOIN", "LeftOuter": "LEFT JOIN", "RightOuter": "RIGHT JOIN",
                            "FullOuter": "FULL JOIN"}.get(jt, jt))
            if tr.get("SearchCondition"):
                self.bexpr(tr.get("SearchCondition"), scope, "join", out.row_uses)
        elif t == "UnqualifiedJoin":
            jt = tr.get("UnqualifiedJoinType", "CrossJoin")
            self.table_ref(tr.get("FirstTableReference"), scope, out, nullable, join)
            label = {"CrossJoin": "CROSS JOIN", "CrossApply": "CROSS APPLY", "OuterApply": "OUTER APPLY"}.get(jt, jt)
            self.table_ref(tr.get("SecondTableReference"), scope, out, nullable or jt == "OuterApply", label)
        elif t == "JoinParenthesisTableReference":
            self.table_ref(tr.get("Join"), scope, out, nullable, join)
        elif t == "QueryDerivedTable":
            alias = ident(tr.get("Alias")) or "derived"
            # APPLY may refer to tables to its left: give it the current scope as parent
            inner_scope = QueryScope(parent=scope if join in ("CROSS APPLY", "OUTER APPLY") else scope.parent,
                                     ctes=scope.ctes)
            sub = self.query(tr.get("QueryExpression"), inner_scope)
            local = LocalRel(key=self.new_local("derived", alias), kind="derived", name=alias)
            self._fill_local(local, sub, [ident(c) for c in tr.get("Columns", []) or []])
            scope.sources.append(Source(alias=alias, names={norm(alias)}, local=local, nullable=nullable,
                                        join=join, span=sp))
        elif t == "InlineDerivedTable":
            alias = ident(tr.get("Alias")) or "values"
            colnames = [ident(c) for c in tr.get("Columns", []) or []]
            local = LocalRel(key=self.new_local("derived", alias), kind="values", name=alias)
            rows = tr.get("RowValues", []) or []
            width = max((len(r.get("ColumnValues", []) or []) for r in rows), default=0)
            for i in range(width):
                uses, tf = [], ["constant list (VALUES)"]
                for r in rows:
                    vals = r.get("ColumnValues", []) or []
                    if i < len(vals):
                        self.expr(vals[i], scope, "value", True, uses, tf)
                name = colnames[i] if i < len(colnames) else f"column{i + 1}"
                nn = self.ctx.node(rel=local.key, col=name, step=self.step.id, op="local", uses=uses,
                                   transforms=["constant list (VALUES)"], text_id=self.text_id)
                local.cols.append((name, nn.id))
            local.rows = self.ctx.node(rel=local.key, col=ROWS, step=self.step.id, op="local",
                                       text_id=self.text_id).id
            scope.sources.append(Source(alias=alias, names={norm(alias)}, local=local, nullable=nullable, join=join, span=sp))
        elif t == "VariableTableReference":
            name = (tr.get("Variable") or {}).get("Name") or ""
            alias = ident(tr.get("Alias"))
            rel = self.ctx.relations.get(f"tvar:{self.ns}{norm(name)}") or self.ctx.table_variable(name, self.ns)
            scope.sources.append(Source(alias=alias, names={norm(name)}, rel=rel, nullable=nullable, join=join, span=sp))
            self.reads.add(rel.key)
        elif t == "SchemaObjectFunctionTableReference":
            on = obj_name(tr.get("SchemaObject"))
            alias = ident(tr.get("Alias")) or on.name
            obj = self.ctx.catalog.find("", on.database, on.schema, on.name, self.ctx.default_db,
                                        [self.ctx.default_schema], kinds=("function",))
            key = f"function:{norm(on.database or self.ctx.default_db)}.{norm(on.schema or 'dbo')}.{norm(on.name)}"
            rel = self.ctx.relation(key, "function", ".".join(p for p in (on.schema or "dbo", on.name)),
                                    endpoint=self.ctx.endpoint(None, on.database or self.ctx.default_db or None,
                                                               on.schema or "dbo", on.name), catalog=obj)
            if obj and obj.columns and not rel.complete:
                rel.columns = [Column(c.name, c.type, c.nullable) for c in obj.columns]
                rel.complete = True
            fq = rel.name
            self.ctx.functions.setdefault(fq.lower(), {"name": fq, "steps": [], "supplied": obj is not None,
                                                       "table": True})
            self.ctx.functions[fq.lower()]["steps"].append(self.step.id)
            args = tr.get("Parameters", []) or []
            if (self.ctx.project and obj is not None and obj.function_kind == "inline" and obj.node is not None
                    and obj.full.lower() not in self._expanding and len(self._expanding) <= 4):
                pmap = {}
                for i, prm in enumerate(obj.params):
                    if i < len(args):
                        pmap[norm(prm["name"])] = self.expr(args[i], scope, "value", True)
                sel = ((obj.node.get("ReturnType") or {}).get("SelectStatement")) or {}
                local = self._expand_definition(obj, sel, pmap, "function")
                if local is not None:
                    self.ctx.expanded_objects.setdefault(obj.full.lower(), {"name": obj.display, "kind": "function",
                                                                            "steps": []})
                    self.ctx.expanded_objects[obj.full.lower()]["steps"].append(self.step.id)
                    self.reads.add(rel.key)
                    scope.sources.append(Source(alias=alias, names={norm(alias), norm(on.name)}, local=local,
                                                nullable=nullable, join=join, span=sp))
                    return
            for p in args:
                self.expr(p, scope, "argument", False, out.row_uses)
            scope.sources.append(Source(alias=alias, names={norm(alias), norm(on.name)}, rel=rel,
                                        nullable=nullable, join=join, span=sp))
            self.reads.add(rel.key)
        elif t in ("OpenRowsetTableReference", "OpenQueryTableReference", "AdHocTableReference", "BulkOpenRowset",
                   "InternalOpenRowset", "OpenRowsetCosmos"):
            alias = ident(tr.get("Alias")) or "remote"
            rel = self._remote(tr)
            scope.sources.append(Source(alias=alias, names={norm(alias)}, rel=rel, nullable=nullable, join=join, span=sp))
            self.reads.add(rel.key)
        elif t in ("GlobalFunctionTableReference", "BuiltInFunctionTableReference", "OpenJsonTableReference",
                   "OpenXmlTableReference"):
            alias = ident(tr.get("Alias")) or "fn"
            uses: List[Use] = []
            for k in ("Parameters", "Variable", "RowPattern"):
                v = tr.get(k)
                for x in (v if isinstance(v, list) else [v]):
                    if isinstance(x, dict):
                        self.expr(x, scope, "value", True, uses)
            cols = [ident(c.get("ColumnDefinition", {}).get("ColumnIdentifier")) or ""
                    for c in tr.get("SchemaDeclarationItems", []) or []]
            if not cols:
                fname = (ident(tr.get("Name")) or "").upper()
                cols = ["value"] if fname in ("STRING_SPLIT", "GENERATE_SERIES") else (
                    ["key", "value", "type"] if t == "OpenJsonTableReference" else ["value"])
                if fname == "STRING_SPLIT":
                    cols.append("ordinal")
            local = LocalRel(key=self.new_local("derived", alias), kind="function", name=alias)
            fname = (ident(tr.get("Name")) or t.replace("TableReference", "")).upper()
            for c in cols:
                nn = self.ctx.node(rel=local.key, col=c, step=self.step.id, op="local", uses=list(uses),
                                   transforms=[f"table function {fname}"], text_id=self.text_id)
                local.cols.append((c, nn.id))
            local.rows = self.ctx.node(rel=local.key, col=ROWS, step=self.step.id, op="local",
                                       uses=[self._as(u, "argument", False) for u in uses],
                                       text_id=self.text_id).id
            scope.sources.append(Source(alias=alias, names={norm(alias)}, local=local, nullable=nullable, join=join, span=sp))
        elif t in ("PivotedTableReference", "UnpivotedTableReference"):
            inner = QueryScope(parent=scope.parent, ctes=scope.ctes)
            tmp = QueryOut()
            self.table_ref(tr.get("TableReference"), inner, tmp)
            alias = ident(tr.get("Alias")) or "pivot"
            local = LocalRel(key=self.new_local("derived", alias), kind="pivot", name=alias)
            row_uses = list(tmp.row_uses)
            if t == "PivotedTableReference":
                value_uses = []
                for vc in tr.get("ValueColumns", []) or []:
                    u = self.resolve(vc, inner, "value", True)
                    if u:
                        value_uses.append(u)
                pivot_u = self.resolve(tr.get("PivotColumn"), inner, "case", False) if tr.get("PivotColumn") else None
                used = {norm(u.col) for u in value_uses} | ({norm(pivot_u.col)} if pivot_u else set())
                for src in inner.sources:
                    names = src.local.names if src.local else [c.name for c in src.rel.columns]
                    for n in names:
                        if norm(n) in used or n in (ROWS, "*"):
                            continue
                        u = self._use_from(src, n, "value", True, src.span, f"{src.label}.{n}")
                        nn = self.ctx.node(rel=local.key, col=n, step=self.step.id, op="local", uses=[u],
                                           text_id=self.text_id)
                        local.cols.append((n, nn.id))
                        row_uses.append(self._as(u, "group", False))
                for ic in tr.get("InColumns", []) or []:
                    n = ident(ic) or ""
                    uses = list(value_uses) + ([pivot_u] if pivot_u else [])
                    nn = self.ctx.node(rel=local.key, col=n, step=self.step.id, op="local", uses=uses,
                                       transforms=["PIVOT"], text_id=self.text_id)
                    local.cols.append((n, nn.id))
            else:
                in_uses = [u for u in (self.resolve(c, inner, "value", True) for c in tr.get("InColumns", []) or []) if u]
                used = {norm(u.col) for u in in_uses}
                for src in inner.sources:
                    names = src.local.names if src.local else [c.name for c in src.rel.columns]
                    for n in names:
                        if norm(n) in used or n in (ROWS, "*"):
                            continue
                        u = self._use_from(src, n, "value", True, src.span, f"{src.label}.{n}")
                        nn = self.ctx.node(rel=local.key, col=n, step=self.step.id, op="local", uses=[u],
                                           text_id=self.text_id)
                        local.cols.append((n, nn.id))
                vname = ident(tr.get("ValueColumn")) or "value"
                pname = ident(tr.get("PivotColumn")) or "name"
                nn = self.ctx.node(rel=local.key, col=vname, step=self.step.id, op="local", uses=in_uses,
                                   transforms=["UNPIVOT"], text_id=self.text_id)
                local.cols.append((vname, nn.id))
                nn = self.ctx.node(rel=local.key, col=pname, step=self.step.id, op="local",
                                   transforms=["UNPIVOT column names"], text_id=self.text_id)
                local.cols.append((pname, nn.id))
            for src in inner.sources:
                if src.rel is not None:
                    row_uses.append(Use(rel=src.rel.key, col=ROWS, role="rows", direct=False, span=src.span))
            local.rows = self.ctx.node(rel=local.key, col=ROWS, step=self.step.id, op="local", uses=row_uses,
                                       text_id=self.text_id).id
            scope.sources.append(Source(alias=alias, names={norm(alias)}, local=local, nullable=nullable, join=join, span=sp))
        elif t == "DataModificationTableReference":
            alias = ident(tr.get("Alias")) or "dml"
            local = LocalRel(key=self.new_local("derived", alias), kind="derived", name=alias, star=True)
            local.cols.append(("*", self.ctx.node(rel=local.key, col="*", step=self.step.id, op="local",
                                                  status="partial", text_id=self.text_id).id))
            scope.sources.append(Source(alias=alias, names={norm(alias)}, local=local, span=sp))
        else:
            alias = ident(tr.get("Alias")) if isinstance(tr, dict) else None
            local = LocalRel(key=self.new_local("derived", alias or "source"), kind="derived",
                             name=alias or (t or "source"), star=True)
            local.cols.append(("*", self.ctx.node(rel=local.key, col="*", step=self.step.id, op="local",
                                                  status="unresolved", text_id=self.text_id,
                                                  note=f"{t} is not read by this version").id))
            scope.sources.append(Source(alias=alias, names={norm(alias or '')}, local=local, span=sp))

    def _remote(self, tr: dict) -> Relation:
        t = typ(tr)
        if t == "OpenQueryTableReference":
            server = ident(tr.get("LinkedServer")) or "linked server"
            query = (tr.get("Query") or {}).get("Value", "")
            key = f"remote:openquery:{norm(server)}:{abs(hash(query)) % 10 ** 8}"
            rel = self.ctx.relation(key, "remote", f"OPENQUERY({server}, …)",
                                    endpoint=self.ctx.endpoint(server, None, None, None))
            rel.note = " ".join(query.split())[:300]
            return rel
        if t == "OpenRowsetTableReference":
            provider = (tr.get("ProviderName") or {}).get("Value", "")
            ds = (tr.get("DataSource") or {}).get("Value", "") or (tr.get("ProviderString") or {}).get("Value", "")
            obj = obj_name(tr.get("Object")) if tr.get("Object") else None
            query = (tr.get("Query") or {}).get("Value", "")
            server = None
            m = re.search(r"(?:server|data source)\s*=\s*([^;]+)", ds or "", re.I)
            if m:
                server = m.group(1).strip()
            label = f"OPENROWSET({provider}, …{', ' + obj.display if obj else ''})"
            key = f"remote:openrowset:{norm(provider)}:{norm(server or ds)}:{norm(obj.display if obj else query)[:120]}"
            rel = self.ctx.relation(key, "remote", label, endpoint=self.ctx.endpoint(
                server, obj.database if obj else None, obj.schema if obj else None, obj.name if obj else None))
            if query:
                rel.note = " ".join(query.split())[:300]
            return rel
        if t == "AdHocTableReference":
            ds = tr.get("DataSource") or {}
            init = (ds.get("Init") or {}).get("Value", "")
            obj = obj_name((tr.get("Object") or {}).get("SchemaObjectName") or tr.get("Object"))
            m = re.search(r"(?:server|data source)\s*=\s*([^;]+)", init or "", re.I)
            server = m.group(1).strip() if m else None
            key = f"remote:opendatasource:{norm(server or init)}:{norm(obj.display)}"
            return self.ctx.relation(key, "remote", f"OPENDATASOURCE(…).{obj.display}",
                                     endpoint=self.ctx.endpoint(server, obj.database, obj.schema, obj.name))
        if t == "BulkOpenRowset":
            files = [(f or {}).get("Value", "") for f in tr.get("DataFiles", []) or []]
            path = files[0] if files else "file"
            rel = self.ctx.relation(f"file:{norm(path)}", "file", path)
            rel.endpoint = {"system": "File", "server": None, "port": None, "database": None, "schema": None,
                            "object": None, "path": path, "url": None, "container": None}
            return rel
        return self.ctx.relation("remote:unknown", "remote", "remote source")
