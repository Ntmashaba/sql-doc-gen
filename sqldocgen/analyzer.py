"""Analysis entry point: find the procedures in parsed scripts and analyse each one.

The analysis runs in up to two passes. The first pass analyses the procedure with dynamic
SQL left opaque; the dataflow it produces is then used to rebuild the text of each dynamic
statement, which is parsed with ScriptDom. If any of it parses, the second pass analyses the
procedure again with that SQL in place, numbered under the EXEC that runs it.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import dataflow
from .catalog import PROCEDURE_STATEMENTS, Catalog, _params
from .dynamic import parse_param_definitions, rebuild
from .engine import Ctx, norm
from .model import Column
from .program import ProgramBuilder, exec_spec_of, is_dynamic_exec
from .statements import StatementAnalyzer
from .syntax import Text, ident, obj_name, typ, walk

DDL_ONLY = {"CreateTableStatement", "CreateViewStatement", "CreateOrAlterViewStatement", "AlterViewStatement",
            "CreateFunctionStatement", "CreateOrAlterFunctionStatement", "AlterFunctionStatement",
            "CreateIndexStatement", "CreateSynonymStatement", "CreateTypeTableStatement", "CreateSchemaStatement",
            "AlterTableAddTableElementStatement", "AlterTableConstraintModificationStatement",
            "CreateTriggerStatement", "CreateOrAlterTriggerStatement", "UseStatement",
            "PredicateSetStatement", "CreateRoleStatement", "GrantStatement", "CreateUserStatement",
            "ExtendedPropertyStatement", "CreateSequenceStatement", "AlterTableSetStatement"}
MAX_STEPS = 6000
MAX_CALL_DEPTH = 3


@dataclass
class Unit:
    """Something to document: a stored procedure, or a script with executable statements."""
    name: str
    schema: str
    database: str
    kind: str                                 # procedure | script
    statements: List[dict]
    text: Text
    source: str
    node: Optional[dict] = None
    params: List[dict] = field(default_factory=list)
    options: List[str] = field(default_factory=list)
    errors: List[dict] = field(default_factory=list)
    span: Optional[Tuple[int, int]] = None

    @property
    def display(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


def find_units(tree: dict, text: Text, source: str, errors: List[dict], script_name: str) -> List[Unit]:
    units: List[Unit] = []
    loose: List[dict] = []
    current_db = ""
    if not tree:
        return units
    for batch in tree.get("Batches", []) or []:
        for st in batch.get("Statements", []) or []:
            t = typ(st)
            if t == "UseStatement":
                current_db = ident(st.get("DatabaseName")) or current_db
            if t in PROCEDURE_STATEMENTS:
                on = obj_name((st.get("ProcedureReference") or {}).get("Name"))
                opts = []
                for o in st.get("Options", []) or []:
                    kind = o.get("OptionKind", "")
                    if kind:
                        opts.append({"Recompile": "WITH RECOMPILE", "Encryption": "WITH ENCRYPTION",
                                     "ExecuteAs": "EXECUTE AS", "NativeCompilation": "NATIVE_COMPILATION",
                                     "SchemaBinding": "SCHEMABINDING"}.get(kind, kind))
                units.append(Unit(name=on.name, schema=on.schema or "dbo", database=on.database or current_db,
                                  kind="procedure",
                                  statements=(st.get("StatementList") or {}).get("Statements", []) or [],
                                  text=text, source=source, node=st, params=_params(st, text), options=opts,
                                  errors=errors, span=text.span(st)))
            elif t not in DDL_ONLY:
                loose.append(st)
    if not units and loose:
        units.append(Unit(name=script_name, schema="", database=current_db, kind="script", statements=loose,
                          text=text, source=source, errors=errors))
    return units


@dataclass
class Analysis:
    unit: Unit
    ctx: Ctx
    builder: ProgramBuilder
    flow: dict
    dynamic: Dict[str, dict]
    passes: int = 1


ParseFn = Callable[[List[Tuple[str, str]]], Dict[str, dict]]


def _run_with_big_stack(fn, *args):
    """Deeply nested expressions (long string concatenations) need a deep Python stack."""
    result, error = [], []

    def target():
        try:
            result.append(fn(*args))
        except BaseException as exc:          # re-raised in the caller's thread
            error.append(exc)

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, 60000))
    old_size = threading.stack_size()
    try:
        threading.stack_size(512 * 1024 * 1024)
    except (ValueError, RuntimeError):
        pass
    try:
        th = threading.Thread(target=target, name="sqldocgen-analysis")
        th.start()
        th.join()
    finally:
        try:
            threading.stack_size(old_size)
        except (ValueError, RuntimeError):
            pass
        sys.setrecursionlimit(old_limit)
    if error:
        raise error[0]
    return result[0]


def analyze(unit: Unit, catalog: Catalog, parse_fn: Optional[ParseFn] = None, project: bool = False,
            default_db: str = "") -> Analysis:
    return _run_with_big_stack(_analyze, unit, catalog, parse_fn, project, default_db)


def _one_pass(unit: Unit, catalog: Catalog, project: bool, default_db: str,
              dyn_expansions: Dict[Tuple[str, int], List[Tuple[list, str, str, dict]]],
              extra_texts: Dict[str, Text], placeholders: Optional[Dict[str, str]] = None
              ) -> Tuple[Ctx, ProgramBuilder, dict]:
    ctx = Ctx(catalog, default_db or unit.database, unit.schema or "dbo", project)
    ctx.texts["main"] = unit.text
    ctx.placeholders = dict(placeholders or {})
    ctx.texts.update(extra_texts)
    for p in unit.params:
        rel = ctx.variable(p["name"], "", kind="parameter", data_type=p["type"])
        rel.output = p["output"]
        rel.default = p["default"]
        if p.get("readonly"):
            # table-valued parameter: a table the caller passes in
            tt = catalog.find("", "", "", p["type"].split("(")[0].split(".")[-1], kinds=("table-type",))
            tv = ctx.relation(f"tvar:{norm(p['name'])}", "table-parameter", p["name"])
            if tt:
                tv.columns = [Column(c.name, c.type, c.nullable) for c in tt.columns]
                tv.complete = True
    budget = {"steps": 0}

    def expand(step, env):
        key = (env.namespace, id(step.node))
        items = []
        if key in dyn_expansions:
            ns = f"{env.namespace}dyn@{step.label}/"
            variants = dyn_expansions[key]
            for k, (stmts, text_id, info) in enumerate(variants):
                ctx.texts.setdefault(text_id, extra_texts[text_id])
                label = "Dynamic SQL" + (f" (variant {k + 1} of {len(variants)})" if len(variants) > 1 else "")
                items.append((stmts, text_id, ns, "dynamic", label))
                ctx.expanded[step.id] = {"namespace": ns, "params": info.get("params", []),
                                         "callee": "dynamic SQL"}
            return items
        if project and step.kind in ("exec", "insert-exec") and not is_dynamic_exec(step.node):
            spec = exec_spec_of(step.node)
            ent = spec.get("ExecutableEntity") or {}
            pr = ((ent.get("ProcedureReference") or {}).get("ProcedureReference") or {})
            if not pr.get("Name") or ident(spec.get("LinkedServer")):
                return []
            on = obj_name(pr["Name"])
            callee = catalog.find("", on.database, on.schema, on.name, ctx.default_db, [ctx.default_schema],
                                  kinds=("procedure",))
            if callee is None or callee.node is None:
                return []
            chain = env.namespace.lower()
            if f"{callee.display.lower()}@" in chain or chain.count("/") >= MAX_CALL_DEPTH:
                return []
            if callee.display.lower() == unit.display.lower() and not chain:
                return []
            stmts = (callee.node.get("StatementList") or {}).get("Statements", []) or []
            size = sum(1 for _ in walk({"$": "x", "s": stmts}) if typ(_) and typ(_).endswith("Statement"))
            if budget["steps"] + size > MAX_STEPS:
                return []
            budget["steps"] += size
            text_id = f"proc:{callee.full.lower()}"
            ctx.texts[text_id] = callee.text
            ns = f"{env.namespace}{callee.display}@{step.label}/"
            ctx.expanded[step.id] = {"namespace": ns, "params": callee.params, "callee": callee.display}
            return [(stmts, text_id, ns, f"call:{callee.display}", f"EXEC {callee.display} (expanded)")]
        return []

    builder = ProgramBuilder(ctx.texts, expand=expand)
    builder.build(unit.statements, "main", f"{unit.display}" if unit.kind == "procedure" else unit.name)
    ctx.steps = builder.steps
    ctx.step_by_id = builder.by_id
    for st in ctx.steps:
        if typ(st.node) == "OpenCursorStatement":
            cur = ((st.node.get("Cursor") or {}).get("Name") or {})
            cname = ident(cur.get("Identifier")) if cur.get("Identifier") else cur.get("Value") or ""
            ctx.opened_cursors.add(f"{st.namespace}{norm(cname)}")
    for st in ctx.steps:
        StatementAnalyzer(ctx, st).run()
    flow = dataflow.solve(ctx, builder.succ)
    return ctx, builder, flow


def _analyze(unit: Unit, catalog: Catalog, parse_fn: Optional[ParseFn], project: bool, default_db: str) -> Analysis:
    dyn_expansions: Dict[Tuple[str, int], list] = {}
    extra_texts: Dict[str, Text] = {}
    ctx, builder, flow = _one_pass(unit, catalog, project, default_db, dyn_expansions, extra_texts)
    dynamic: Dict[str, dict] = {}
    passes = 1
    dyn_steps = [s for s in ctx.steps if s.kind == "exec-dynamic" or (s.kind == "insert-exec" and is_dynamic_exec(s.node))]
    if dyn_steps:
        requests = []
        registry: Dict[str, str] = {}          # placeholder token -> the run-time value it stands for
        for st in dyn_steps:
            texts, status, labels, unsafe = rebuild(ctx, flow, st, registry)
            info = {"status": status, "variants": texts, "placeholders": labels, "parsed": False, "errors": [],
                    "step": st.label, "namespace": st.namespace, "unsafe": unsafe}
            dynamic[st.label] = info
            for k, t in enumerate(texts):
                requests.append((f"dyn:{st.label}:{k + 1}", t, st, info))
        if requests and parse_fn is not None:
            results = parse_fn([(rid, t) for rid, t, _, _ in requests])
            per_step: Dict[str, list] = {}
            for rid, t, st, info in requests:
                res = results.get(rid) or {}
                tree = res.get("tree")
                errs = res.get("errors") or []
                stmts = [s for b in (tree or {}).get("Batches", []) or [] for s in b.get("Statements", []) or []]
                per_step.setdefault(st.label, []).append((rid, t, st, info, stmts, errs))
            for label, variants in per_step.items():
                info = variants[0][3]
                clean = [v for v in variants if v[4] and not v[5]]
                # analyse the variants that parse; when none parse cleanly, what ScriptDom recovered
                use = clean or [v for v in variants if v[4]]
                for rid, t, st, _, stmts, errs in use:
                    info["parsed"] = True
                    params = []
                    if (st.detail.get("dynamic") or {}).get("form") == "sp_executesql":
                        params = parse_param_definitions(_literal_params(st))
                    extra_texts[rid] = Text(t)
                    dyn_expansions.setdefault((st.namespace, id(st.node)), []).append((stmts, rid, {"params": params}))
                for _, _, _, _, stmts, errs in variants:
                    info["errors"].extend(e.get("message", "") for e in errs)
                    if not stmts and not errs:
                        info["errors"].append("nothing to run")
                info["errors"] = list(dict.fromkeys(info["errors"]))
                if not use:
                    info["status"] = "unresolved"
                elif len(clean) < len(variants) and info["status"] == "resolved":
                    info["status"] = "partial"
                info["variantsParsed"] = len(clean)
        if dyn_expansions:
            ctx, builder, flow = _one_pass(unit, catalog, project, default_db, dyn_expansions, extra_texts, registry)
            passes = 2
    return Analysis(unit=unit, ctx=ctx, builder=builder, flow=flow, dynamic=dynamic, passes=passes)


def _literal_params(step) -> str:
    """The parameter definition string of sp_executesql when it is a literal."""
    ent = exec_spec_of(step.node).get("ExecutableEntity") or {}
    params = ent.get("Parameters", []) or []
    if len(params) > 1:
        v = params[1].get("ParameterValue") or {}
        if typ(v) == "StringLiteral":
            return v.get("Value", "")
    return ""
