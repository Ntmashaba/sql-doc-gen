"""Which versions of a column can reach each read: reaching definitions over the CFG.

A write that replaces every row of a column (an UPDATE without a filter, SET @x = ...,
CREATE / TRUNCATE / SELECT INTO of a table) kills the earlier versions. A write that touches
only some rows (an UPDATE with WHERE or a join, an INSERT into a table that may already hold
rows) does not, so a later read can see either version. That is how the document can say
"possible sources" honestly when branches or partial updates differ.

Values that existed before the procedure ran (table contents, parameter values, temp tables
created by a caller) are "initial" versions.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set, Tuple

from .engine import Ctx
from .model import ROWS, VALUE
from .program import ENTRY, EXIT, reverse_postorder

# relation kinds whose contents exist before the procedure runs
EXTERNAL = {"table", "view", "function", "system", "remote", "file", "parameter", "global-temp", "temp",
            "table-variable", "unknown", "table-parameter"}


def all_uses(ctx: Ctx):
    """(step id, use, node or None) for every read in the procedure."""
    for st in ctx.steps:
        for u in st.uses:
            yield st.id, u, None
    for n in ctx.nodes:
        if n.step is None:
            continue
        for u in n.uses:
            yield n.step, u, n


def solve(ctx: Ctx, succ: Dict[str, Set[str]]) -> Dict[str, object]:
    nodes = ctx.nodes
    # ------------------------------------------------------------ initial versions for every external read
    defs_by_key: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    for n in nodes:
        if n.op != "local" and n.step is not None:
            defs_by_key[(n.rel, n.col)].append(n.id)
    wanted: Set[Tuple[str, str]] = set()
    for sid, u, _ in all_uses(ctx):
        if u.local is None and not u.rel.startswith(("system:", "unresolved:", "proc-result:")):
            wanted.add((u.rel, u.col))
    initial: Dict[Tuple[str, str], int] = {}
    for key in sorted(wanted):
        rel = ctx.relations.get(key[0])
        kind = rel.kind if rel else "unknown"
        if kind in EXTERNAL or (rel and rel.note == "not declared in this code"):
            n = ctx.node(rel=key[0], col=key[1], step=None, op="initial",
                         note=_initial_note(kind, rel))
            initial[key] = n.id
            defs_by_key[key].append(n.id)
    # ------------------------------------------------------------ bit positions
    def_ids = [n.id for n in nodes if n.op != "local" and (n.step is not None or n.op == "initial")]
    bit = {nid: i for i, nid in enumerate(def_ids)}
    key_mask: Dict[Tuple[str, str], int] = defaultdict(int)
    rel_mask: Dict[str, int] = defaultdict(int)
    for key, ids in defs_by_key.items():
        for nid in ids:
            key_mask[key] |= 1 << bit[nid]
            rel_mask[key[0]] |= 1 << bit[nid]
    gen: Dict[str, int] = defaultdict(int)
    kill: Dict[str, int] = defaultdict(int)
    for st in ctx.steps:
        for nid in st.nodes:
            n = nodes[nid]
            if n.op == "local":
                continue
            gen[st.id] |= 1 << bit[nid]
            if n.kills:
                kill[st.id] |= key_mask[(n.rel, n.col)]
        for rk in st.detail.get("kill_rel", []):
            kill[st.id] |= rel_mask[rk]
    init_mask = 0
    for nid in initial.values():
        init_mask |= 1 << bit[nid]
    # ------------------------------------------------------------ iterate to a fixed point
    order = [s for s in reverse_postorder(succ) if s not in (ENTRY, EXIT)]
    preds: Dict[str, Set[str]] = defaultdict(set)
    for a, bs in succ.items():
        for b in bs:
            preds[b].add(a)
    IN: Dict[str, int] = defaultdict(int)
    OUT: Dict[str, int] = defaultdict(int)
    OUT[ENTRY] = init_mask
    changed = True
    rounds = 0
    while changed and rounds < 200:
        changed = False
        rounds += 1
        for s in order:
            inm = 0
            for p in preds[s]:
                inm |= OUT[p]
            outm = gen[s] | (inm & ~kill[s])
            if inm != IN[s] or outm != OUT[s]:
                IN[s], OUT[s] = inm, outm
                changed = True
    exit_in = 0
    for p in preds[EXIT]:
        exit_in |= OUT[p]
    IN[EXIT] = exit_in
    reachable = set(order)
    # ------------------------------------------------------------ resolve every read
    rev = {i: nid for nid, i in bit.items()}

    def ids_of(mask: int) -> List[int]:
        out = []
        while mask:
            low = mask & -mask
            out.append(rev[low.bit_length() - 1])
            mask ^= low
        return sorted(out)

    for sid, u, owner in all_uses(ctx):
        if u.local is not None:
            u.defs = [u.local]
            continue
        key = (u.rel, u.col)
        if u.same_step:
            own = gen[sid] & key_mask.get(key, 0)
            mask = own or (IN[sid] & key_mask.get(key, 0))
        else:
            mask = IN[sid] & key_mask.get(key, 0)
        if owner is not None and not u.same_step and owner.id in bit:
            mask &= ~(1 << bit[owner.id])
        found = ids_of(mask)
        if u.col not in (ROWS, VALUE, "*"):
            # columns of a table filled with an unexpanded SELECT * come from those * versions,
            # unless a later write replaced every row of the column
            star = IN[sid] & key_mask.get((u.rel, "*"), 0)
            if star and not any(nodes[f].kills for f in found):
                found = sorted(set(found) | set(ids_of(star)))
                if u.status == "resolved":
                    u.status = "partial"
        u.defs = found
    return {"IN": IN, "OUT": OUT, "bit": bit, "reachable": reachable, "exit": ids_of(exit_in),
            "initial": initial, "ids_of": ids_of, "key_mask": key_mask}


def _initial_note(kind: str, rel) -> str:
    if kind == "parameter":
        return "value passed by the caller" + (f" (default {rel.default})" if rel and rel.default else "")
    if kind in ("temp",):
        return "already in the temp table when the procedure started (created by the caller)"
    if kind == "global-temp":
        return "already in the global temp table when the procedure started"
    if kind == "table-variable":
        return "table-valued parameter or table variable from outside this code"
    if kind == "file":
        return "contents of the file"
    if kind == "system":
        return "system catalog or dynamic management view"
    if kind == "variable":
        return "never assigned before this read (NULL)"
    return "data already in the table"
