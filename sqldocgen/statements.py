"""What each statement reads and writes, column by column.

``StatementAnalyzer.run()`` handles one step. Every value a step writes becomes a ``ColNode``
(a version of that column) whose ``uses`` say where the value comes from. Uses that decide
which rows are written (joins, filters, grouping...) are attached to the step itself and apply
to every column it writes. Facts used by the summaries and the review checks go into
``step.detail``.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .catalog import table_definition_columns
from .engine import Analyzer, OutCol, QueryOut, QueryScope, Source, norm
from .model import ROWS, VALUE, Column, Relation, Use
from .syntax import data_type_text, ident, literal_value, obj_name, parts, typ, walk

AGG_PREFIX = "aggregate"


def status_of(uses: List[Use]) -> str:
    direct = [u for u in uses if u.direct]
    if any(u.status == "unresolved" for u in direct) and not any(u.status == "resolved" for u in direct):
        return "unresolved"
    if any(u.status != "resolved" for u in uses):
        return "partial"
    return "resolved"


class StatementAnalyzer(Analyzer):

    # ------------------------------------------------------------------ writing
    def write(self, rel: Relation, col: str, op: str, uses: List[Use], expr: str = "", span=None,
              keeps: bool = False, kills: bool = False, transforms=(), status: Optional[str] = None,
              note: str = "", ast=None) -> int:
        n = self.ctx.node(rel=rel.key, col=col, step=self.step.id, op=op, expr=expr, span=span, uses=list(uses),
                          keeps_previous=keeps, kills=kills, transforms=list(dict.fromkeys(transforms)),
                          status=status or status_of(uses), note=note, text_id=self.step.text_id, ast=ast)
        self.step.nodes.append(n.id)
        if rel.key not in self.step.writes:
            self.step.writes.append(rel.key)
        return n.id

    def kill_relation(self, rel: Relation) -> None:
        self.step.detail.setdefault("kill_rel", []).append(rel.key)

    def add_row_uses(self, uses: List[Use]) -> None:
        self.step.uses.extend(uses)

    def finish(self) -> None:
        for k in sorted(self.reads):
            if k not in self.step.reads and not k.startswith(("var:", "system:")):
                self.step.reads.append(k)

    # ------------------------------------------------------------------ dispatch
    def run(self) -> None:
        node = self.step.node
        t = typ(node)
        handler = getattr(self, "h_" + (t or "none"), None)
        if handler:
            handler(node)
        else:
            self.h_other(node)
        self.finish()

    def h_none(self, node):
        pass

    def h_EndOfNested(self, node):
        info = self.ctx.nested.get(self.step.parent, {})
        for outer_key, inner_key, label in info.get("outputs", []):
            outer = self.ctx.relations.get(outer_key)
            if outer is None:
                continue
            u = Use(rel=inner_key, col=VALUE, role="value", direct=True, text=label)
            self.write(outer, VALUE, "exec-output", [u], expr=label, kills=True,
                       note=f"output parameter of {info.get('callee', 'the call')}")
        if info.get("insert_into"):
            # INSERT ... EXEC: every result set the call returns is inserted, column by position
            target = self.ctx.relations.get(info["insert_into"])
            nested_ids = {s.id for s in self.ctx.steps if s.parent == self.step.parent}
            names = info.get("insert_columns") or []
            produced = [r for r in self.ctx.relations.values()
                        if r.kind == "result" and any(n.step in nested_ids for n in self.ctx.nodes
                                                      if n.rel == r.key and n.op == "result")]
            if target is not None:
                row_uses = []
                for res in produced:
                    cols = [c for c in res.columns]
                    for i, c in enumerate(cols):
                        name = names[i] if i < len(names) else f"column {i + 1}"
                        if not target.find(name) and not target.complete:
                            target.learn(name)
                        u = Use(rel=res.key, col=c.name, role="value", direct=True, text=f"{res.name}.{c.name}")
                        self.write(target, name, "insert", [u], f"{res.name}: {c.name}", None,
                                   transforms=["INSERT ... EXEC"], note=f"from {info.get('callee', 'the call')}")
                    row_uses.append(Use(rel=res.key, col=ROWS, role="rows", direct=False, text=res.name))
                    res.note = f"inserted into {target.name}"
                self.write(target, ROWS, "insert", row_uses, note=f"rows returned by {info.get('callee', 'the call')}")
                self.step.uses.extend(row_uses)
        # temp tables created inside the call or dynamic batch are dropped when it ends
        nested_ids = {s.id for s in self.ctx.steps if s.parent == self.step.parent}
        for rel in self.ctx.relations.values():
            if rel.kind == "temp" and any(c in nested_ids for c in rel.created) and info.get("drops_temps", True):
                self.kill_relation(rel)
                rel.dropped.append(self.step.id)
        self.step.detail["callee"] = info.get("callee", "")

    # ------------------------------------------------------------------ SELECT
    def statement_query(self, node, q) -> Tuple[QueryOut, QueryScope]:
        ctes = self.with_ctes(node.get("WithCtesAndXmlNamespaces"), None)
        scope = QueryScope(ctes=ctes)
        out = self.query(q, scope)
        if ctes:
            self.step.detail["ctes"] = [loc.key for loc in ctes.values()]
        return out, scope

    def h_SelectStatement(self, node):
        out, scope = self.statement_query(node, node.get("QueryExpression"))
        d = self.step.detail
        d["sources"] = [s.label for s in out.sources]
        d["source_keys"] = [s.key for s in out.sources]
        d["top"], d["distinct"] = out.top, out.distinct
        self._query_facts(node.get("QueryExpression"))
        into = node.get("Into")
        assigns = [c for c in out.cols if c.assign_var]
        if into:
            on = obj_name(into)
            rel = self.ctx.table_relation(on, self.step)
            self.kill_relation(rel)
            rel.created.append(self.step.id)
            cols, star_from = [], []
            for i, c in enumerate(out.cols):
                if c.star is not None:
                    star_from.append(c.star.key)
                    self.write(rel, "*", "select-into", c.uses, c.expr, c.span, status="partial",
                               note=f"all columns of {c.star.label}, not expanded")
                    continue
                name = c.name or f"column{i + 1}"
                ty = self._infer_type(c)
                cols.append(Column(name, ty))
                self.write(rel, name, "select-into", c.uses, c.expr, c.span, kills=True, transforms=c.transforms,
                           ast=(c.node or {}).get("Expression") if c.node else None)
            rel.columns = cols + [c for c in rel.columns if c.name.lower() not in {x.name.lower() for x in cols}
                                  and not rel.complete]
            rel.complete = not star_from
            rel.star_from = star_from
            self.write(rel, ROWS, "select-into", out.row_uses, kills=True)
            self.add_row_uses(out.row_uses)
            d["target"] = rel.key
            d["columns"] = [c.name for c in cols] + (["*"] if star_from else [])
            return
        if assigns:
            keeps = out.has_from and not out.aggregate_only
            for c in assigns:
                rel = self.ctx.relations.get(self.ctx.var_key(c.assign_var, self.ns)) or self.ctx.variable(c.assign_var, self.ns)
                uses = list(c.uses)
                if c.assign_kind != "Equals":
                    uses.append(Use(rel=rel.key, col=VALUE, role="value", direct=True, text=c.assign_var))
                self.write(rel, VALUE, "select-assign", uses, c.expr, c.span, keeps=keeps, kills=not keeps,
                           transforms=c.transforms, ast=(c.node or {}).get("Expression"),
                           note="keeps its earlier value when the query returns no rows; the last row read wins"
                           if keeps else "")
            self.add_row_uses(out.row_uses)
            d["assigns"] = [c.assign_var for c in assigns]
            return
        # a result set returned to the caller
        self.ctx.results += 1
        n = self.ctx.results
        key = f"result:{self.ns}{n}"
        rel = self.ctx.relation(key, "result", f"Result set {n}" + (f" ({self.ns.rstrip('/')})" if self.ns else ""))
        seen: Dict[str, int] = {}
        for i, c in enumerate(out.cols):
            if c.star is not None:
                self.write(rel, "*", "result", c.uses, c.expr, c.span, kills=True, status="partial",
                           note=f"all columns of {c.star.label}, not expanded")
                continue
            name = c.name or f"(column {i + 1})"
            if norm(name) in seen:
                seen[norm(name)] += 1
                name = f"{name} ({seen[norm(name)]})"
            else:
                seen[norm(name)] = 1
            rel.learn(name)
            self.write(rel, name, "result", c.uses, c.expr, c.span, kills=True, transforms=c.transforms)
        rel.complete = True
        self.write(rel, ROWS, "result", out.row_uses, kills=True)
        self.add_row_uses(out.row_uses)
        d["target"] = key
        d["columns"] = [c.name for c in rel.columns]

    def _infer_type(self, c: OutCol) -> str:
        direct = [u for u in c.uses if u.direct]
        if len(direct) == 1 and not [t for t in c.transforms if t not in ("constant",)]:
            rel = self.ctx.relations.get(direct[0].rel)
            if rel:
                col = rel.find(direct[0].col)
                if col and col.type:
                    return col.type
        for t in c.transforms:
            if t.startswith(("CAST to ", "CONVERT to ", "TRY_CAST to ", "TRY_CONVERT to ")):
                return t.split(" to ", 1)[1]
        return ""

    def _query_facts(self, q) -> None:
        """Facts for summaries: the first WHERE clause text and the join list."""
        d = self.step.detail
        qs = q
        while typ(qs) in ("BinaryQueryExpression", "QueryParenthesisExpression"):
            qs = qs.get("FirstQueryExpression") or qs.get("QueryExpression")
        if typ(qs) == "QuerySpecification":
            wh = qs.get("WhereClause")
            if wh and wh.get("SearchCondition"):
                d["where"] = self.sq(wh.get("SearchCondition"), 140)
            gb = qs.get("GroupByClause")
            if gb:
                d["group_by"] = self.sq(gb, 120)

    # ------------------------------------------------------------------ INSERT
    def target_relation(self, target) -> Optional[Relation]:
        t = typ(target)
        if t == "NamedTableReference":
            on = obj_name(target.get("SchemaObject"))
            return self.ctx.table_relation(on, self.step)
        if t == "VariableTableReference":
            name = (target.get("Variable") or {}).get("Name") or ""
            return self.ctx.relations.get(f"tvar:{self.ns}{norm(name)}") or self.ctx.table_variable(name, self.ns)
        return None

    def h_InsertStatement(self, node):
        spec = node.get("InsertSpecification") or {}
        rel = self.target_relation(spec.get("Target"))
        d = self.step.detail
        if rel is None:
            d["target"] = None
            return
        d["target"] = rel.key
        listed = [parts(c.get("MultiPartIdentifier"))[-1] for c in spec.get("Columns", []) or []
                  if parts(c.get("MultiPartIdentifier"))]
        d["column_list"] = bool(listed)
        if listed:
            for c in listed:
                if not rel.find(c) and not rel.complete:
                    rel.learn(c)
        src = spec.get("InsertSource") or {}
        st = typ(src)
        ctes = self.with_ctes(node.get("WithCtesAndXmlNamespaces"), None)
        values: List[OutCol] = []
        row_uses: List[Use] = []
        if st == "SelectInsertSource":
            out = self.query(src.get("Select"), QueryScope(ctes=ctes))
            values, row_uses = out.cols, out.row_uses
            d["sources"] = [s.label for s in out.sources]
            d["source_keys"] = [s.key for s in out.sources]
            d["select_star"] = any(c.star is not None for c in out.cols) or any(
                typ(e) == "SelectStarExpression" for e in walk(src.get("Select")) if typ(e) == "SelectStarExpression")
            self._query_facts(src.get("Select"))
            d["top"] = out.top
        elif st == "ValuesInsertSource":
            rows = src.get("RowValues", []) or []
            width = max((len(r.get("ColumnValues", []) or []) for r in rows), default=0)
            for i in range(width):
                uses, tf = [], []
                for r in rows:
                    vals = r.get("ColumnValues", []) or []
                    if i < len(vals):
                        self.expr(vals[i], QueryScope(ctes=ctes), "value", True, uses, tf)
                first = (rows[0].get("ColumnValues") or [None])[i] if rows and i < len(rows[0].get("ColumnValues") or []) else None
                values.append(OutCol(name=None, uses=uses, expr=self.sq(first, 200) if first else "",
                                     span=self.span(first) if first else None, transforms=tf or ["constant"]))
            d["values_rows"] = len(rows)
            if src.get("IsDefaultValues"):
                d["default_values"] = True
        elif st == "ExecuteInsertSource":
            ex = (src.get("Execute") or {})
            callee = self._exec_spec(ex, as_insert=True)
            d["insert_exec"] = callee
            names = listed or ([c.name for c in rel.columns if not c.identity and not c.computed] if rel.complete else [])
            if self.ctx.nested_expanded(self.step.id):
                # the rows arrive when the expanded call ends: see h_EndOfNested
                self.ctx.nested.setdefault(self.step.id, {}).update({"insert_into": rel.key, "insert_columns": names})
                d["columns"] = names
                return
            width = len(listed) if listed else len(rel.columns)
            for i in range(max(width, 1)):
                values.append(OutCol(name=None, uses=[Use(rel=f"proc-result:{norm(callee)}", col=f"column {i + 1}",
                                                          role="value", direct=True, status="partial",
                                                          text=f"{callee} result column {i + 1}")],
                                     expr=f"EXEC {callee} (result column {i + 1})", transforms=["procedure result"]))
        # map values to target columns
        if listed:
            names = listed
        else:
            names = [c.name for c in rel.columns if not c.identity and not c.computed] if rel.complete else []
        star = [v for v in values if v.star is not None]
        if star and not names:
            self.write(rel, "*", "insert", star[0].uses, star[0].expr, star[0].span, status="partial",
                       note="all columns, not expanded")
        else:
            for i, v in enumerate(values):
                if v.star is not None:
                    self.write(rel, "*", "insert", v.uses, v.expr, v.span, status="partial",
                               note=f"all columns of {v.star.label}, not expanded")
                    continue
                if i < len(names):
                    name, status = names[i], None
                else:
                    name, status = f"column {i + 1}", "partial"
                    rel.note = rel.note or "insert without a column list into a table whose columns are unknown"
                    if not rel.complete:
                        rel.learn(name)
                self.write(rel, name, "insert", v.uses, v.expr, v.span, transforms=v.transforms, status=status,
                           ast=(v.node or {}).get("Expression") if v.node else None)
            if rel.complete and listed:
                for c in rel.columns:
                    if c.name.lower() not in {x.lower() for x in listed} and not c.computed:
                        how = "IDENTITY" if c.identity else (f"default {c.default}" if c.default else "NULL")
                        self.write(rel, c.name, "default", [], how, None,
                                   transforms=["identity" if c.identity else "default"],
                                   note="not in the column list")
        self.write(rel, ROWS, "insert", row_uses)
        self.add_row_uses(row_uses)
        d["columns"] = names[:len(values)] if names else []
        self.output_clauses(spec, rel, QueryScope(ctes=ctes), None)

    # ------------------------------------------------------------------ UPDATE / DELETE
    def dml_scope(self, spec, ctes) -> Tuple[QueryScope, QueryOut, Optional[Source]]:
        scope = QueryScope(ctes=ctes)
        out = QueryOut()
        fc = spec.get("FromClause")
        if fc:
            for tr in fc.get("TableReferences", []) or []:
                self.table_ref(tr, scope, out)
        target = spec.get("Target")
        tsrc = None
        if typ(target) == "NamedTableReference":
            on = obj_name(target.get("SchemaObject"))
            if not on.schema and not on.database:
                for s in scope.sources:
                    if s.alias and norm(s.alias) == norm(on.name):
                        tsrc = s
                        break
            if tsrc is None:
                for s in scope.sources:
                    if s.rel is not None and norm(s.rel.name.split(".")[-1]) == norm(on.name) and \
                            (not on.schema or f"{norm(on.schema)}.{norm(on.name)}" in s.names):
                        tsrc = s
                        break
            if tsrc is None:
                tsrc = self.named_source(target, scope)
                scope.sources.insert(0, tsrc)
        elif typ(target) == "VariableTableReference":
            name = (target.get("Variable") or {}).get("Name") or ""
            for s in scope.sources:
                if s.rel is not None and s.rel.key == f"tvar:{self.ns}{norm(name)}":
                    tsrc = s
            if tsrc is None:
                rel = self.target_relation(target)
                tsrc = Source(alias=ident(target.get("Alias")), names={norm(name)}, rel=rel, span=self.span(target))
                scope.sources.insert(0, tsrc)
        return scope, out, tsrc

    def h_UpdateStatement(self, node):
        spec = node.get("UpdateSpecification") or {}
        ctes = self.with_ctes(node.get("WithCtesAndXmlNamespaces"), None)
        scope, out, tsrc = self.dml_scope(spec, ctes)
        d = self.step.detail
        if tsrc is None or (tsrc.rel is None and tsrc.local is None):
            return
        if tsrc.local is not None:
            d["target"] = tsrc.local.key
            d["note"] = "updates through a CTE or derived table"
            rel = None
        else:
            rel = tsrc.rel
            d["target"] = rel.key
        row_uses: List[Use] = list(out.row_uses)
        wh = spec.get("WhereClause")
        if wh and wh.get("SearchCondition"):
            self.bexpr(wh.get("SearchCondition"), scope, "filter", row_uses)
            d["where"] = self.sq(wh.get("SearchCondition"), 140)
        if wh and wh.get("Cursor"):
            d["where"] = "CURRENT OF cursor"
        top = spec.get("TopRowFilter")
        if top:
            self.expr(top.get("Expression"), scope, "top", False, row_uses)
            d["top"] = True
        others = [s for s in scope.sources if s is not tsrc]
        for s in others:
            if s.local is not None:
                if s.local.rows is not None:
                    row_uses.append(Use(rel=s.local.key, col=ROWS, role="rows", direct=False, local=s.local.rows,
                                        span=s.span, text=s.label))
            else:
                row_uses.append(Use(rel=s.rel.key, col=ROWS, role="rows", direct=False, span=s.span, text=s.label))
        d["joins"] = [f"{s.join or 'FROM'} {s.label}" for s in others]
        d["sources"] = [s.label for s in others]
        d["source_keys"] = [s.key for s in others]
        partial = bool(wh) or bool(top) or any(s.join in ("INNER JOIN", "", "CROSS APPLY") for s in others)
        cols = []
        if rel is not None:
            for sc in spec.get("SetClauses", []) or []:
                cols += self._set_clause(sc, rel, scope, partial, "update")
        self.add_row_uses(row_uses)
        d["columns"] = cols
        d["partial"] = partial
        d["update_from"] = bool(others)
        if rel is not None:
            self.output_clauses(spec, rel, scope, tsrc)

    def _set_clause(self, sc, rel: Relation, scope, partial: bool, op: str, extra: Optional[List[Use]] = None) -> List[str]:
        written = []
        extra = extra or []
        if typ(sc) == "AssignmentSetClause":
            tf: List[str] = []
            uses = self.expr(sc.get("NewValue"), scope, "value", True, None, tf)
            kind = sc.get("AssignmentKind", "Equals")
            col_ref = sc.get("Column")
            var = (sc.get("Variable") or {}).get("Name")
            if col_ref is not None:
                cname = (parts(col_ref.get("MultiPartIdentifier")) or [""])[-1]
                c = rel.find(cname)
                if c is None:
                    if rel.complete:
                        self.ctx.unresolved.append({"step": self.step.id, "text": cname, "span": self.span(col_ref),
                                                    "note": f"{cname} is not a column of {rel.name}"})
                    c = rel.learn(cname)
                cname = c.name
                cu = list(uses) + list(extra)
                if kind != "Equals":
                    cu.append(Use(rel=rel.key, col=cname, role="value", direct=True, span=self.span(col_ref), text=cname))
                    tf.append({"AddEquals": "+=", "SubtractEquals": "-=", "MultiplyEquals": "*=",
                               "DivideEquals": "/=", "ConcatEquals": "+="}.get(kind, kind))
                expr = self.sq(sc.get("NewValue"), 400)
                self.write(rel, cname, op, cu, expr, self.span(sc), keeps=partial, kills=not partial,
                           transforms=tf or ["constant"] if not uses else tf, ast=sc.get("NewValue"))
                written.append(cname)
                if var:
                    vrel = self.ctx.relations.get(self.ctx.var_key(var, self.ns)) or self.ctx.variable(var, self.ns)
                    self.write(vrel, VALUE, "set", [Use(rel=rel.key, col=cname, role="value", direct=True,
                                                        same_step=True, text=cname)], expr, self.span(sc),
                               keeps=True, note="assigned during the UPDATE (last row updated wins)")
            elif var:
                vrel = self.ctx.relations.get(self.ctx.var_key(var, self.ns)) or self.ctx.variable(var, self.ns)
                self.write(vrel, VALUE, "set", uses, self.sq(sc.get("NewValue"), 400), self.span(sc), keeps=True,
                           transforms=tf, note="assigned during the UPDATE (last row updated wins)")
        elif typ(sc) == "FunctionCallSetClause":
            fc = sc.get("MutatorFunction") or {}
            target = fc.get("CallTarget") or {}
            cname = (parts(target.get("MultiPartIdentifier")) or [""])[-1] if target else ""
            uses = []
            for p in fc.get("Parameters", []) or []:
                self.expr(p, scope, "value", True, uses)
            if cname:
                uses.append(Use(rel=rel.key, col=cname, role="value", direct=True, text=cname))
                rel.learn(cname)
                self.write(rel, cname, op, uses, self.sq(fc, 300), self.span(sc), keeps=True,
                           transforms=[f"method {(ident(fc.get('FunctionName')) or '').lower()}()"])
                written.append(cname)
        return written

    def h_DeleteStatement(self, node):
        spec = node.get("DeleteSpecification") or {}
        ctes = self.with_ctes(node.get("WithCtesAndXmlNamespaces"), None)
        scope, out, tsrc = self.dml_scope(spec, ctes)
        d = self.step.detail
        if tsrc is None or tsrc.rel is None:
            return
        rel = tsrc.rel
        d["target"] = rel.key
        row_uses: List[Use] = list(out.row_uses)
        wh = spec.get("WhereClause")
        if wh and wh.get("SearchCondition"):
            self.bexpr(wh.get("SearchCondition"), scope, "filter", row_uses)
            d["where"] = self.sq(wh.get("SearchCondition"), 140)
        top = spec.get("TopRowFilter")
        if top:
            self.expr(top.get("Expression"), scope, "top", False, row_uses)
        others = [s for s in scope.sources if s is not tsrc]
        for s in others:
            if s.rel is not None:
                row_uses.append(Use(rel=s.rel.key, col=ROWS, role="rows", direct=False, span=s.span, text=s.label))
            elif s.local is not None and s.local.rows is not None:
                row_uses.append(Use(rel=s.local.key, col=ROWS, role="rows", direct=False, local=s.local.rows,
                                    span=s.span, text=s.label))
        d["joins"] = [f"{s.join or 'FROM'} {s.label}" for s in others]
        everything = not wh and not top and not others
        d["all_rows"] = everything
        if everything:
            self.kill_relation(rel)
            self.write(rel, ROWS, "delete", [], kills=True, note="every row deleted")
        else:
            self.write(rel, ROWS, "delete", row_uses, keeps=True, note="rows matching the condition are removed")
        self.add_row_uses(row_uses)
        self.output_clauses(spec, rel, scope, tsrc, deleting=True)

    # ------------------------------------------------------------------ MERGE
    def h_MergeStatement(self, node):
        spec = node.get("MergeSpecification") or {}
        ctes = self.with_ctes(node.get("WithCtesAndXmlNamespaces"), None)
        d = self.step.detail
        scope = QueryScope(ctes=ctes)
        out = QueryOut()
        target = spec.get("Target")
        rel = self.target_relation(target)
        if rel is None:
            return
        alias = ident(spec.get("TableAlias")) or ident((target or {}).get("Alias"))
        on = obj_name(target.get("SchemaObject")) if typ(target) == "NamedTableReference" else None
        names = {norm(alias)} if alias else set()
        if on:
            names |= {norm(on.name), f"{norm(on.schema)}.{norm(on.name)}"}
        tsrc = Source(alias=alias, names=names, rel=rel, span=self.span(target))
        scope.sources.append(tsrc)
        self.table_ref(spec.get("TableReference"), scope, out)
        d["target"] = rel.key
        sources = [s for s in scope.sources if s is not tsrc]
        d["sources"] = [s.label for s in sources]
        d["source_keys"] = [s.key for s in sources]
        row_uses: List[Use] = list(out.row_uses)
        if spec.get("SearchCondition"):
            self.bexpr(spec.get("SearchCondition"), scope, "join", row_uses)
            d["on"] = self.sq(spec.get("SearchCondition"), 140)
        for s in sources:
            if s.rel is not None:
                row_uses.append(Use(rel=s.rel.key, col=ROWS, role="rows", direct=False, span=s.span, text=s.label))
            elif s.local is not None and s.local.rows is not None:
                row_uses.append(Use(rel=s.local.key, col=ROWS, role="rows", direct=False, local=s.local.rows,
                                    span=s.span, text=s.label))
        self.add_row_uses(row_uses)
        actions = []
        for ac in spec.get("ActionClauses", []) or []:
            cond = ac.get("Condition", "")
            when = {"Matched": "WHEN MATCHED", "NotMatched": "WHEN NOT MATCHED",
                    "NotMatchedByTarget": "WHEN NOT MATCHED BY TARGET",
                    "NotMatchedBySource": "WHEN NOT MATCHED BY SOURCE"}.get(cond, cond)
            extra: List[Use] = []
            if ac.get("SearchCondition"):
                self.bexpr(ac.get("SearchCondition"), scope, "merge", extra)
                when += " AND " + self.sq(ac.get("SearchCondition"), 100)
            act = ac.get("Action") or {}
            at = typ(act)
            if at == "UpdateMergeAction":
                cols = []
                for sc in act.get("SetClauses", []) or []:
                    cols += self._set_clause(sc, rel, scope, True, "merge-update", extra)
                actions.append({"when": when, "action": "UPDATE", "columns": cols})
            elif at == "InsertMergeAction":
                listed = [parts(c.get("MultiPartIdentifier"))[-1] for c in act.get("Columns", []) or []]
                vals = ((act.get("Source") or {}).get("RowValues") or [{}])[0].get("ColumnValues", []) or []
                names = listed or ([c.name for c in rel.columns if not c.identity and not c.computed] if rel.complete else [])
                for i, v in enumerate(vals):
                    tf: List[str] = []
                    uses = self.expr(v, scope, "value", True, None, tf) + list(extra)
                    name = names[i] if i < len(names) else f"column {i + 1}"
                    if not rel.find(name) and not rel.complete:
                        rel.learn(name)
                    self.write(rel, name, "merge-insert", uses, self.sq(v, 300), self.span(v), transforms=tf,
                               status=None if i < len(names) else "partial")
                self.write(rel, ROWS, "merge-insert", list(extra))
                actions.append({"when": when, "action": "INSERT", "columns": names[:len(vals)]})
            elif at == "DeleteMergeAction":
                self.write(rel, ROWS, "merge-delete", list(extra), keeps=True, note=when)
                actions.append({"when": when, "action": "DELETE", "columns": []})
        d["actions"] = actions
        self.output_clauses(spec, rel, scope, tsrc)

    # ------------------------------------------------------------------ OUTPUT clauses
    def output_clauses(self, spec, rel: Relation, scope: QueryScope, tsrc: Optional[Source], deleting=False) -> None:
        for clause_name in ("OutputIntoClause", "OutputClause"):
            clause = spec.get(clause_name)
            if not clause:
                continue
            oscope = QueryScope(parent=scope, ctes=scope.ctes)
            ins = Source(alias="inserted", names={"inserted"}, rel=rel)
            dele = Source(alias="deleted", names={"deleted"}, rel=rel)
            oscope.special = {"inserted": ins, "deleted": dele}
            cols: List[OutCol] = []
            for el in clause.get("SelectColumns", []) or []:
                if typ(el) == "SelectScalarExpression":
                    tf: List[str] = []
                    uses = self.expr(el.get("Expression"), oscope, "value", True, None, tf)
                    for u in uses:
                        if u.text.lower().startswith("inserted."):
                            u.same_step = True
                    cn = el.get("ColumnName")
                    name = (ident(cn.get("Identifier")) if cn and cn.get("Identifier") else (cn or {}).get("Value")) or \
                        ((parts(el["Expression"].get("MultiPartIdentifier")) or [None])[-1]
                         if typ(el.get("Expression")) == "ColumnReferenceExpression" else None)
                    cols.append(OutCol(name=name, uses=uses, expr=self.sq(el.get("Expression"), 200),
                                       span=self.span(el), transforms=tf))
                elif typ(el) == "SelectStarExpression":
                    q = norm((parts(el.get("Qualifier")) or ["inserted"])[0])
                    for c in rel.columns:
                        cols.append(OutCol(name=c.name, uses=[Use(rel=rel.key, col=c.name, role="value", direct=True,
                                                                  same_step=(q == "inserted"), text=f"{q}.{c.name}")],
                                           expr=f"{q}.{c.name}", span=self.span(el)))
            if clause_name == "OutputIntoClause":
                into = self.target_relation(clause.get("IntoTable"))
                if into is None:
                    continue
                listed = [parts(c.get("MultiPartIdentifier"))[-1] for c in clause.get("IntoTableColumns", []) or []]
                names = listed or [c.name for c in into.columns]
                for i, c in enumerate(cols):
                    name = names[i] if i < len(names) else (c.name or f"column {i + 1}")
                    if not into.find(name) and not into.complete:
                        into.learn(name)
                    self.write(into, name, "output-into", c.uses, c.expr, c.span, transforms=c.transforms + ["OUTPUT clause"])
                self.write(into, ROWS, "output-into", [])
                self.step.detail["output_into"] = into.key
            else:
                self.ctx.results += 1
                key = f"result:{self.ns}{self.ctx.results}"
                res = self.ctx.relation(key, "result", f"Result set {self.ctx.results} (OUTPUT clause)")
                for i, c in enumerate(cols):
                    name = c.name or f"(column {i + 1})"
                    res.learn(name)
                    self.write(res, name, "result", c.uses, c.expr, c.span, kills=True,
                               transforms=c.transforms + ["OUTPUT clause"])
                res.complete = True
                self.write(res, ROWS, "result", [], kills=True)
                self.step.detail["output_result"] = key

    # ------------------------------------------------------------------ DDL on tables
    def h_TruncateTableStatement(self, node):
        rel = self.ctx.table_relation(obj_name(node.get("TableName")), self.step)
        self.kill_relation(rel)
        self.write(rel, ROWS, "truncate", [], kills=True, note="every row removed")
        self.step.detail["target"] = rel.key

    def h_CreateTableStatement(self, node):
        on = obj_name(node.get("SchemaObjectName"))
        rel = self.ctx.table_relation(on, self.step)
        cols, keys = table_definition_columns(node.get("Definition"), self.text)
        if node.get("SelectStatement"):
            # CREATE TABLE AS SELECT (Synapse / Fabric)
            self.step.node = node
            sel = node["SelectStatement"]
            out, _ = self.statement_query(sel, sel.get("QueryExpression"))
            self.kill_relation(rel)
            rel.created.append(self.step.id)
            names = [ident(c) for c in node.get("CtasColumns", []) or []]
            for i, c in enumerate(out.cols):
                name = names[i] if i < len(names) and names[i] else (c.name or f"column{i + 1}")
                rel.learn(name)
                self.write(rel, name, "select-into", c.uses, c.expr, c.span, kills=True, transforms=c.transforms)
            rel.complete = True
            self.write(rel, ROWS, "select-into", out.row_uses, kills=True)
            self.add_row_uses(out.row_uses)
            self.step.detail["target"] = rel.key
            return
        rel.columns = cols
        rel.complete = bool(cols)
        rel.created.append(self.step.id)
        if keys:
            self.step.detail["keys"] = keys
            self.ctx.keys.setdefault(rel.key, []).extend(keys)
        self.kill_relation(rel)
        self.write(rel, ROWS, "create", [], kills=True, note="created empty")
        self.step.detail["target"] = rel.key
        self.step.detail["columns"] = [c.name for c in cols]

    def h_DropTableStatement(self, node):
        targets = []
        for o in node.get("Objects", []) or []:
            rel = self.ctx.table_relation(obj_name(o), self.step)
            self.kill_relation(rel)
            rel.dropped.append(self.step.id)
            targets.append(rel.key)
            if rel.key not in self.step.writes:
                self.step.writes.append(rel.key)
        self.step.detail["targets"] = targets
        self.step.detail["if_exists"] = bool(node.get("IsIfExists"))

    def h_AlterTableAddTableElementStatement(self, node):
        rel = self.ctx.table_relation(obj_name(node.get("SchemaObjectName")), self.step)
        cols, keys = table_definition_columns(node.get("Definition"), self.text)
        for c in cols:
            if not rel.find(c.name):
                rel.columns.append(c)
            self.write(rel, c.name, "default", [], c.default or "NULL", None, transforms=["new column"],
                       note="column added: existing rows get its default")
        if keys:
            self.ctx.keys.setdefault(rel.key, []).extend(keys)
        self.step.detail["target"] = rel.key
        self.step.detail["columns"] = [c.name for c in cols]

    def h_CreateIndexStatement(self, node):
        on = obj_name(node.get("OnName"))
        rel = self.ctx.table_relation(on, self.step) if on.name else None
        if rel is not None:
            self.step.detail["target"] = rel.key
            if node.get("Unique"):
                kcols = [(parts((c.get("Column") or {}).get("MultiPartIdentifier")) or [""])[-1]
                         for c in node.get("Columns", []) or []]
                self.ctx.keys.setdefault(rel.key, []).append(kcols)

    # ------------------------------------------------------------------ variables
    def h_DeclareVariableStatement(self, node):
        names = []
        for el in node.get("Declarations", []) or []:
            name = ident(el.get("VariableName")) or ""
            dtype = data_type_text(el.get("DataType"))
            if typ(el.get("DataType")) == "SqlDataTypeReference" and el["DataType"].get("SqlDataTypeOption") == "Cursor":
                dtype = "cursor"
            rel = self.ctx.variable(name, self.ns, data_type=dtype)
            if rel.note == "not declared in this code":
                rel.note = ""
            if el.get("Value") is not None:
                tf: List[str] = []
                uses = self.expr(el.get("Value"), None, "value", True, None, tf)
                self.write(rel, VALUE, "declare", uses, self.sq(el.get("Value"), 300), self.span(el), kills=True,
                           transforms=tf, ast=el.get("Value"))
            else:
                self.write(rel, VALUE, "declare", [], "NULL", self.span(el), kills=False,
                           note="declared without a value (NULL)")
            names.append(name)
        self.step.detail["variables"] = names

    def h_DeclareTableVariableStatement(self, node):
        body = node.get("Body") or {}
        name = ident(body.get("VariableName")) or ""
        rel = self.ctx.table_variable(name, self.ns)
        cols, keys = table_definition_columns(body.get("Definition"), self.text)
        rel.columns, rel.complete = cols, True
        rel.created.append(self.step.id)
        if keys:
            self.ctx.keys.setdefault(rel.key, []).extend(keys)
        self.kill_relation(rel)
        self.write(rel, ROWS, "create", [], kills=True, note="declared empty")
        self.step.detail["target"] = rel.key
        self.step.detail["columns"] = [c.name for c in cols]

    def h_SetVariableStatement(self, node):
        var = (node.get("Variable") or {}).get("Name") or ""
        rel = self.ctx.relations.get(self.ctx.var_key(var, self.ns)) or self.ctx.variable(var, self.ns)
        if node.get("CursorDefinition"):
            self.ctx.cursor_defs[f"{self.ns}{norm(var)}"] = (node["CursorDefinition"].get("Select"), self.step.id)
            self.write(rel, VALUE, "set", [], "CURSOR", self.span(node), kills=True, note="cursor variable")
            self.step.detail["variable"] = var
            return
        tf: List[str] = []
        uses = self.expr(node.get("Expression"), None, "value", True, None, tf)
        kind = node.get("AssignmentKind", "Equals")
        if kind != "Equals":
            uses.append(Use(rel=rel.key, col=VALUE, role="value", direct=True, text=var))
            tf.append({"AddEquals": "+=", "SubtractEquals": "-=", "MultiplyEquals": "*=", "DivideEquals": "/=",
                       "ConcatEquals": "+="}.get(kind, kind))
        self.write(rel, VALUE, "set", uses, self.sq(node.get("Expression"), 400), self.span(node), kills=True,
                   transforms=tf, ast=node.get("Expression") if kind == "Equals" else self._compound_ast(node, var))
        self.step.detail["variable"] = var
        self.step.detail["expression"] = self.sq(node.get("Expression"), 140)

    @staticmethod
    def _compound_ast(node, var):
        """SET @v += expr behaves like SET @v = @v + expr (used to rebuild dynamic SQL)."""
        if node.get("AssignmentKind") in ("AddEquals", "ConcatEquals"):
            return {"$": "BinaryExpression", "@": node.get("@"), "BinaryExpressionType": "Add",
                    "FirstExpression": {"$": "VariableReference", "@": node.get("@"), "Name": var},
                    "SecondExpression": node.get("Expression")}
        return None

    # ------------------------------------------------------------------ cursors
    def h_DeclareCursorStatement(self, node):
        name = ident(node.get("Name")) or ""
        key = f"{self.ns}{norm(name)}"
        sel = (node.get("CursorDefinition") or {}).get("Select")
        self.ctx.cursor_defs[key] = (sel, self.step.id)
        self.step.detail["cursor"] = name
        if key not in self.ctx.opened_cursors:
            self._open_cursor(name, sel)

    def h_OpenCursorStatement(self, node):
        name = ((node.get("Cursor") or {}).get("Name") or {})
        cname = ident(name.get("Identifier")) if name.get("Identifier") else name.get("Value") or ""
        key = f"{self.ns}{norm(cname)}"
        sel = self.ctx.cursor_defs.get(key, (None, None))[0]
        self.step.detail["cursor"] = cname
        if sel is not None:
            self._open_cursor(cname, sel)

    def _open_cursor(self, name: str, sel) -> None:
        if not sel:
            return
        out, _ = self.statement_query(sel, sel.get("QueryExpression"))
        key = f"cursor:{self.ns}{norm(name)}"
        rel = self.ctx.relation(key, "cursor", f"cursor {name}")
        self.kill_relation(rel)
        rel.created.append(self.step.id)
        cols = []
        for i, c in enumerate(out.cols):
            cname = c.name or f"column{i + 1}"
            if c.star is not None:
                cname = "*"
            cols.append(Column(cname))
            self.write(rel, cname, "open-cursor", c.uses, c.expr, c.span, kills=True, transforms=c.transforms)
        rel.columns, rel.complete = cols, True
        self.write(rel, ROWS, "open-cursor", out.row_uses, kills=True)
        self.add_row_uses(out.row_uses)
        self.step.detail["sources"] = [s.label for s in out.sources]
        self.step.detail["source_keys"] = [s.key for s in out.sources]
        self._query_facts(sel.get("QueryExpression"))

    def h_FetchCursorStatement(self, node):
        cur = (node.get("Cursor") or {}).get("Name") or {}
        cname = ident(cur.get("Identifier")) if cur.get("Identifier") else cur.get("Value") or ""
        if cname.startswith("@"):
            key = f"cursor:{self.ns}{norm(cname)}"
        else:
            key = f"cursor:{self.ns}{norm(cname)}"
        rel = self.ctx.relations.get(key)
        targets = []
        for i, v in enumerate(node.get("IntoVariables", []) or []):
            var = v.get("Name") or ""
            vrel = self.ctx.relations.get(self.ctx.var_key(var, self.ns)) or self.ctx.variable(var, self.ns)
            if rel is not None and i < len(rel.columns):
                col = rel.columns[i].name
                u = Use(rel=rel.key, col=col, role="value", direct=True, span=self.span(v), text=f"{cname}.{col}")
                self.write(vrel, VALUE, "fetch", [u], f"{cname} column {i + 1} ({col})", self.span(v), kills=True,
                           note="when a row is fetched")
            else:
                u = Use(rel=key, col=f"column {i + 1}", role="value", direct=True, status="unresolved",
                        text=f"{cname} column {i + 1}")
                self.write(vrel, VALUE, "fetch", [u], f"{cname} column {i + 1}", self.span(v), kills=True)
            targets.append(var)
        if rel is not None:
            self.step.uses.append(Use(rel=rel.key, col=ROWS, role="rows", direct=False, text=cname))
            self.reads.add(rel.key)
        self.step.detail["cursor"] = cname
        self.step.detail["variables"] = targets

    # ------------------------------------------------------------------ control flow
    def h_IfStatement(self, node):
        uses = self.bexpr(node.get("Predicate"), None, "condition", [])
        self.step.uses.extend(uses)
        self.step.detail["predicate"] = self.sq(node.get("Predicate"), 200)

    def h_WhileStatement(self, node):
        uses = self.bexpr(node.get("Predicate"), None, "condition", [])
        self.step.uses.extend(uses)
        self.step.detail["predicate"] = self.sq(node.get("Predicate"), 200)

    def h_ReturnStatement(self, node):
        if node.get("Expression") is not None:
            rel = self.ctx.relation(f"return:{self.ns}", "return",
                                    "Return value" + (f" ({self.ns.rstrip('/')})" if self.ns else ""),
                                    columns=[Column(VALUE, "int")], complete=True)
            tf: List[str] = []
            uses = self.expr(node.get("Expression"), None, "value", True, None, tf)
            self.write(rel, VALUE, "return", uses, self.sq(node.get("Expression"), 200), self.span(node), kills=True,
                       transforms=tf)
            self.step.detail["value"] = self.sq(node.get("Expression"), 80)

    def h_PrintStatement(self, node):
        self.step.uses.extend(self.expr(node.get("Expression"), None, "argument", False))

    def h_RaiseErrorStatement(self, node):
        uses = []
        for k in ("FirstParameter", "SecondParameter", "ThirdParameter"):
            self.expr(node.get(k), None, "argument", False, uses)
        self.step.uses.extend(uses)
        sev = literal_value(node.get("SecondParameter"))
        self.step.detail["severity"] = sev
        self.step.detail["message"] = self.sq(node.get("FirstParameter"), 120)

    def h_ThrowStatement(self, node):
        uses = []
        for k in ("ErrorNumber", "Message", "State"):
            self.expr(node.get(k), None, "argument", False, uses)
        self.step.uses.extend(uses)
        self.step.detail["message"] = self.sq(node.get("Message"), 120) if node.get("Message") else "re-throws the error"

    def h_PredicateSetStatement(self, node):
        self.step.detail["options"] = node.get("Options", "")
        self.step.detail["on"] = bool(node.get("IsOn"))

    def h_SetTransactionIsolationLevelStatement(self, node):
        self.step.detail["isolation"] = node.get("Level", "")

    def h_BeginTransactionStatement(self, node):
        self.step.detail["name"] = self.sq(node.get("Name"), 60) if node.get("Name") else ""

    h_CommitTransactionStatement = h_BeginTransactionStatement
    h_RollbackTransactionStatement = h_BeginTransactionStatement
    h_SaveTransactionStatement = h_BeginTransactionStatement

    def h_BulkInsertStatement(self, node):
        rel = self.ctx.table_relation(obj_name(node.get("To")), self.step)
        frm = node.get("From") or {}
        path = frm.get("Value") or ident(frm.get("Identifier")) or "file"
        frel = self.ctx.relation(f"file:{norm(path)}", "file", path)
        frel.endpoint = {"system": "File", "server": None, "port": None, "database": None, "schema": None,
                         "object": None, "path": path, "url": None, "container": None}
        self.reads.add(frel.key)
        u = Use(rel=frel.key, col="*", role="value", direct=True, status="partial", text=path)
        self.write(rel, "*", "insert", [u], f"BULK INSERT from {path}", self.span(node), status="partial",
                   note="loaded from a file; columns are not visible")
        self.write(rel, ROWS, "insert", [Use(rel=frel.key, col=ROWS, role="rows", direct=False, text=path)])
        self.step.detail["target"] = rel.key
        self.step.detail["file"] = path

    h_InsertBulkStatement = h_BulkInsertStatement

    def h_UseStatement(self, node):
        self.step.detail["database"] = ident(node.get("DatabaseName")) or ""

    def h_other(self, node):
        # statements read only for the objects they name (maintenance, security, unknown kinds)
        names = []
        for x in walk(node):
            if typ(x) == "SchemaObjectName":
                on = obj_name(x)
                if on.name and not on.name.startswith("@"):
                    names.append(on.display)
        self.step.detail["objects"] = list(dict.fromkeys(names))[:10]

    # ------------------------------------------------------------------ EXEC
    def h_ExecuteStatement(self, node):
        spec = node.get("ExecuteSpecification") or {}
        self._exec_spec(spec)

    def _exec_spec(self, spec, as_insert: bool = False) -> str:
        d = self.step.detail
        ent = spec.get("ExecutableEntity") or {}
        linked = ident(spec.get("LinkedServer"))
        ret_var = (spec.get("Variable") or {}).get("Name")
        et = typ(ent)
        if et == "ExecutableStringList":
            strings = ent.get("Strings", []) or []
            uses = []
            for s in strings:
                self.expr(s, None, "dynamic", False, uses)
            self.step.uses.extend(uses)
            d["dynamic"] = {"form": "EXEC()", "parts": [self.sq(s, 200) for s in strings]}
            if linked:
                d["dynamic"]["linked_server"] = linked
            self.ctx.calls.append({"step": self.step.id, "kind": "dynamic", "name": "EXEC(…)",
                                   "linkedServer": linked, "supplied": False})
            if self.ctx.nested_expanded(self.step.id):
                self.ctx.nested.setdefault(self.step.id, {}).update({"outputs": [], "callee": "dynamic SQL",
                                                                     "drops_temps": True})
            return "dynamic SQL"
        pref = ent.get("ProcedureReference") or {}
        pr = pref.get("ProcedureReference") or {}
        on = obj_name(pr.get("Name")) if pr.get("Name") else None
        if on is None:
            pv = (pref.get("ProcedureVariable") or {}).get("Name") or "@procedure"
            self.step.uses.append(self.var_use(pv, "dynamic", False))
            self.ctx.calls.append({"step": self.step.id, "kind": "procedure", "name": pv, "dynamicName": True,
                                   "supplied": False})
            d["callee"] = pv
            return pv
        name = on.display
        params = ent.get("Parameters", []) or []
        if on.name.lower() == "sp_executesql":
            stmt = params[0].get("ParameterValue") if params else None
            uses = self.expr(stmt, None, "dynamic", False, []) if stmt is not None else []
            self.step.uses.extend(uses)
            pdef = params[1].get("ParameterValue") if len(params) > 1 else None
            args = []
            for p in params[2:]:
                pname = (p.get("Variable") or {}).get("Name")
                val = p.get("ParameterValue")
                vu = self.expr(val, None, "argument", True, []) if val is not None else []
                args.append({"name": pname, "value": self.sq(val, 80), "output": bool(p.get("IsOutput")),
                             "uses": vu, "node": val})
                if not p.get("IsOutput"):
                    self.step.uses.extend(self._as(u, "argument", False) for u in vu)
            d["dynamic"] = {"form": "sp_executesql", "parts": [self.sq(stmt, 200)] if stmt is not None else [],
                            "params": self.sq(pdef, 300) if pdef is not None else "", "args":
                                [{k: v for k, v in a.items() if k not in ("uses", "node")} for a in args]}
            d["dynamic_args"] = args
            self.ctx.calls.append({"step": self.step.id, "kind": "dynamic", "name": "sp_executesql",
                                   "linkedServer": linked, "supplied": False})
            info = self.ctx.nested_expanded(self.step.id)
            if info:
                # the dynamic batch's parameters are set from the arguments, OUTPUT ones copied back
                declared = info.get("params", [])
                outputs = []
                for i, a in enumerate(args):
                    pname = a["name"] or (declared[i]["name"] if i < len(declared) else None)
                    if not pname:
                        continue
                    inner = self.ctx.variable(pname, info["namespace"], kind="parameter")
                    self.write(inner, VALUE, "bind", a["uses"], a["value"], self.span(a["node"]), kills=True,
                               note=f"sp_executesql argument {pname}", ast=a["node"])
                    if a["output"] and typ(a["node"]) == "VariableReference":
                        outer = self.ctx.relations.get(self.ctx.var_key(a["node"].get("Name"), self.ns)) or \
                            self.ctx.variable(a["node"].get("Name"), self.ns)
                        outputs.append((outer.key, inner.key, f"{pname} of the dynamic SQL"))
                self.ctx.nested.setdefault(self.step.id, {}).update({"outputs": outputs, "callee": "dynamic SQL",
                                                                     "drops_temps": True})
                return "dynamic SQL"
            # OUTPUT parameters are written by the dynamic batch
            for a in args:
                if a["output"] and typ(a["node"]) == "VariableReference":
                    var = a["node"].get("Name")
                    vrel = self.ctx.relations.get(self.ctx.var_key(var, self.ns)) or self.ctx.variable(var, self.ns)
                    if not self.ctx.nested_expanded(self.step.id):
                        self.write(vrel, VALUE, "exec-output", list(uses), f"output of dynamic SQL ({a['name']})",
                                   None, kills=True, status="partial", note="set by dynamic SQL that is not resolved")
            return "dynamic SQL"
        obj = self.ctx.catalog.find("", on.database, on.schema, on.name, self.ctx.default_db,
                                    [self.ctx.default_schema], kinds=("procedure",))
        system = on.name.lower().startswith(("sp_", "xp_")) and obj is None
        call = {"step": self.step.id, "kind": "procedure", "name": name, "linkedServer": linked,
                "supplied": obj is not None, "system": system, "insertExec": as_insert,
                "expanded": self.ctx.nested_expanded(self.step.id)}
        self.ctx.calls.append(call)
        d["callee"] = name
        d["system"] = system
        d["args"] = []
        input_uses: List[Use] = []
        outputs = []
        for i, p in enumerate(params):
            pname = (p.get("Variable") or {}).get("Name")
            val = p.get("ParameterValue")
            vu = self.expr(val, None, "argument", False, []) if val is not None else []
            d["args"].append({"name": pname or f"#{i + 1}", "value": self.sq(val, 80), "output": bool(p.get("IsOutput"))})
            if p.get("IsOutput") and typ(val) == "VariableReference":
                outputs.append((pname, val.get("Name")))
            input_uses.extend(vu)
        self.step.uses.extend(input_uses)
        expanded = self.ctx.nested_expanded(self.step.id)
        if not expanded:
            for pname, var in outputs:
                vrel = self.ctx.relations.get(self.ctx.var_key(var, self.ns)) or self.ctx.variable(var, self.ns)
                self.write(vrel, VALUE, "exec-output", [self._as(u, "argument", False) for u in input_uses],
                           f"output parameter {pname or ''} of {name}", None, kills=True, status="partial",
                           note=f"set inside {name}, which is not expanded here")
            if ret_var:
                vrel = self.ctx.relations.get(self.ctx.var_key(ret_var, self.ns)) or self.ctx.variable(ret_var, self.ns)
                self.write(vrel, VALUE, "exec-return", [], f"return code of {name}", None, kills=True,
                           status="partial", note=f"return code of {name}")
        else:
            self._bind_call(expanded, params, ret_var)
        return name

    def _bind_call(self, info: dict, params, ret_var) -> None:
        """Expanded call: parameters of the callee are set from the arguments here, and its OUTPUT
        parameters are copied back when it ends (``h_EndOfNested``)."""
        ns = info["namespace"]
        callee_params = info.get("params", [])
        given = {}
        positional = 0
        for p in params:
            pname = (p.get("Variable") or {}).get("Name")
            val = p.get("ParameterValue")
            if pname:
                given[pname.lower()] = (p, val)
            else:
                if positional < len(callee_params):
                    given[callee_params[positional]["name"].lower()] = (p, val)
                positional += 1
        outputs = []
        for cp in callee_params:
            pname = cp["name"]
            inner = self.ctx.variable(pname, ns, kind="parameter", data_type=cp.get("type", ""))
            if pname.lower() in given:
                p, val = given[pname.lower()]
                tf: List[str] = []
                uses = self.expr(val, None, "value", True, None, tf) if val is not None else []
                self.write(inner, VALUE, "bind", uses, self.sq(val, 200), self.span(val), kills=True, transforms=tf,
                           note=f"argument for {pname}", ast=val)
                if p.get("IsOutput") and typ(val) == "VariableReference":
                    outer = self.ctx.relations.get(self.ctx.var_key(val.get("Name"), self.ns)) or \
                        self.ctx.variable(val.get("Name"), self.ns)
                    outputs.append((outer.key, inner.key, f"{pname} of {info['callee']}"))
            else:
                self.write(inner, VALUE, "bind", [], cp.get("default") or "default", None, kills=True,
                           transforms=["default value"], note=f"{pname} not passed: its default applies")
        if ret_var:
            outer = self.ctx.relations.get(self.ctx.var_key(ret_var, self.ns)) or self.ctx.variable(ret_var, self.ns)
            outputs.append((outer.key, f"return:{ns}", f"return code of {info['callee']}"))
        self.ctx.nested.setdefault(self.step.id, {}).update({"outputs": outputs, "callee": info["callee"],
                                                             "drops_temps": True})
