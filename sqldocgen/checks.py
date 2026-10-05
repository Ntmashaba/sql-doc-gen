"""Review checks: things a developer should look at, ranked high / medium / low.

Every finding says why it matters and what to do next. Findings that depend on facts the
code cannot show (keys, nullability, which rows exist) are marked "possible", never stated as
certain. Checks read the analysis (versions, reads, the CFG) and, for patterns, the syntax tree.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .engine import Ctx, norm
from .model import ROWS, VALUE, Issue
from .program import ENTRY, EXIT, reverse_postorder
from .syntax import ident, obj_name, parts, typ, unparen, walk
from .trace import output_relations

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

FAMILIES = [
    ("nstring", ("nvarchar", "nchar", "ntext", "sysname")),
    ("string", ("varchar", "char", "text")),
    ("integer", ("bigint", "int", "smallint", "tinyint", "bit")),
    ("decimal", ("decimal", "numeric", "money", "smallmoney", "float", "real")),
    ("date", ("date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time")),
    ("guid", ("uniqueidentifier",)),
    ("binary", ("varbinary", "binary", "image", "rowversion", "timestamp")),
]


def family(sqltype: str) -> Optional[str]:
    base = (sqltype or "").lower().split("(")[0].strip()
    for fam, names in FAMILIES:
        if base in names:
            return fam
    return None


def _length(sqltype: str) -> Optional[int]:
    m = re.match(r"\s*n?(?:var)?char\s*\(\s*(\d+|max)\s*\)", sqltype or "", re.I)
    if not m:
        return None
    return 10 ** 9 if m.group(1).lower() == "max" else int(m.group(1))


class Checker:
    def __init__(self, ctx: Ctx, flow: dict, dynamic: Dict[str, dict], succ):
        self.ctx = ctx
        self.flow = flow
        self.dynamic = dynamic
        self.succ = succ
        self.issues: List[Issue] = []
        self.read_defs: Set[int] = set()
        self.read_rels: Set[str] = set()
        for st in ctx.steps:
            for u in st.uses:
                self.read_defs.update(u.defs)
                self.read_rels.add(u.rel)
        for n in ctx.nodes:
            for u in n.uses:
                self.read_defs.update(u.defs)
                self.read_rels.add(u.rel)
        self.outputs = set(output_relations(ctx, flow))
        self.exit_defs = set(flow["exit"])

    def name(self, key: str) -> str:
        rel = self.ctx.relations.get(key)
        return rel.name if rel else key

    def add(self, rule, severity, title, why, next_step, steps=(), spans=(), columns=(), relations=(),
            certainty="definite", text_id="main"):
        self.issues.append(Issue(rule=rule, severity=severity, title=title, why=why, next_step=next_step,
                                 steps=[s for s in steps if s], spans=[s for s in spans if s], columns=list(columns),
                                 relations=list(relations), certainty=certainty, text_id=text_id))

    def label(self, sid: str) -> str:
        st = self.ctx.step_by_id.get(sid)
        return st.label if st else sid

    # ------------------------------------------------------------------ run
    def run(self) -> List[Issue]:
        for fn in (self.dead_writes, self.unread_temp, self.empty_reads, self.conversions, self.not_in,
                   self.equals_null, self.top_without_order, self.sargability, self.left_join_rejected,
                   self.select_star, self.insert_without_columns, self.dynamic_sql, self.nolock, self.cursors,
                   self.transactions, self.error_patterns, self.duplicate_joins, self.select_assign_loops,
                   self.truncation, self.unreachable, self.sniffing, self.divide_by_zero, self.unresolved,
                   self.applied_twice):
            try:
                fn()
            except Exception as exc:          # a check must never stop the document
                self.add("check-failed", "info", f"A review check could not run ({fn.__name__})",
                         f"{type(exc).__name__}: {exc}", "Report this with the procedure if you can.")
        def own(issue):
            if not issue.steps:
                return True
            return any(not (self.ctx.step_by_id[s].origin or "").startswith("call:")
                       for s in issue.steps if s in self.ctx.step_by_id)
        self.issues = [i for i in self.issues if own(i)]
        self.issues.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.certainty != "definite",
                                        self._first_step(i)))
        return self.issues

    def _first_step(self, issue: Issue) -> int:
        idx = {s.id: i for i, s in enumerate(self.ctx.steps)}
        return min((idx.get(s, 10 ** 6) for s in issue.steps), default=10 ** 6)

    # ------------------------------------------------------------------ values written and lost
    def dead_writes(self):
        ctx = self.ctx
        grouped: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        for n in ctx.nodes:
            if n.step is None or n.op in ("local", "initial", "default", "create", "declare", "bind", "result",
                                          "exec-output", "exec-return", "return", "truncate", "delete"):
                continue
            if n.col in (ROWS, "*") or n.id in self.read_defs:
                continue
            rel = ctx.relations.get(n.rel)
            if rel is None or rel.kind in ("result", "return", "cursor"):
                continue
            if n.rel in self.outputs and n.id in self.exit_defs:
                continue
            st = ctx.step_by_id.get(n.step)
            if st is None or n.step not in self.flow["reachable"]:
                continue
            killed_by = self._killer(n)
            if killed_by:
                grouped[(n.rel, n.col)].append(n.id)
        read_keys = set()
        for st in ctx.steps:
            for u in st.uses:
                read_keys.add((u.rel, u.col))
        for n in ctx.nodes:
            for u in n.uses:
                read_keys.add((u.rel, u.col))
        for (rk, col), ids in grouped.items():
            rel = ctx.relations[rk]
            n0 = ctx.nodes[ids[0]]
            killer = self._killer(n0)
            what = f"{rel.name}" if col == VALUE else f"{rel.name}.{col}"
            steps = [ctx.nodes[i].step for i in ids]
            if (rk, col) not in read_keys and (rel.kind == "variable" or (rk, "*") not in read_keys):
                if rel.kind in ("temp", "table-variable"):
                    continue           # reported by the temp-column check
                self.add("assigned-never-read", "low", f"{what} is assigned but never read",
                         "Nothing in this code reads it, so the statements that assign it are dead code (or a "
                         "later statement uses a different variable by mistake).",
                         "Remove it, or check which statement was meant to use it.",
                         steps=steps, spans=[ctx.nodes[i].span for i in ids], columns=[f"{rk}|{col}"], relations=[rk])
                continue
            self.add("overwritten-before-read", "medium",
                     f"{what} is written at step {', '.join(self.label(s) for s in steps)} and replaced before anything reads it",
                     "The value written there never reaches an output: a later statement overwrites every row "
                     "first. Either that write is dead code, or the later one was meant to update only some rows.",
                     f"Check step {self.label(killer)}: should it have a WHERE clause or a join that limits it? "
                     f"If not, remove the earlier write.",
                     steps=steps + [killer], spans=[ctx.nodes[i].span for i in ids], columns=[f"{rk}|{col}"],
                     relations=[rk])

    def _killer(self, n) -> Optional[str]:
        """The step that replaces every row of this version before anything reads it, if any."""
        for st in self.ctx.steps:
            for nid in st.nodes:
                m = self.ctx.nodes[nid]
                if m.id != n.id and m.rel == n.rel and m.col == n.col and m.kills:
                    # m kills n if n reaches m's step
                    if (self.flow["IN"].get(st.id, 0) >> self.flow["bit"][n.id]) & 1:
                        return st.id
            if n.rel in st.detail.get("kill_rel", []) and st.id != n.step:
                if (self.flow["IN"].get(st.id, 0) >> self.flow["bit"][n.id]) & 1:
                    st_kind = st.kind
                    if st_kind in ("drop-table", "nested-end") and self.ctx.relations[n.rel].kind == "temp":
                        continue
                    return st.id
        return None

    def unread_temp(self):
        ctx = self.ctx
        for rk, rel in ctx.relations.items():
            if rel.kind not in ("temp", "table-variable") or not rel.created:
                continue
            writes = [n for n in ctx.nodes if n.rel == rk and n.step and n.op not in ("create", "local")]
            if not writes:
                continue
            if rk not in self.read_rels:
                self.add("temp-never-read", "medium", f"{rel.name} is filled but never read",
                         "Every row written to it is thrown away, so the work that fills it is wasted, or a later "
                         "statement reads the wrong table.",
                         "Remove the statements that fill it, or check which table the later steps were meant to read.",
                         steps=sorted({n.step for n in writes}), relations=[rk])
                continue
            read_cols = {u.col for st in ctx.steps for u in st.uses if u.rel == rk} | \
                        {u.col for n in ctx.nodes for u in n.uses if u.rel == rk}
            if "*" in read_cols:
                continue
            unread = sorted({n.col for n in writes if n.col not in read_cols and n.col not in (ROWS, "*")})
            if unread:
                self.add("temp-column-never-read", "low",
                         f"{rel.name}: {', '.join(unread[:6])}{'…' if len(unread) > 6 else ''} "
                         f"{'is' if len(unread) == 1 else 'are'} written but never read",
                         "Columns that are computed and never used cost time and make the logic harder to follow.",
                         "Drop them from the statements that fill the table, or check whether a later step should use them.",
                         steps=sorted({n.step for n in writes if n.col in unread}),
                         columns=[f"{rk}|{c}" for c in unread], relations=[rk])

    def empty_reads(self):
        ctx = self.ctx
        seen = set()
        for st in ctx.steps:
            for u in st.uses:
                if u.col != ROWS or u.rel in seen:
                    continue
                rel = ctx.relations.get(u.rel)
                if rel is None or rel.kind not in ("temp", "table-variable") or not rel.created:
                    continue
                ops = {ctx.nodes[d].op for d in u.defs}
                if u.defs and ops <= {"create", "truncate", "delete"} and st.id in self.flow["reachable"]:
                    seen.add(u.rel)
                    self.add("reads-empty-table", "high", f"Step {st.label} reads {rel.name} while it is still empty",
                             "On every path to this step nothing has been inserted into the table since it was "
                             "created or emptied, so this step works on no rows.",
                             "Check the order of the steps, and whether the statement that fills the table is "
                             "in a branch that does not run.",
                             steps=[st.id], spans=[u.span], relations=[u.rel])

    # ------------------------------------------------------------------ predicates
    def _type_of(self, op: dict) -> Tuple[Optional[str], str]:
        kind = op.get("kind")
        if kind in ("column", "variable"):
            rel = self.ctx.relations.get(op.get("rel"))
            if rel is None:
                return None, ""
            if kind == "variable":
                return family(rel.data_type), rel.data_type
            c = rel.find(op.get("col", ""))
            return (family(c.type), c.type) if c and c.type else (None, "")
        if kind == "literal":
            t = op.get("type")
            return {"varchar": "string", "nvarchar": "nstring", "int": "integer", "decimal": "decimal"}.get(t), t
        if kind == "expression" and op.get("type"):
            return family(op["type"]), op["type"]
        return None, ""

    def conversions(self):
        done = set()
        for c in self.ctx.comparisons:
            if c["role"] not in ("join", "filter", "merge", "having"):
                continue
            l, r = c["left"], c["right"]
            fl, tl = self._type_of(l)
            fr, tr = self._type_of(r)
            if not fl or not fr or fl == fr:
                continue
            pair = {fl, fr}
            col_side = None
            # the lower-precedence side is converted; flag only when that side is a column
            if pair == {"string", "nstring"}:
                col_side = l if fl == "string" else r
                sev, cert = "low", "possible"
                why = ("The varchar side is converted to nvarchar. Depending on the collation this can stop an index "
                       "seek on that column.")
            elif "string" in pair or "nstring" in pair:
                other = (pair - {"string", "nstring"}).pop()
                col_side = l if fl in ("string", "nstring") else r
                sev, cert = "medium", "definite"
                why = (f"The text side is converted to {other}. Every row's value is converted before comparing, so an "
                       f"index on it cannot be used, and a value that does not convert stops the statement with an error.")
                if col_side.get("kind") == "literal":
                    continue          # a literal converts once, harmlessly
            elif pair == {"integer", "decimal"}:
                continue
            else:
                col_side = l
                sev, cert = "medium", "definite"
                why = f"Comparing {fl} with {fr} forces an implicit conversion of one side for every row."
            if col_side is None or col_side.get("kind") not in ("column",):
                continue
            key = (c["step"], c["text"])
            if key in done:
                continue
            done.add(key)
            where = "join" if c["role"] == "join" else "filter"
            self.add("implicit-conversion", sev,
                     f"Implicit conversion in a {where} at step {self.label(c['step'])}: {c['text']}",
                     why + f" ({l.get('text')} is {tl or 'unknown'}; {r.get('text')} is {tr or 'unknown'}.)",
                     "Make both sides the same type: change the column or variable type, or CAST the non-column side.",
                     steps=[c["step"]], spans=[c["span"]], certainty=cert, text_id=c["text_id"])

    def not_in(self):
        for f in self.ctx.not_in:
            nullable = None
            for rk, col in f["uses"]:
                rel = self.ctx.relations.get(rk)
                c = rel.find(col) if rel else None
                if c is not None and c.nullable is False:
                    nullable = False
                elif c is not None and c.nullable:
                    nullable = True
            if nullable is False:
                continue
            self.add("not-in-nullable", "medium" if nullable else "low",
                     f"NOT IN against a subquery that may return NULL (step {self.label(f['step'])})",
                     "If the subquery returns a single NULL, NOT IN is never true and the statement silently matches no rows.",
                     "Use NOT EXISTS, or filter NULLs out inside the subquery (WHERE col IS NOT NULL).",
                     steps=[f["step"]], spans=[f["span"]], certainty="definite" if nullable else "possible",
                     text_id=f["text_id"])

    def equals_null(self):
        for c in self.ctx.comparisons:
            if c["left"].get("kind") == "null" or c["right"].get("kind") == "null":
                if c["op"] not in ("Equals", "NotEqualToBrackets", "NotEqualToExclamation"):
                    continue
                self.add("equals-null", "high", f"Comparison with NULL using {'=' if c['op'] == 'Equals' else '<>'} "
                                                f"(step {self.label(c['step'])}): {c['text']}",
                         "Under the default ANSI_NULLS ON setting a comparison with NULL is never true, so this "
                         "condition matches nothing (or everything, if negated).",
                         "Use IS NULL / IS NOT NULL.", steps=[c["step"]], spans=[c["span"]], text_id=c["text_id"])

    def _queries(self, st):
        """(QuerySpecification, inside EXISTS?) pairs in a step's own statement."""
        out = []
        stack = [(st.node, False)]
        while stack:
            n, in_exists = stack.pop()
            if not isinstance(n, dict):
                continue
            t = typ(n)
            if t in ("IfStatement", "WhileStatement", "TryCatchStatement", "BeginEndBlockStatement"):
                if t in ("IfStatement", "WhileStatement"):
                    stack.append((n.get("Predicate"), in_exists))
                continue
            if t == "QuerySpecification":
                out.append((n, in_exists))
            for k, v in n.items():
                if k in ("$", "@"):
                    continue
                child_exists = in_exists or t == "ExistsPredicate"
                if isinstance(v, dict):
                    stack.append((v, child_exists))
                elif isinstance(v, list):
                    stack.extend((x, child_exists) for x in v if isinstance(x, dict))
        return out

    def top_without_order(self):
        for st in self.ctx.steps:
            if st.kind in ("if", "while") and not st.uses:
                continue
            for q, in_exists in self._queries(st):
                top = q.get("TopRowFilter")
                if not top or in_exists or q.get("OrderByClause"):
                    continue
                if top.get("Percent") and str((top.get("Expression") or {}).get("Value", "")) == "100":
                    continue
                text = self.ctx.texts[st.text_id]
                self.add("top-without-order-by", "medium",
                         f"TOP without ORDER BY at step {st.label}",
                         "Which rows TOP keeps is not defined without ORDER BY; it can change between runs, "
                         "with indexes, statistics or parallelism.",
                         "Add an ORDER BY that makes the choice deterministic (include a unique column as a tie-breaker).",
                         steps=[st.id], spans=[text.span(top)], text_id=st.text_id)

    def sargability(self):
        done = set()
        for c in self.ctx.comparisons:
            if c["role"] not in ("filter", "join"):
                continue
            for side, other in ((c["left"], c["right"]), (c["right"], c["left"])):
                if side.get("kind") == "expression" and side.get("wraps_column") and \
                        other.get("kind") in ("literal", "variable"):
                    fn = side.get("fn", "")
                    if fn in ("CAST", "CONVERT", "TRY_CAST", "TRY_CONVERT") and family(side.get("type", "")) == "date":
                        continue          # CAST(column AS date) can still seek
                    if all(r.startswith(("temp:", "tvar:")) for r in side.get("wraps", [])):
                        continue          # staging tables are scanned anyway
                    if (c["step"], c["text"]) in done:
                        continue
                    done.add((c["step"], c["text"]))
                    self.add("non-sargable", "low",
                             f"A function or calculation wraps a column in a {c['role']} (step {self.label(c['step'])}): {c['text']}",
                             "The expression must be evaluated for every row, so an index on the column cannot be "
                             "used to find the matching rows.",
                             "Rewrite the condition so the column stands alone, for example "
                             "YEAR(d) = 2024 as d >= '20240101' AND d < '20250101'.",
                             steps=[c["step"]], spans=[c["span"]], certainty="possible", text_id=c["text_id"])

    def left_join_rejected(self):
        for st in self.ctx.steps:
            text = self.ctx.texts[st.text_id]
            for q, _ in self._queries(st):
                outer: Set[str] = set()
                for j in walk(q.get("FromClause") or {}, stop=lambda x: typ(x) in ("QueryDerivedTable", "ScalarSubquery")):
                    if typ(j) == "QualifiedJoin":
                        jt = j.get("QualifiedJoinType")
                        side = j.get("SecondTableReference") if jt == "LeftOuter" else (
                            j.get("FirstTableReference") if jt == "RightOuter" else None)
                        if side is None:
                            continue
                        for x in walk(side):
                            if typ(x) in ("NamedTableReference", "QueryDerivedTable", "VariableTableReference"):
                                a = ident(x.get("Alias")) or (obj_name(x.get("SchemaObject")).name
                                                              if x.get("SchemaObject") else "")
                                if a:
                                    outer.add(norm(a))
                if not outer:
                    continue
                wh = (q.get("WhereClause") or {}).get("SearchCondition")
                for term in self._and_terms(wh):
                    hit = self._rejects_null(term, outer)
                    if hit:
                        self.add("left-join-made-inner", "high",
                                 f"A WHERE condition on the outer side of a LEFT JOIN turns it into an inner join (step {st.label})",
                                 f"{text.squeeze(term, 120)} is false when {hit} has no matching row (its columns are "
                                 f"NULL), so those rows are dropped: the LEFT JOIN behaves like an INNER JOIN.",
                                 "Move the condition into the ON clause if unmatched rows should stay, or use an INNER "
                                 "JOIN if they should not, so the intent is visible.",
                                 steps=[st.id], spans=[text.span(term)], text_id=st.text_id)

    def _and_terms(self, b) -> List[dict]:
        out, stack = [], [b]
        while stack:
            x = unparen(stack.pop())
            if not isinstance(x, dict):
                continue
            if typ(x) == "BooleanBinaryExpression" and x.get("BinaryExpressionType") == "And":
                stack += [x.get("FirstExpression"), x.get("SecondExpression")]
            else:
                out.append(x)
        return out

    def _rejects_null(self, term, outer: Set[str]) -> Optional[str]:
        t = typ(term)
        if t == "BooleanIsNullExpression" and not term.get("IsNot"):
            return None
        if t == "BooleanBinaryExpression":       # OR: rejects only if every branch does
            a = self._rejects_null(unparen(term.get("FirstExpression")), outer)
            b = self._rejects_null(unparen(term.get("SecondExpression")), outer)
            return a if a and b else None
        if t not in ("BooleanComparisonExpression", "LikePredicate", "InPredicate", "BooleanTernaryExpression",
                     "BooleanIsNullExpression"):
            return None
        for x in walk(term, stop=lambda y: typ(y) in ("FunctionCall", "CoalesceExpression", "ScalarSubquery",
                                                         "SearchedCaseExpression", "SimpleCaseExpression", "IIfCall")):
            if typ(x) == "ColumnReferenceExpression":
                p = parts(x.get("MultiPartIdentifier"))
                if len(p) >= 2 and norm(p[-2]) in outer:
                    return p[-2]
        return None

    def select_star(self):
        for st in self.ctx.steps:
            if st.kind not in ("insert", "select-into"):
                continue
            target = self.ctx.relations.get(st.detail.get("target") or "")
            if target is None or target.kind not in ("table", "view", "remote", "global-temp"):
                continue
            stars = [x for x in walk(st.node, stop=lambda y: typ(y) == "ExistsPredicate") if typ(x) == "SelectStarExpression"]
            if stars:
                text = self.ctx.texts[st.text_id]
                self.add("select-star-into-table", "medium", f"SELECT * writes {target.name} (step {st.label})",
                         "The columns written depend on the source table's current definition: adding or reordering a "
                         "column there silently changes, or breaks, what lands in this table.",
                         "List the columns explicitly.", steps=[st.id], spans=[text.span(stars[0])],
                         relations=[target.key], text_id=st.text_id)

    def insert_without_columns(self):
        for st in self.ctx.steps:
            if st.kind not in ("insert", "insert-exec") or st.detail.get("column_list", True):
                continue
            target = self.ctx.relations.get(st.detail.get("target") or "")
            if target is None:
                continue
            permanent = target.kind in ("table", "view", "remote", "global-temp")
            self.add("insert-without-column-list", "medium" if permanent else "low",
                     f"INSERT into {target.name} without a column list (step {st.label})",
                     "Values are matched to columns by position, so a change to the table's column order or a new "
                     "column breaks the load or puts values in the wrong columns.",
                     "Name the target columns in the INSERT.", steps=[st.id], spans=[st.span], relations=[target.key],
                     text_id=st.text_id)

    def dynamic_sql(self):
        for label, info in self.dynamic.items():
            st = next((s for s in self.ctx.steps if s.label == label), None)
            if st is None:
                continue
            if info["status"] == "unresolved" or not info["parsed"]:
                self.add("dynamic-sql-unresolved", "high", f"Dynamic SQL at step {label} cannot be read",
                         "The statement is built at run time from values this document cannot see, so what it reads "
                         "and writes is unknown, not absent. Lineage stops here.",
                         "Read the code that builds the string; consider logging the generated SQL, or replacing it "
                         "with static SQL if the variations are few.", steps=[st.id], spans=[st.span],
                         text_id=st.text_id)
            elif info["status"] == "partial":
                self.add("dynamic-sql-partial", "medium",
                         f"Dynamic SQL at step {label} depends on run-time values ({', '.join(info['placeholders'][:3])})",
                         "The statement's shape was rebuilt, but some names or values come from variables, so the "
                         "objects it touches may differ at run time.",
                         "Check the values those variables can take; validate object names with QUOTENAME and a lookup.",
                         steps=[st.id], spans=[st.span], certainty="possible", text_id=st.text_id)

    def nolock(self):
        by_step = defaultdict(list)
        for h in self.ctx.hints:
            if h["hint"] in ("NoLock", "ReadUncommitted"):
                by_step[h["step"]].append(h)
        for sid, hs in by_step.items():
            names = sorted({self.name(h["relation"]) for h in hs})
            self.add("nolock", "medium", f"NOLOCK on {', '.join(names)} (step {self.label(sid)})",
                     "Reads under NOLOCK can see uncommitted rows, miss rows, or read the same row twice while pages "
                     "move, which can produce wrong totals that are hard to reproduce.",
                     "Remove the hint, or use READ COMMITTED SNAPSHOT / SNAPSHOT isolation if blocking was the reason.",
                     steps=[sid], spans=[h["span"] for h in hs])
        for st in self.ctx.steps:
            if st.detail.get("isolation") == "ReadUncommitted":
                self.add("nolock", "medium", f"READ UNCOMMITTED isolation (step {st.label})",
                         "Every following read behaves as if it had NOLOCK.",
                         "Use the default isolation level or snapshot isolation.", steps=[st.id], spans=[st.span])

    def cursors(self):
        for st in self.ctx.steps:
            if st.kind == "declare-cursor":
                self.add("cursor", "low", f"Row-by-row cursor {st.detail.get('cursor', '')} (step {st.label})",
                         "A cursor processes one row per loop iteration; on large tables this is usually far slower "
                         "than one set-based statement.",
                         "Check whether the loop body can be written as a single INSERT/UPDATE ... SELECT over the "
                         "cursor's query.", steps=[st.id], spans=[st.span], certainty="possible")

    def transactions(self):
        ctx = self.ctx
        steps = ctx.steps
        xact_abort = any(st.kind == "set-option" and "XactAbort" in str(st.detail.get("options", "")) and
                         st.detail.get("on") for st in steps)
        for st in steps:
            if st.kind == "begin-tran" and not st.in_try and not xact_abort:
                self.add("transaction-without-try", "medium", f"Transaction begun outside TRY/CATCH (step {st.label})",
                         "If a statement inside it fails, the error does not roll it back by default: the procedure "
                         "can continue or exit with part of the work done and the transaction left open.",
                         "Wrap the transaction in TRY/CATCH with ROLLBACK in the CATCH block, and/or SET XACT_ABORT ON.",
                         steps=[st.id], spans=[st.span])
        if not any(st.kind == "begin-tran" for st in steps):
            return
        # transaction depth along every path
        preds = defaultdict(set)
        for a, bs in self.succ.items():
            for b in bs:
                preds[b].add(a)
        order = [s for s in reverse_postorder(self.succ) if s not in (ENTRY, EXIT)]
        state: Dict[str, Set[int]] = defaultdict(set)
        out_state: Dict[str, Set[int]] = defaultdict(set)
        out_state[ENTRY] = {0}

        def transfer(st, counts):
            if st.kind == "begin-tran":
                return {min(c + 1, 3) for c in counts}
            if st.kind == "commit":
                return {max(c - 1, 0) for c in counts}
            if st.kind == "rollback":
                return {0}
            return set(counts)

        def edge_counts(src, dst, counts):
            st = ctx.step_by_id.get(src)
            if st is not None and st.kind == "if" and re.search(r"@@trancount|xact_state", st.detail.get("predicate", ""), re.I):
                dst_st = ctx.step_by_id.get(dst)
                in_then = dst_st is not None and any(c.step == src and c.branch == "then" for c in dst_st.conditions)
                if not in_then:
                    return {0}
            return counts

        changed, rounds = True, 0
        while changed and rounds < 100:
            changed, rounds = False, rounds + 1
            for s in order:
                inc = set()
                for p in preds[s]:
                    inc |= edge_counts(p, s, out_state[p])
                st = ctx.step_by_id[s]
                outc = transfer(st, inc)
                if inc != state[s] or outc != out_state[s]:
                    state[s], out_state[s] = inc, outc
                    changed = True
        exits = []
        for p in preds[EXIT]:
            if p == ENTRY:
                continue
            if xact_abort and ctx.step_by_id[p].kind == "throw":
                continue           # with XACT_ABORT ON the error that ends the batch rolls the transaction back
            if any(c > 0 for c in edge_counts(p, EXIT, out_state[p])):
                exits.append(p)
        if exits:
            ret = [s for s in exits if ctx.step_by_id[s].kind in ("return", "throw")] or exits
            self.add("open-transaction-on-exit", "high",
                     f"The procedure can end with a transaction still open (after step {', '.join(self.label(s) for s in ret[:4])})",
                     "On at least one path a BEGIN TRANSACTION is not matched by COMMIT or ROLLBACK before the "
                     "procedure returns. The caller is left holding locks, and the work is committed or rolled back "
                     "by whatever runs next.",
                     "Commit or roll back before every RETURN, or use TRY/CATCH with ROLLBACK and SET XACT_ABORT ON.",
                     steps=ret, spans=[ctx.step_by_id[s].span for s in ret])

    def error_patterns(self):
        for c in self.ctx.comparisons:
            for side in (c["left"], c["right"]):
                if side.get("kind") == "variable" and side.get("rel") == "system:@@error":
                    self.add("@@error", "low", f"@@ERROR check at step {self.label(c['step'])}",
                             "@@ERROR reflects only the statement immediately before it and resets after every "
                             "statement; errors that abort the batch never reach the check.",
                             "Use TRY/CATCH.", steps=[c["step"]], spans=[c["span"]], text_id=c["text_id"])
                    break

    def duplicate_joins(self):
        ctx = self.ctx
        for st in ctx.steps:
            if st.kind not in ("insert", "select-into", "update", "merge", "select"):
                continue
            text = ctx.texts[st.text_id]
            for q, in_exists in self._queries(st):
                if in_exists or q.get("GroupByClause") or q.get("UniqueRowFilter") == "Distinct":
                    continue
                self._check_joins(st, q.get("FromClause") or {}, text, st.kind == "update", q)
            if st.kind in ("update", "merge"):
                spec = st.node.get("UpdateSpecification") or st.node.get("MergeSpecification") or {}
                if st.kind == "update":
                    self._check_joins(st, spec.get("FromClause") or {}, text, True)
                else:
                    src = spec.get("TableReference")
                    if typ(src) == "NamedTableReference":
                        self._check_join_side(st, src, spec.get("SearchCondition"), text, True, merge=True)

    def _check_joins(self, st, from_clause, text, update, query=None):
        for j in walk(from_clause, stop=lambda x: typ(x) in ("QueryDerivedTable", "ScalarSubquery", "ExistsPredicate")):
            if typ(j) == "QualifiedJoin" and j.get("QualifiedJoinType") in ("Inner", "LeftOuter"):
                side = j.get("SecondTableReference")
                if typ(side) == "NamedTableReference":
                    self._check_join_side(st, side, j.get("SearchCondition"), text, update, query=query)

    def _check_join_side(self, st, side, cond, text, update, merge=False, query=None):
        on = obj_name(side.get("SchemaObject"))
        if not on.name:
            return
        rel = self.ctx.table_relation(on)
        keys = list(self.ctx.keys.get(rel.key, []))
        if rel.catalog is not None:
            keys += rel.catalog.keys
        if not keys:
            return
        alias = norm(ident(side.get("Alias")) or on.name)
        covered = set()
        for term in self._and_terms(cond):
            if typ(term) == "BooleanComparisonExpression" and term.get("ComparisonType") == "Equals":
                for x in (term.get("FirstExpression"), term.get("SecondExpression")):
                    x = unparen(x)
                    if typ(x) == "ColumnReferenceExpression":
                        p = parts(x.get("MultiPartIdentifier"))
                        if (len(p) >= 2 and norm(p[-2]) == alias) or (len(p) == 1 and rel.find(p[0])):
                            covered.add(norm(p[-1]))
        if any({norm(k) for k in key} <= covered for key in keys):
            return
        if query is not None and not update and not merge:
            # the rest of the key is selected: the query is meant to return one row per child row
            selected = set()
            for el in query.get("SelectElements", []) or []:
                for x in walk(el):
                    if typ(x) == "ColumnReferenceExpression":
                        p = parts(x.get("MultiPartIdentifier"))
                        if (len(p) >= 2 and norm(p[-2]) == alias) or len(p) == 1:
                            selected.add(norm(p[-1]))
            if any({norm(k) for k in key} <= covered | selected for key in keys):
                return
        key_txt = " / ".join("(" + ", ".join(k) + ")" for k in keys[:2])
        if update or merge:
            why = (f"{rel.name} is unique on {key_txt}, but the join uses only "
                   f"{', '.join(sorted(covered)) or 'other columns'}. When several of its rows match one target row, "
                   f"{'MERGE fails' if merge else 'the UPDATE uses one of them, and which one is not defined'}.")
        else:
            why = (f"{rel.name} is unique on {key_txt}, but the join uses only "
                   f"{', '.join(sorted(covered)) or 'other columns'}, so one row on the other side can match several "
                   f"of its rows and be written more than once.")
        self.add("possible-duplicate-join", "medium",
                 f"Join to {rel.name} may match several rows (step {st.label})", why,
                 f"Add the missing key columns to the join, or aggregate {rel.name} first so it has one row per join key.",
                 steps=[st.id], spans=[text.span(cond) if cond else text.span(side)], relations=[rel.key],
                 certainty="possible", text_id=st.text_id)

    def select_assign_loops(self):
        for n in self.ctx.nodes:
            if n.op != "select-assign" or not n.keeps_previous or not n.step:
                continue
            st = self.ctx.step_by_id[n.step]
            if any(c.branch == "loop" for c in st.conditions):
                rel = self.ctx.relations[n.rel]
                self.add("variable-kept-in-loop", "medium",
                         f"{rel.name} can keep the previous iteration's value (step {st.label})",
                         f"SELECT {rel.name} = ... assigns nothing when the query returns no rows, so inside a loop "
                         f"{rel.name} silently keeps the value from the previous iteration.",
                         f"Reset {rel.name} to NULL before the SELECT, or use SET {rel.name} = (SELECT ...), which "
                         f"assigns NULL when there are no rows.",
                         steps=[st.id], spans=[n.span], columns=[f"{n.rel}|{n.col}"], text_id=st.text_id)

    def truncation(self):
        for n in self.ctx.nodes:
            if not n.step or n.op not in ("insert", "update", "merge-update", "merge-insert", "set"):
                continue
            rel = self.ctx.relations.get(n.rel)
            if rel is None:
                continue
            target = rel.find(n.col) if n.col != VALUE else None
            ttype = target.type if target else (rel.data_type if n.col == VALUE else "")
            tlen = _length(ttype)
            if not tlen:
                continue
            direct = [u for u in n.uses if u.direct]
            if len(direct) != 1 or n.transforms not in ([], ["constant"]):
                continue
            u = direct[0]
            src = self.ctx.relations.get(u.rel)
            sc = None
            if src is not None:
                sc = src.find(u.col) if u.col != VALUE else None
            stype = sc.type if sc else (src.data_type if src is not None and u.col == VALUE else "")
            slen = _length(stype)
            if slen and slen > tlen:
                st = self.ctx.step_by_id[n.step]
                self.add("possible-truncation", "medium",
                         f"{rel.name}.{n.col} ({ttype}) receives {stype} (step {st.label})",
                         f"Values longer than {tlen} characters fail with a truncation error (or are cut off when "
                         f"ANSI_WARNINGS is OFF).",
                         "Widen the target column, or make the truncation explicit with LEFT() so it is intended.",
                         steps=[st.id], spans=[n.span], columns=[f"{n.rel}|{n.col}"], certainty="possible",
                         text_id=st.text_id)

    def unreachable(self):
        dead = [st for st in self.ctx.steps if st.id not in self.flow["reachable"]
                and st.kind not in ("label", "nested-end")]
        if dead:
            self.add("unreachable", "low",
                     f"Step{'s' if len(dead) > 1 else ''} {', '.join(s.label for s in dead[:8])} can never run",
                     "Nothing reaches these statements (they follow a RETURN, BREAK or GOTO).",
                     "Remove them, or check whether the RETURN was meant to be conditional.",
                     steps=[s.id for s in dead], spans=[s.span for s in dead[:5]])

    def sniffing(self):
        params = {k for k, r in self.ctx.relations.items() if r.kind == "parameter" and k.startswith("var:@")}
        filtered = defaultdict(set)
        for c in self.ctx.comparisons:
            if c["role"] not in ("filter", "join"):
                continue
            for side, other in ((c["left"], c["right"]), (c["right"], c["left"])):
                if side.get("rel") in params and c["op"] not in ("Equals",) and other.get("kind") == "column":
                    filtered[side["rel"]].add(c["step"])
        recompile = [st for st in self.ctx.steps if "RECOMPILE" in str([h.get("HintKind") for h in
                                                                        st.node.get("OptimizerHints", []) or []]).upper()]
        if filtered:
            names = sorted(self.name(k) for k in filtered)
            steps = sorted({s for v in filtered.values() for s in v})
            self.add("parameter-sniffing", "info",
                     f"Range filters on parameters {', '.join(names)}",
                     "The plan is compiled for the first values passed. If later calls pass very different ranges the "
                     "cached plan can be slow for them (parameter sniffing).",
                     "Only if performance varies between calls: consider OPTION (RECOMPILE) or OPTIMIZE FOR on the "
                     "affected statements.", steps=steps, certainty="possible")
        for st in recompile:
            self.add("option-recompile", "info", f"OPTION (RECOMPILE) at step {st.label}",
                     "The statement is compiled on every run: good for very different parameter values, costly when "
                     "called very often.", "No action needed unless the procedure runs many times a second.",
                     steps=[st.id], spans=[st.span])

    def divide_by_zero(self):
        for st in self.ctx.steps:
            text = self.ctx.texts[st.text_id]
            for x in walk(st.node, stop=lambda y: typ(y) in ("IfStatement", "WhileStatement", "TryCatchStatement",
                                                             "BeginEndBlockStatement")):
                if typ(x) == "BinaryExpression" and x.get("BinaryExpressionType") == "Divide":
                    d = unparen(x.get("SecondExpression"))
                    if typ(d) in ("ColumnReferenceExpression",):
                        self.add("possible-divide-by-zero", "low",
                                 f"Division by a column at step {st.label}: {text.squeeze(x, 80)}",
                                 "If the column is 0 for any row the statement fails with a divide-by-zero error.",
                                 "Use NULLIF(column, 0) in the divisor if a zero is possible.",
                                 steps=[st.id], spans=[text.span(x)], certainty="possible", text_id=st.text_id)
                        break

    def unresolved(self):
        bad = [u for u in self.ctx.unresolved if not u.get("note")]
        if bad:
            names = sorted({u["text"] for u in bad})
            self.add("unresolved-columns", "low",
                     f"{len(names)} column reference{'s' if len(names) > 1 else ''} could not be tied to a table",
                     "Without the table definitions some unqualified column names could belong to more than one "
                     "table. Their lineage is shown as Partial or Unresolved, which means unknown, not absent.",
                     "Supply the table definitions (--schema with CREATE scripts, a DACPAC or a column list), or "
                     "qualify the columns with table aliases.",
                     steps=sorted({u["step"] for u in bad}), spans=[u["span"] for u in bad[:6]],
                     certainty="possible")
        notcols = [u for u in self.ctx.unresolved if u.get("note")]
        for u in notcols[:20]:
            self.add("unknown-column", "medium", f"{u['note']} (step {self.label(u['step'])})",
                     "The definition this document was given does not have this column, so either the definition is "
                     "out of date or the statement fails when it runs.",
                     "Check the table definition supplied with --schema against the database.",
                     steps=[u["step"]], spans=[u["span"]])

    # ------------------------------------------------------------------ a factor applied twice
    WRAPPERS = {"ROUND", "ISNULL", "COALESCE", "NULLIF", "ABS", "FLOOR", "CEILING"}

    def _exponents(self, e, keys_at, sign, out):
        """Columns that multiply (+1) or divide (-1) a product term. Sums are not followed."""
        t = typ(e)
        if t in ("ParenthesisExpression",):
            self._exponents(e.get("Expression"), keys_at, sign, out)
        elif t == "BinaryExpression" and e.get("BinaryExpressionType") in ("Multiply", "Divide"):
            self._exponents(e.get("FirstExpression"), keys_at, sign, out)
            self._exponents(e.get("SecondExpression"), keys_at,
                            -sign if e.get("BinaryExpressionType") == "Divide" else sign, out)
        elif t == "FunctionCall" and ((e.get("FunctionName") or {}).get("Value") or "").upper() in self.WRAPPERS:
            params = e.get("Parameters") or []
            if params:
                self._exponents(params[0], keys_at, sign, out)
        elif t in ("CastCall", "ConvertCall", "TryCastCall", "TryConvertCall"):
            self._exponents(e.get("Parameter"), keys_at, sign, out)
        elif t == "ColumnReferenceExpression":
            k = keys_at(e)
            if k:
                out[k] = out.get(k, 0) + sign

    def _terms(self, e):
        """Top-level additive terms of an expression."""
        stack, out = [e], []
        while stack:
            x = stack.pop()
            if typ(x) == "ParenthesisExpression":
                stack.append(x.get("Expression"))
            elif typ(x) == "BinaryExpression" and x.get("BinaryExpressionType") in ("Add", "Subtract"):
                stack += [x.get("FirstExpression"), x.get("SecondExpression")]
            elif typ(x) == "FunctionCall" and ((x.get("FunctionName") or {}).get("Value") or "").upper() in ("ROUND",) \
                    and x.get("Parameters"):
                stack.append(x["Parameters"][0])
            else:
                out.append(x)
        return out

    def _factor_sets(self, n):
        """[{column: exponent}] per additive term of the value a version is computed with."""
        if not isinstance(n.ast, dict):
            return []
        text = self.ctx.texts[n.text_id]
        by_span = {tuple(u.span): (u.rel, u.col) for u in n.uses if u.direct and u.span}

        def keys_at(cre):
            sp = text.span(cre)
            return by_span.get(tuple(sp)) if sp else None

        out = []
        for term in self._terms(n.ast):
            ex = {}
            self._exponents(term, keys_at, 1, ex)
            out.append(ex)
        return out

    def applied_twice(self):
        ctx = self.ctx
        done = set()
        for n in ctx.nodes:
            if not n.step or n.op not in ("update", "merge-update", "set") or n.col in (ROWS, "*"):
                continue
            key = (n.rel, n.col)
            again = set()
            for ex in self._factor_sets(n):
                if ex.get(key, 0) > 0:
                    again |= {k for k, e in ex.items() if e > 0 and k != key}
            if not again:
                continue
            for u in n.uses:
                if not u.direct or (u.rel, u.col) != key:
                    continue
                for d in u.defs:
                    prev = ctx.nodes[d]
                    if prev.step is None:
                        continue
                    before = set()
                    for ex in self._factor_sets(prev):
                        before |= {k for k, e in ex.items() if e > 0}
                    for f in sorted(again & before):
                        if (n.id, f) in done:
                            continue
                        done.add((n.id, f))
                        rel = ctx.relations.get(n.rel)
                        fname = (ctx.relations[f[0]].name if f[0] in ctx.relations else f[0]) + "." + f[1]
                        what = rel.name if n.col == VALUE else f"{rel.name}.{n.col}"
                        self.add("factor-applied-twice", "high",
                                 f"{what} is multiplied by {fname} again at step {self.label(n.step)}",
                                 f"The value it starts from (written at step {self.label(prev.step)}) was already "
                                 f"multiplied by {fname}. Applying a rate, price or percentage twice is a classic cause "
                                 f"of values that are wrong by a factor for only some rows.",
                                 f"Compare steps {self.label(prev.step)} and {self.label(n.step)}: the second should "
                                 f"probably start from the unconverted value, or add only the new amount.",
                                 steps=[prev.step, n.step], spans=[prev.span, n.span], columns=[f"{n.rel}|{n.col}"],
                                 relations=[n.rel], certainty="possible", text_id=n.text_id)
