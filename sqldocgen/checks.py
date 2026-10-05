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
from .program import ENTRY, EXIT, is_dynamic_exec, reverse_postorder
from .syntax import ident, obj_name, parts, typ, unparen, walk
from .textutil import scan_tokens
from .trace import output_relations

# a literal or NULL: a placeholder or a reset rather than a computed value
_CONSTANT = re.compile(r"^\s*\(*\s*(?:NULL|N?'(?:[^']|'')*'|[-+]?\d+(?:\.\d+)?|0x[0-9A-F]*|"
                       r"CAST\s*\(\s*NULL\s+AS\s+[^)]*\))\s*\)*\s*$", re.IGNORECASE)

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
            # an issue that lives entirely inside an expanded callee belongs to the callee's document
            keys = list(issue.relations) + [c.split("|")[0] for c in issue.columns]
            if keys and all("/" in k for k in keys):
                return False                 # only the callee's namespaced variables or tables
            if not issue.steps:
                return True
            return any(not (self.ctx.step_by_id[s].origin or "").startswith("call:")
                       for s in issue.steps if s in self.ctx.step_by_id)
        unique, seen = [], set()
        for i in self.issues:
            key = (i.rule, i.title, tuple(i.steps))
            if key not in seen:
                seen.add(key)
                unique.append(i)
        self.issues = self._merge_variants([i for i in unique if own(i)])
        self.issues.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.certainty != "definite",
                                        self._first_step(i)))
        return self.issues

    def _merge_variants(self, issues: List[Issue]) -> List[Issue]:
        """One finding per statement: the variants of a rebuilt dynamic statement repeat the same
        code, and so would their findings."""
        out: List[Issue] = []
        by_key: Dict[tuple, Issue] = {}
        label_rx = re.compile(r"\bsteps? [0-9][0-9.]*(?:, [0-9][0-9.]*)*")
        for i in issues:
            st = self.ctx.step_by_id.get(i.steps[0]) if i.steps else None
            if st is None or not st.parent or len(i.steps) != 1:
                out.append(i)
                continue
            parent = self.ctx.step_by_id.get(st.parent)
            if parent is None or not (parent.kind == "exec-dynamic" or is_dynamic_exec(parent.node)):
                out.append(i)
                continue
            code = " ".join(self.ctx.texts[st.text_id].squeeze(st.node, 400).split())
            key = (i.rule, label_rx.sub("", i.title), parent.id, code)
            first = by_key.get(key)
            if first is None:
                by_key[key] = i
                out.append(i)
                continue
            first.steps.append(i.steps[0])             # spans stay those of the first variant's text
            labels = [self.label(s) for s in first.steps]
            shown = ", ".join(labels[:4]) + (f" and {len(labels) - 4} more" if len(labels) > 4 else "")
            first.title = label_rx.sub(f"steps {shown}", first.title, count=1)
        return out

    def _first_step(self, issue: Issue) -> int:
        idx = {s.id: i for i, s in enumerate(self.ctx.steps)}
        return min((idx.get(s, 10 ** 6) for s in issue.steps), default=10 ** 6)

    # ------------------------------------------------------------------ values written and lost
    def dead_writes(self):
        ctx = self.ctx
        grouped: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        for n in ctx.nodes:
            if n.step is None or n.op in ("local", "initial", "default", "alter-add", "create", "declare", "bind", "result",
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
            what = f"{rel.name}" if col == VALUE else f"{rel.name}.{col}"
            if (rk, col) not in read_keys and (rel.kind == "variable" or (rk, "*") not in read_keys):
                if rel.kind in ("temp", "table-variable"):
                    continue           # reported by the temp-column check
                steps = [ctx.nodes[i].step for i in ids]
                self.add("assigned-never-read", "low", f"{what} is assigned but never read",
                         "Nothing in this code reads it, so the statements that assign it are dead code (or a "
                         "later statement uses a different variable by mistake).",
                         "Remove it, or check which statement was meant to use it.",
                         steps=steps, spans=[ctx.nodes[i].span for i in ids], columns=[f"{rk}|{col}"], relations=[rk])
                continue
            # a placeholder (NULL, '', 0) that a later statement fills in is how the code is meant to work
            ids = [i for i in ids if not _CONSTANT.match(ctx.nodes[i].expr or "")]
            if not ids:
                continue
            steps = [ctx.nodes[i].step for i in ids]
            killer = self._killer(ctx.nodes[ids[0]])
            if rel.kind == "temp" and (rel.name.lower() in self._literal_temp_names() or
                                       self._reach(steps, True) & self._reach([killer], False) & self._opaque_steps()):
                continue          # dynamic SQL or a called procedure in between may read it
            at = f"step {', '.join(self.label(s) for s in steps)}"
            kst = ctx.step_by_id.get(killer)
            if kst is not None and kst.kind in ("delete", "truncate", "drop-table"):
                self.add("overwritten-before-read", "low", f"{what} is written at {at} and never read",
                         f"Nothing reads the value before step {self.label(killer)} removes the rows, so the write "
                         f"is wasted work (or a later statement was meant to use it).",
                         "Remove the write, or check which statement was meant to read it.",
                         steps=steps + [killer], spans=[ctx.nodes[i].span for i in ids], columns=[f"{rk}|{col}"],
                         relations=[rk])
                continue
            if rel.kind in ("variable", "parameter"):
                self.add("overwritten-before-read", "low", f"{what} is set at {at} and set again before anything reads it",
                         "The first value is never used: a later statement assigns the variable again first. "
                         "Either the first assignment is dead code, or a statement in between was meant to use it.",
                         f"Check what should happen between that step and step {self.label(killer)}.",
                         steps=steps + [killer], spans=[ctx.nodes[i].span for i in ids], columns=[f"{rk}|{col}"],
                         relations=[rk])
                continue
            filled = all(ctx.nodes[i].op in ("insert", "select-into", "merge-insert", "output-into") for i in ids)
            if filled:
                self.add("overwritten-before-read", "low",
                         f"{what} is written at {at} and replaced before anything reads it",
                         f"The value inserted there is never used: step {self.label(killer)} overwrites it on every "
                         f"row first. Harmless if the insert only fills a required column, wasted work otherwise.",
                         f"Insert NULL (or a constant) there and let step {self.label(killer)} compute it, or check "
                         f"whether step {self.label(killer)} was meant to update only some rows.",
                         steps=steps + [killer], spans=[ctx.nodes[i].span for i in ids], columns=[f"{rk}|{col}"],
                         relations=[rk])
                continue
            self.add("overwritten-before-read", "medium",
                     f"{what} is written at {at} and replaced before anything reads it",
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

    # ------------------------------------------------------------------ what the analysis cannot see
    def _opaque_steps(self) -> Set[str]:
        """Steps that may read or fill a temp table without the analysis seeing it: dynamic SQL that
        was not fully read, and procedures that were called but not expanded (both see the caller's
        temp tables)."""
        if getattr(self, "_opaque", None) is None:
            out = set()
            for st in self.ctx.steps:
                if st.kind == "exec-dynamic" or (st.kind == "insert-exec" and is_dynamic_exec(st.node)):
                    info = self.dynamic.get(st.label) or {}
                    if not info.get("parsed") or info.get("status") != "resolved":
                        out.add(st.id)
                elif st.kind in ("exec", "insert-exec") and not self.ctx.expanded.get(st.id):
                    callee = (st.detail.get("callee") or "").lower()
                    if not (callee.startswith(("xp_", "sys.", "master.sys.")) or callee in ("sp_executesql",)):
                        out.add(st.id)
            self._opaque = out
        return self._opaque

    def _literal_temp_names(self) -> Set[str]:
        """#names written inside string literals: dynamic SQL may use those tables."""
        if getattr(self, "_lit_names", None) is None:
            names = set()
            for text in self.ctx.texts.values():
                src = text.text
                for kind, a, b in scan_tokens(src):
                    if kind == "string" and "#" in src[a:b]:
                        names.update(m.group(0).lower() for m in re.finditer(r"#{1,2}[A-Za-z0-9_@$#]+", src[a:b]))
            self._lit_names = names
        return self._lit_names

    def _reach(self, starts, forward: bool) -> Set[str]:
        if forward:
            graph = self.succ
        else:
            if getattr(self, "_pred", None) is None:
                self._pred = defaultdict(set)
                for a, bs in self.succ.items():
                    for b in bs:
                        self._pred[b].add(a)
            graph = self._pred
        seen, stack = set(), list(starts)
        while stack:
            x = stack.pop()
            for y in graph.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return seen

    def _hidden_use(self, rel, steps, forward: bool) -> bool:
        """Could a temp table be used where the analysis cannot see (before or after these steps)?"""
        if rel.kind != "temp":
            return False                    # table variables are invisible to callees and dynamic SQL
        if rel.name.lower() in self._literal_temp_names():
            return True
        opaque = self._opaque_steps()
        return bool(opaque) and bool(self._reach(steps, forward) & opaque)

    def unread_temp(self):
        ctx = self.ctx
        for rk, rel in ctx.relations.items():
            if rel.kind not in ("temp", "table-variable") or not rel.created:
                continue
            writes = [n for n in ctx.nodes if n.rel == rk and n.step and n.op not in ("create", "local")]
            if not writes:
                continue
            if self._hidden_use(rel, {n.step for n in writes}, forward=True):
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
                    if self._hidden_use(rel, {st.id}, forward=False):
                        continue                # filled by dynamic SQL or a called procedure, maybe
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
            if sev == "medium" and str(col_side.get("rel", "")).startswith(("temp:", "tvar:")):
                # a staging table is scanned anyway: only the conversion error risk is left
                sev = "low"
                why = why.replace("so an index on it cannot be used, and a value", "and a value")
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
                count = str((unparen(top.get("Expression")) or {}).get("Value", ""))
                if top.get("Percent") and count == "100":
                    continue
                tables = (q.get("FromClause") or {}).get("TableReferences") or []
                if not tables:
                    continue          # no FROM: one row, TOP changes nothing
                text = self.ctx.texts[st.text_id]
                if count == "1" and not top.get("Percent"):
                    self.add("top-without-order-by", "low", f"TOP 1 without ORDER BY at step {st.label}",
                             "If several rows match, which one is used is not defined and can change between runs.",
                             "If more than one row can match, add an ORDER BY that picks the intended one.",
                             steps=[st.id], spans=[text.span(top)], text_id=st.text_id, certainty="possible")
                    continue
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
                    if all(r.startswith(("temp:", "tvar:")) or
                           (self.ctx.relations.get(r) is not None and self.ctx.relations[r].kind in ("system", "dynamic"))
                           for r in side.get("wraps", [])):
                        continue          # staging tables and system views are scanned anyway
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
                # the * expands to the columns of the tables it reads; the procedure's own temp tables
                # cannot change behind its back
                if self._star_stable(st):
                    continue
                text = self.ctx.texts[st.text_id]
                self.add("select-star-into-table", "medium" if st.kind == "insert" else "low",
                         f"SELECT * writes {target.name} (step {st.label})",
                         "The columns written depend on the source table's current definition: adding or reordering a "
                         "column there silently changes, or breaks, what lands in this table.",
                         "List the columns explicitly.", steps=[st.id], spans=[text.span(stars[0])],
                         relations=[target.key], text_id=st.text_id)

    def _star_stable(self, st, depth: int = 0) -> bool:
        """SELECT * that reads only the procedure's own temp tables, themselves defined with explicit
        columns, cannot change when a permanent table changes."""
        sources = {u.rel for u in st.uses} | {u.rel for nid in st.nodes for u in self.ctx.nodes[nid].uses}
        rels = [self.ctx.relations[r] for r in sources if r in self.ctx.relations]
        if not rels or depth > 4:
            return False
        for rel in rels:
            if rel.kind in ("variable", "parameter"):
                continue
            if rel.kind not in ("temp", "table-variable"):
                return False
            for sid in rel.created:
                cst = self.ctx.step_by_id.get(sid)
                if cst is None:
                    continue
                starred = any(typ(x) == "SelectStarExpression" for x in walk(cst.node))
                if starred and not self._star_stable(cst, depth + 1):
                    return False
        return True

    def insert_without_columns(self):
        by_target: Dict[str, list] = defaultdict(list)
        for st in self.ctx.steps:
            if st.kind not in ("insert", "insert-exec") or st.detail.get("column_list", True):
                continue
            target = self.ctx.relations.get(st.detail.get("target") or "")
            if target is not None:
                by_target[target.key].append(st)
        for key, steps in by_target.items():
            target = self.ctx.relations[key]
            permanent = target.kind in ("table", "view", "remote", "global-temp")
            n = len(steps)
            at = f"step {steps[0].label}" if n == 1 else \
                f"{n} statements, steps {', '.join(st.label for st in steps[:5])}{', …' if n > 5 else ''}"
            self.add("insert-without-column-list", "medium" if permanent else "low",
                     f"INSERT into {target.name} without a column list ({at})",
                     "Values are matched to columns by position, so a change to the table's column order or a new "
                     "column breaks the load or puts values in the wrong columns.",
                     "Name the target columns in the INSERT.", steps=[st.id for st in steps],
                     spans=[steps[0].span], relations=[key], text_id=steps[0].text_id)

    def dynamic_sql(self):
        partial: Dict[tuple, list] = {}
        for label, info in self.dynamic.items():
            st = next((s for s in self.ctx.steps if s.label == label), None)
            if st is None:
                continue
            unsafe = info.get("unsafe") or []
            if unsafe and st.namespace == "":
                one = len(unsafe) == 1
                names = " and ".join(unsafe) if len(unsafe) <= 2 else ", ".join(unsafe[:-1]) + " and " + unsafe[-1]
                self.add("sql-injection-risk", "high",
                         f"{names} {'is' if one else 'are'} pasted into dynamic SQL at step {label} without quoting",
                         f"{'This parameter is' if one else 'These parameters are'} text chosen by whoever calls the "
                         f"procedure, and the text becomes part of the statement, so a value like x'; DROP TABLE … -- "
                         f"runs as SQL with the procedure's permissions (SQL injection).",
                         "Pass values as sp_executesql parameters instead of concatenating them; wrap object names in "
                         "QUOTENAME() and check them against sys.objects first.",
                         steps=[st.id], spans=[st.span], certainty="possible", text_id=st.text_id)
            if info["status"] == "unresolved" or not info["parsed"]:
                self.add("dynamic-sql-unresolved", "medium", f"Dynamic SQL at step {label} cannot be read",
                         "The statement is built at run time from values this document cannot see, so what it reads "
                         "and writes is unknown, not absent. Lineage stops here.",
                         "Read the code that builds the string; consider logging the generated SQL, or replacing it "
                         "with static SQL if the variations are few.", steps=[st.id], spans=[st.span],
                         text_id=st.text_id)
            elif info["status"] == "partial":
                partial.setdefault(tuple(info["placeholders"][:3]), []).append(st)
        for values, steps in partial.items():
            n = len(steps)
            at = f"step {steps[0].label}" if n == 1 else \
                f"{n} statements (steps {', '.join(st.label for st in steps[:5])}{', …' if n > 5 else ''})"
            self.add("dynamic-sql-partial", "low",
                     f"Dynamic SQL at {at} depends on run-time values ({', '.join(values)})",
                     "The statement's shape was rebuilt, but some names or values come from variables, so the "
                     "objects it touches may differ at run time.",
                     "Check the values those variables can take; validate object names with QUOTENAME and a lookup.",
                     steps=[st.id for st in steps], spans=[steps[0].span], certainty="possible",
                     text_id=steps[0].text_id)

    def nolock(self):
        # dirty reads matter for business data; on system views, DMVs and the procedure's own temp
        # tables NOLOCK is harmless (and usual in diagnostic procedures)
        harmless = ("system", "temp", "global-temp", "table-variable")
        by_step = defaultdict(list)
        for h in self.ctx.hints:
            rel = self.ctx.relations.get(h["relation"])
            if h["hint"] in ("NoLock", "ReadUncommitted") and not (rel is not None and rel.kind in harmless):
                by_step[h["step"]].append(h)
        def reads_user_tables(after):
            # SET TRANSACTION ISOLATION LEVEL lasts until the end of its batch (procedure or dynamic SQL)
            seen = False
            for st in self.ctx.steps:
                if st.id == after.id:
                    seen = True
                    continue
                if not seen or st.text_id != after.text_id or st.namespace != after.namespace:
                    continue
                for u in st.uses:
                    rel = self.ctx.relations.get(u.rel)
                    if rel is not None and rel.kind in ("table", "view", "remote"):
                        return True
            return False
        for sid, hs in by_step.items():
            names = sorted({self.name(h["relation"]) for h in hs})
            self.add("nolock", "medium", f"NOLOCK on {', '.join(names)} (step {self.label(sid)})",
                     "Reads under NOLOCK can see uncommitted rows, miss rows, or read the same row twice while pages "
                     "move, which can produce wrong totals that are hard to reproduce.",
                     "Remove the hint, or use READ COMMITTED SNAPSHOT / SNAPSHOT isolation if blocking was the reason.",
                     steps=[sid], spans=[h["span"] for h in hs])
        for st in self.ctx.steps:
            if st.detail.get("isolation") == "ReadUncommitted" and reads_user_tables(st):
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
                if self._aggregates_only(q):
                    continue          # SELECT SUM(...) over the join: counting the matches is the point
                self._check_joins(st, q.get("FromClause") or {}, text, False, q)
            if st.kind in ("update", "merge"):
                spec = st.node.get("UpdateSpecification") or st.node.get("MergeSpecification") or {}
                if st.kind == "update":
                    target = obj_name((spec.get("Target") or {}).get("SchemaObject"))
                    self._check_joins(st, spec.get("FromClause") or {}, text, True,
                                      target=norm(target.name) if target.name else None)
                else:
                    src = spec.get("TableReference")
                    if typ(src) == "NamedTableReference":
                        self._check_join_side(st, src, spec.get("SearchCondition"), text, True, merge=True)

    @staticmethod
    def _aggregates_only(q) -> bool:
        elements = q.get("SelectElements", []) or []
        if not elements:
            return False
        for el in elements:
            found = False
            for x in walk(el, stop=lambda y: typ(y) in ("ScalarSubquery", "QueryDerivedTable")):
                if typ(x) == "FunctionCall" and ((x.get("FunctionName") or {}).get("Value") or "").upper() in \
                        ("SUM", "COUNT", "COUNT_BIG", "MIN", "MAX", "AVG", "STRING_AGG", "STDEV", "VAR") \
                        and not x.get("OverClause"):
                    found = True
                    break
            if not found:
                return False
        return True

    def _from_aliases(self, st, from_clause) -> Dict[str, object]:
        """alias -> relation for the tables and table variables named in a FROM clause."""
        out = {}
        for t in walk(from_clause, stop=lambda x: typ(x) in ("QueryDerivedTable", "ScalarSubquery", "ExistsPredicate")):
            if typ(t) == "NamedTableReference":
                on = obj_name(t.get("SchemaObject"))
                if on.name:
                    out[norm(ident(t.get("Alias")) or on.name)] = self.ctx.table_relation(on)
            elif typ(t) == "VariableTableReference":
                name = (t.get("Variable") or {}).get("Name") or ""
                rel = self.ctx.relations.get(f"tvar:{st.namespace}{norm(name)}")
                if rel is not None:
                    out[norm(ident(t.get("Alias")) or name)] = rel
        return out

    def _keys_of(self, rel) -> List[List[str]]:
        keys = list(self.ctx.keys.get(rel.key, []))
        if rel.catalog is not None:
            keys += rel.catalog.keys
        return keys + [[c] for c in self._sequence_columns().get(rel.key, [])]

    def _sequence_columns(self) -> Dict[str, List[str]]:
        """Temp-table and table-variable columns only ever filled from NEXT VALUE FOR: unique in practice."""
        if getattr(self, "_seq_cols", None) is None:
            seen: Dict[Tuple[str, str], bool] = {}
            for n in self.ctx.nodes:
                if n.step is None or n.col in (ROWS, VALUE) or n.op in ("create", "declare", "initial", "local", "drop"):
                    continue
                if not n.rel.startswith(("temp:", "tvar:")):
                    continue
                ok = bool(re.match(r"\s*NEXT\s+VALUE\s+FOR\b", n.expr or "", re.IGNORECASE))
                seen[(n.rel, n.col)] = seen.get((n.rel, n.col), True) and ok
            self._seq_cols = defaultdict(list)
            for (rel, col), ok in seen.items():
                if ok:
                    self._seq_cols[rel].append(col)
        return self._seq_cols

    def _check_joins(self, st, from_clause, text, update, query=None, target=None):
        aliases = self._from_aliases(st, from_clause)
        for j in walk(from_clause, stop=lambda x: typ(x) in ("QueryDerivedTable", "ScalarSubquery", "ExistsPredicate")):
            if typ(j) == "QualifiedJoin" and j.get("QualifiedJoinType") in ("Inner", "LeftOuter"):
                side = j.get("SecondTableReference")
                if typ(side) == "NamedTableReference":
                    self._check_join_side(st, side, j.get("SearchCondition"), text, update, query=query,
                                          aliases=aliases, target=target)

    def _check_join_side(self, st, side, cond, text, update, merge=False, query=None, aliases=None, target=None):
        on = obj_name(side.get("SchemaObject"))
        if not on.name:
            return
        rel = self.ctx.table_relation(on)
        keys = self._keys_of(rel)
        if not keys:
            return
        alias = norm(ident(side.get("Alias")) or on.name)
        if update and target and alias == target:
            return               # the rows being updated: each is still updated once
        covered = set()
        other: Dict[str, Set[str]] = defaultdict(set)      # the other side's columns, by alias
        for term in self._and_terms(cond):
            if typ(term) == "InPredicate" and not term.get("NotDefined") and not term.get("Subquery"):
                # tb.index_id IN (0, 1): the key column is pinned to the listed values
                x = unparen(term.get("Expression"))
                if typ(x) == "ColumnReferenceExpression":
                    p = parts(x.get("MultiPartIdentifier"))
                    if (len(p) >= 2 and norm(p[-2]) == alias) or (len(p) == 1 and rel.find(p[0])):
                        covered.add(norm(p[-1]))
                continue
            if typ(term) == "BooleanComparisonExpression" and term.get("ComparisonType") == "Equals":
                pair = [unparen(term.get("FirstExpression")), unparen(term.get("SecondExpression"))]
                mine = []
                for x in pair:
                    if typ(x) == "ColumnReferenceExpression":
                        p = parts(x.get("MultiPartIdentifier"))
                        if (len(p) >= 2 and norm(p[-2]) == alias) or (len(p) == 1 and rel.find(p[0])):
                            covered.add(norm(p[-1]))
                            mine.append(x)
                for x in pair:
                    if x not in mine and mine and typ(x) == "ColumnReferenceExpression":
                        p = parts(x.get("MultiPartIdentifier"))
                        if len(p) >= 2:
                            other[norm(p[-2])].add(norm(p[-1]))
        if any({norm(k) for k in key} <= covered for key in keys):
            return
        if not update and not merge:
            # A join from a row's own key to child rows (order -> order lines) is meant to return
            # one row per child: only a join where the other side repeats too is suspicious.
            other_keys = [(a, cols, self._keys_of(aliases[a])) for a, cols in other.items()
                          if aliases and a in aliases]
            if not other_keys or not all(k for _, _, k in other_keys):
                return           # the other side's keys are unknown: nothing to show
            if any(any({norm(c) for c in key} <= cols for key in akeys) for _, cols, akeys in other_keys):
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
        by_step: Dict[str, list] = defaultdict(list)
        for n in self.ctx.nodes:
            if n.op == "select-assign" and n.keeps_previous and n.step:
                st = self.ctx.step_by_id[n.step]
                if any(c.branch == "loop" for c in st.conditions):
                    by_step[st.id].append(n)
        for sid, assigned in by_step.items():
            st = self.ctx.step_by_id[sid]
            # the value from the previous iteration must be able to come round to this SELECT:
            # a reset (SET @x = NULL) later in the loop body, or before the SELECT, stops it
            stale = [n for n in assigned if (self.flow["IN"].get(sid, 0) >> self.flow["bit"][n.id]) & 1]
            if not stale or self._rowcount_checked(sid):
                continue
            names_all = [self.ctx.relations[n.rel].name for n in assigned]
            # WHILE @id IS NULL BEGIN SELECT @id = ... END repeats only when nothing was assigned
            loop = [c for c in st.conditions if c.branch == "loop"][-1]
            if any(re.search(re.escape(a) + r"\b", loop.text, re.IGNORECASE) for a in names_all):
                continue
            # WHILE EXISTS (SELECT 1 FROM @queue) BEGIN SELECT TOP 1 @x = ... FROM @queue ... END
            read = {u.rel for n in assigned for u in n.uses} | {u.rel for u in st.uses}
            read.discard("")
            loop_st = self.ctx.step_by_id.get(loop.step)
            if len(read) == 1 and loop_st is not None and read <= {u.rel for u in loop_st.uses} \
                    and not (st.node.get("QueryExpression") or {}).get("WhereClause"):
                continue
            # SET @id = NULL; SELECT @id = ..., @x = ...; IF @id IS NULL BREAK -- a sentinel guards the rest
            fresh = [self.ctx.relations[n.rel].name for n in assigned if n not in stale]
            if fresh and self._tested_next(sid, fresh):
                continue
            names = [self.ctx.relations[n.rel].name for n in stale]
            one = len(names) == 1
            listed = names[0] if one else ", ".join(names[:-1]) + " and " + names[-1]
            self.add("variable-kept-in-loop", "medium",
                     f"{listed} can keep the previous iteration's value{'' if one else 's'} (step {st.label})",
                     f"SELECT {names[0]} = ... assigns nothing when the query returns no rows, so inside a loop "
                     f"{'it' if one else 'each of them'} silently keeps the value from the previous iteration.",
                     f"Reset {'it' if one else 'them'} to NULL before the SELECT, or use SET {names[0]} = (SELECT ...), "
                     f"which assigns NULL when there are no rows.",
                     steps=[st.id], spans=[n.span for n in stale], columns=[f"{n.rel}|{n.col}" for n in stale],
                     text_id=st.text_id)

    def _tested_next(self, sid: str, names: List[str]) -> bool:
        for nxt in self.succ.get(sid, ()):
            st = self.ctx.step_by_id.get(nxt)
            if st is not None and st.kind in ("if", "while"):
                pred = st.detail.get("predicate") or ""
                if any(re.search(re.escape(a) + r"\b", pred, re.IGNORECASE) for a in names):
                    return True
        return False

    def _rowcount_checked(self, sid: str) -> bool:
        """IF @@ROWCOUNT = 0 BREAK (or SET @n = @@ROWCOUNT) right after the SELECT handles 'no rows'."""
        for nxt in self.succ.get(sid, ()):
            st = self.ctx.step_by_id.get(nxt)
            if st is None:
                continue
            text = (st.detail.get("predicate") or "") if st.kind in ("if", "while") else \
                (st.detail.get("expression") or "") if st.kind in ("set", "select-assign") else ""
            if "@@ROWCOUNT" in text.upper() or "ROWCOUNT_BIG()" in text.upper():
                return True
        return False

    def truncation(self):
        found: Dict[Tuple[str, str, str, str], list] = {}
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
            # (max) only says the type is unbounded, not that long values occur: compare real lengths
            if slen and slen > tlen and slen < 10 ** 9:
                found.setdefault((n.rel, n.col, ttype, stype), []).append(n)
        for (rk, col, ttype, stype), nodes in found.items():
            rel = self.ctx.relations[rk]
            steps = [self.ctx.step_by_id[n.step] for n in nodes]
            what = rel.name if col == VALUE else f"{rel.name}.{col}"
            at = "step" + ("s " if len(steps) > 1 else " ") + ", ".join(st.label for st in steps)
            # a declared table column is a fact about the data; a temp column or variable only a guess
            from_table = any(self.ctx.relations.get(u.rel) is not None and
                             self.ctx.relations[u.rel].kind in ("table", "view", "remote", "system")
                             for n in nodes for u in n.uses if u.direct)
            self.add("possible-truncation", "medium" if from_table else "low", f"{what} ({ttype}) receives {stype} ({at})",
                     f"Values longer than {_length(ttype)} characters fail with a truncation error (or are cut off "
                     f"when ANSI_WARNINGS is OFF).",
                     "Widen the target column, or make the truncation explicit with LEFT() so it is intended.",
                     steps=[st.id for st in steps], spans=[n.span for n in nodes], columns=[f"{rk}|{col}"],
                     certainty="possible", text_id=steps[0].text_id)

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
        if recompile:
            n = len(recompile)
            at = (f"step {recompile[0].label}" if n == 1 else
                  f"{n} statements (steps {', '.join(st.label for st in recompile[:5])}{', …' if n > 5 else ''})")
            self.add("option-recompile", "info", f"OPTION (RECOMPILE) on {at}",
                     "Each such statement is compiled on every run: good for very different parameter values, costly "
                     "when the procedure is called very often.",
                     "No action needed unless the procedure runs many times a second.",
                     steps=[st.id for st in recompile], spans=[st.span for st in recompile[:5]])

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
