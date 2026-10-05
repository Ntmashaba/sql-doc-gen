"""Output columns, and the backward (and forward) slices behind the column trace.

A trace starts from every version of an output column and walks back through:
  * the reads its value is computed from (direct),
  * the joins, filters, grouping, CASE predicates and row sets that decide which rows get
    which value (indirect), and
  * the IF / WHILE predicates the writing step depends on (indirect, condition).
Anything reached only through an indirect edge stays indirect, however direct the rest of
its path is. The page's JavaScript runs the same walk; tests keep the two in step.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, List, Optional, Tuple

from .engine import Ctx
from .model import ROWS

OUTPUT_KINDS = {"table", "view", "remote", "global-temp", "function"}


def output_relations(ctx: Ctx, flow: dict) -> List[str]:
    """Relation keys this procedure produces for someone else to read."""
    written = {ctx.nodes[n].rel for st in ctx.steps for n in st.nodes}
    out = []
    for key, rel in ctx.relations.items():
        if key not in written and not (rel.kind == "parameter" and rel.output):
            continue
        if rel.kind in OUTPUT_KINDS and key in written:
            out.append(key)
        elif rel.kind == "temp" and key in written and not rel.created:
            out.append(key)          # created by the caller: the caller reads what we wrote
        elif rel.kind == "result" and "inserted into" not in rel.note:
            out.append(key)
        elif rel.kind == "return" and key == "return:":
            out.append(key)
        elif rel.kind == "parameter" and rel.output and key.startswith("var:@"):
            out.append(key)
    return out


def output_columns(ctx: Ctx, flow: dict) -> List[dict]:
    """[{key, rel, col, defs}] for every column of every output relation, rows last."""
    rows: List[dict] = []
    exit_defs = set(flow["exit"])
    for rk in output_relations(ctx, flow):
        rel = ctx.relations[rk]
        defs_by_col: Dict[str, List[int]] = {}
        for n in ctx.nodes:
            if n.rel != rk or n.op == "local":
                continue
            if rel.kind == "parameter":
                if n.id in exit_defs:
                    defs_by_col.setdefault(n.col, []).append(n.id)
            elif n.step is not None:
                defs_by_col.setdefault(n.col, []).append(n.id)
        order = [c.name for c in rel.columns]
        cols = sorted(defs_by_col, key=lambda c: (c == ROWS, order.index(c) if c in order else len(order), c.lower()))
        for c in cols:
            rows.append({"key": f"{rk}|{c}", "rel": rk, "col": c, "defs": defs_by_col[c]})
    return rows


INDIRECT_STEP_ROLES = {"join", "filter", "group", "having", "top", "rows", "order", "merge", "condition",
                       "dynamic", "argument", "subquery", "case", "window"}


def backward(ctx: Ctx, starts: List[int], indirect: bool = True, limit: int = 20000) -> Dict[int, dict]:
    """Every column version that can contribute to ``starts``: {node id: {direct, depth, role}}."""
    seen: Dict[int, dict] = {}
    q = deque()
    for s in starts:
        seen[s] = {"direct": True, "depth": 0, "role": "start"}
        q.append(s)

    def visit(nid: int, direct: bool, depth: int, role: str):
        if ctx.nodes[nid].op == "create":
            return                     # an empty table being created adds no rows or values
        cur = seen.get(nid)
        if cur is None:
            if len(seen) >= limit:
                return
            seen[nid] = {"direct": direct, "depth": depth, "role": role}
            q.append(nid)
        elif direct and not cur["direct"]:
            cur.update(direct=True, depth=min(depth, cur["depth"]), role=role)
            q.append(nid)

    done_steps = set()
    while q:
        nid = q.popleft()
        info = seen[nid]
        n = ctx.nodes[nid]
        d0 = info["depth"] + 1
        for u in n.uses:
            if not u.direct and not indirect:
                continue
            for d in u.defs:
                visit(d, info["direct"] and u.direct, d0, u.role)
        if not indirect or n.step is None or n.op == "local" or n.step in done_steps:
            continue
        # step-level reads are indirect for every column the step writes: walk them once per step
        done_steps.add(n.step)
        st = ctx.step_by_id.get(n.step)
        if st is None:
            continue
        for u in st.uses:
            for d in u.defs:
                visit(d, False, d0, u.role)
        for c in st.conditions:
            if not c.step:
                continue
            ist = ctx.step_by_id.get(c.step)
            if ist is None:
                continue
            for u in ist.uses:
                for d in u.defs:
                    visit(d, False, d0, "condition")
    return seen


def forward_index(ctx: Ctx) -> Dict[int, List[Tuple[int, bool, str]]]:
    """node id -> [(node it feeds, direct, role)] including step-level and condition reads."""
    fwd: Dict[int, List[Tuple[int, bool, str]]] = {}
    by_step: Dict[str, List[int]] = {}
    for n in ctx.nodes:
        if n.step is not None:
            by_step.setdefault(n.step, []).append(n.id)
    for n in ctx.nodes:
        for u in n.uses:
            for d in u.defs:
                fwd.setdefault(d, []).append((n.id, u.direct, u.role))
    for st in ctx.steps:
        targets = by_step.get(st.id, [])
        for u in st.uses:
            for d in u.defs:
                for t in targets:
                    fwd.setdefault(d, []).append((t, False, u.role))
    # a condition decides every write under it
    for st in ctx.steps:
        for c in st.conditions:
            if not c.step or c.step not in ctx.step_by_id:
                continue
            for u in ctx.step_by_id[c.step].uses:
                for d in u.defs:
                    for t in by_step.get(st.id, []):
                        fwd.setdefault(d, []).append((t, False, "condition"))
    return fwd


def forward(ctx: Ctx, starts: List[int], fwd: Optional[dict] = None, limit: int = 20000) -> Dict[int, dict]:
    fwd = fwd if fwd is not None else forward_index(ctx)
    seen: Dict[int, dict] = {s: {"direct": True, "depth": 0} for s in starts}
    q = deque(starts)
    while q and len(seen) < limit:
        nid = q.popleft()
        info = seen[nid]
        for t, direct, role in fwd.get(nid, []):
            dd = info["direct"] and direct
            cur = seen.get(t)
            if cur is None:
                seen[t] = {"direct": dd, "depth": info["depth"] + 1}
                q.append(t)
            elif dd and not cur["direct"]:
                cur["direct"] = True
                q.append(t)
    return seen


def base_sources(ctx: Ctx, slice_: Dict[int, dict], direct_only: bool = True) -> List[Tuple[str, str]]:
    """The values that existed before the procedure ran (tables, parameters) a slice reaches."""
    out = []
    for nid, info in slice_.items():
        n = ctx.nodes[nid]
        if n.op == "initial" and (info["direct"] or not direct_only) and n.col != ROWS:
            out.append((n.rel, n.col))
    return sorted(set(out))


def column_status(ctx: Ctx, slice_: Dict[int, dict]) -> str:
    """Resolved, Partial or Unresolved for a traced column (direct part of the slice)."""
    direct = [ctx.nodes[n] for n, i in slice_.items() if i["direct"]]
    if not direct:
        return "unresolved"
    statuses = {n.status for n in direct}
    for n in direct:
        for u in n.uses:
            if u.direct:
                statuses.add(u.status)
                if not u.defs and not u.rel.startswith(("system:",)) and u.local is None:
                    statuses.add("partial")
    if statuses == {"unresolved"}:
        return "unresolved"
    if statuses - {"resolved"}:
        return "partial"
    return "resolved"
