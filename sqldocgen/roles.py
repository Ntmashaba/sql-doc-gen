"""What each statement is for: the logic that moves and decides data, or housekeeping.

Long ETL procedures spend many statements on bookkeeping: keeping @@ROWCOUNT after every
statement, a log row after every section, progress messages, debug output, declarations.
They matter to whoever runs the job, not to what the procedure computes, so the page folds
them and says why.

The rule is a program slice. A statement is *logic* when it can affect a business output (a
data table, a result set or an OUTPUT parameter) through the values it writes, the rows it
decides or the branches it controls. Statements that move data through tables are logic in
any case, so a staging step whose result is never used still shows (the review issues say
it is unused). Everything else is housekeeping, with a reason.

Logging tables are recognised first, so that they do not count as business outputs: the
table is named like a log (log, audit, batch, run, error, history...), every value written
to it comes from variables, parameters, literals or system values, and nothing but its own
log statements reads it.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Set, Tuple

from .engine import Ctx
from .model import ROWS, VALUE
from .trace import backward

LOG_NAME = re.compile(r"(log|audit|trace|error|history|journal|event|batch|run|execution|monitor|progress|status)",
                      re.IGNORECASE)
DEBUG_FLAG = re.compile(r"@\w*(debug|verbose)\w*", re.IGNORECASE)
DEBUG_OFF = re.compile(r"(=\s*0\b|=\s*N?'N'|=\s*N?'NO'|\bIS\s+NULL\b|<\s*1\b)", re.IGNORECASE)
CONTROL = {"return", "throw", "break", "continue", "goto", "begin-tran", "commit", "rollback", "save-tran", "waitfor"}
DATA_KINDS = {"table", "view", "remote", "global-temp", "temp", "table-variable", "result", "cursor", "dynamic"}
ROW_COUNT = re.compile(r"@@ROWCOUNT|ROWCOUNT_BIG\s*\(|@@ERROR|@@IDENTITY|SCOPE_IDENTITY\s*\(", re.IGNORECASE)
CLOCK = re.compile(r"SYSDATETIME|SYSUTCDATETIME|GETDATE|GETUTCDATE|CURRENT_TIMESTAMP|SYSDATETIMEOFFSET",
                   re.IGNORECASE)
LOG_CALL = re.compile(r"(^|[._])(usp_|sp_)?(log|write_?log|log_?step|audit|trace)\w*$", re.IGNORECASE)
WRITE_OPS = {"insert", "update", "merge-insert", "merge-update", "merge-delete", "delete", "truncate", "select-into",
             "output-into", "default"}


def debug_only(st) -> bool:
    """Runs only when a debug flag is on (IF @Debug = 1 ...)."""
    for c in st.conditions:
        if not DEBUG_FLAG.search(c.text or ""):
            continue
        off = bool(DEBUG_OFF.search(c.text or ""))
        if (c.branch == "then" and not off) or (c.branch == "else" and off):
            return True
    return False


def logging_tables(ctx: Ctx) -> Set[str]:
    nodes_of: Dict[str, list] = defaultdict(list)
    for n in ctx.nodes:
        if n.step and n.op in WRITE_OPS:
            nodes_of[n.rel].append(n)
    out = set()
    for key, nodes in nodes_of.items():
        rel = ctx.relations.get(key)
        if rel is None or rel.kind not in ("table", "remote", "global-temp"):
            continue
        if not LOG_NAME.search(rel.name.split(".")[-1]):
            continue
        plain = True
        for n in nodes:
            for u in n.uses:
                if u.rel == key or u.rel.startswith("system:"):
                    continue
                r = ctx.relations.get(u.rel)
                if r is None or r.kind not in ("variable", "parameter", "return"):
                    plain = False
                    break
            if not plain:
                break
        if not plain:
            continue
        writers = {n.step for n in nodes}
        readers = {st.id for st in ctx.steps for u in st.uses if u.rel == key}
        readers |= {n.step for n in ctx.nodes for u in n.uses if u.rel == key and n.step}
        if readers - writers:
            continue
        out.add(key)
    return out


def classify(ctx: Ctx, relation_roles: Dict[str, str]) -> Tuple[Dict[str, Tuple[str, str]], Set[str]]:
    """{step id: ("logic" | "housekeeping", reason)} and the logging tables."""
    logging = logging_tables(ctx)
    steps = ctx.steps
    by_id = ctx.step_by_id

    def writes_of(st):
        return {ctx.nodes[n].rel for n in st.nodes if ctx.nodes[n].op not in ("local",)}

    # business outputs: what the procedure produces for someone else, minus logs and debug output
    business = set()
    for key, role in relation_roles.items():
        rel = ctx.relations.get(key)
        if role not in ("output", "both") or rel is None or key in logging or rel.kind == "return":
            continue
        if rel.kind == "result":
            writers = [by_id[n.step] for n in ctx.nodes if n.rel == key and n.step in by_id]
            if writers and all(debug_only(st) for st in writers):
                continue
        business.add(key)

    seeds = [n.id for n in ctx.nodes if n.step and n.rel in business]
    logic: Set[str] = set()
    for st in steps:
        rels = writes_of(st)
        data = {r for r in rels if r in business or (ctx.relations.get(r) is not None and
                ctx.relations[r].kind in ("temp", "table-variable", "cursor") and r not in logging)}
        if data and not debug_only(st):
            logic.add(st.id)                          # moves data through tables
        if st.kind in CONTROL or (st.kind == "raiserror" and (st.detail.get("severity") or 0) >= 11):
            logic.add(st.id)                          # changes what runs next
        if st.kind in ("exec", "insert-exec") and not ctx.expanded.get(st.id):
            callee = (st.detail.get("callee") or "").split(".")[-1]
            if not LOG_CALL.search(callee):
                logic.add(st.id)                      # a call whose effects are not known here

    # everything those statements depend on: values, rows and the branches around them
    for _ in range(3):
        starts = list(seeds)
        for sid in list(logic):
            st = by_id[sid]
            starts += [d for u in st.uses for d in u.defs]
            starts += [n for n in st.nodes]
            for c in st.conditions:
                cs = by_id.get(c.step) if c.step else None
                if cs is not None:
                    starts += [d for u in cs.uses for d in u.defs]
        sl = backward(ctx, sorted(set(starts)), indirect=True, limit=len(ctx.nodes) + 10)
        grown = set(logic)
        for nid in sl:
            if ctx.nodes[nid].step:
                grown.add(ctx.nodes[nid].step)
        for sid in list(grown):
            st = by_id.get(sid)
            if st is None:
                continue
            for c in st.conditions:
                if c.step:
                    grown.add(c.step)                 # the IF / WHILE that decides whether it runs
            if st.parent:
                grown.add(st.parent)                  # the EXEC that runs nested code
        if grown == logic:
            break
        logic = grown

    # nested code (dynamic SQL, expanded calls) ends with a marker step: logic if its body is
    for st in steps:
        if st.kind == "nested-end" and st.parent in logic and st.nodes:
            logic.add(st.id)

    # a DECLARE without a value only makes room for a variable: its first real value comes later
    for st in steps:
        if st.kind == "declare" and st.id in logic and all(ctx.nodes[n].expr in ("", "NULL") for n in st.nodes):
            logic.discard(st.id)

    roles: Dict[str, Tuple[str, str]] = {}
    readers_of: Dict[str, Set[str]] = defaultdict(set)
    for st in steps:
        for u in st.uses:
            readers_of[u.rel].add(st.id)
        for nid in st.nodes:
            for u in ctx.nodes[nid].uses:
                readers_of[u.rel].add(st.id)
    for st in steps:
        if st.id in logic:
            roles[st.id] = ("logic", "")
            continue
        roles[st.id] = ("housekeeping", _reason(ctx, st, logging, readers_of, logic))
    return roles, logging


def _reason(ctx: Ctx, st, logging: Set[str], readers_of, logic: Set[str]) -> str:
    k = st.kind
    if k == "nested-end":
        return "end of nested code"
    if debug_only(st):
        return "debug output"
    if k == "set-option" or k == "use":
        return "setting"
    if k in ("declare", "declare-table", "declare-cursor"):
        return "declaration"
    if k == "print" or k == "raiserror":
        return "message"
    if k in ("close-cursor", "deallocate-cursor", "drop-table"):
        return "cleanup"
    writes = {ctx.nodes[n].rel for n in st.nodes}
    if writes and writes <= logging:
        return "log entry"
    if k in ("exec", "insert-exec"):
        return "log entry"
    exprs = " ".join(ctx.nodes[n].expr or "" for n in st.nodes)
    if k in ("set", "select-assign", "fetch"):
        if ROW_COUNT.search(exprs):
            return "row count"
        if CLOCK.search(exprs) and not any(u.rel.startswith(("table:", "temp:", "tvar:"))
                                           for n in st.nodes for u in ctx.nodes[n].uses):
            return "timing"
        readers = set().union(*(readers_of.get(r, set()) for r in writes)) - {st.id} if writes else set()
        return "for logging and messages" if readers else "not used"
    if k in ("if", "while"):
        return "only housekeeping inside"
    return "not used by any output"


def housekeeping_summary(ctx: Ctx, st, reason: str, logging: Set[str]) -> str:
    """A shorter, plainer sentence for a folded statement, or '' to keep the usual one."""
    if reason == "row count" and st.kind in ("set", "select-assign"):
        targets = [ctx.relations[ctx.nodes[n].rel].name for n in st.nodes if ctx.nodes[n].rel in ctx.relations]
        exprs = " ".join(ctx.nodes[n].expr or "" for n in st.nodes).upper()
        what = "row count" + (" and error number" if "@@ERROR" in exprs else "")
        if "IDENTITY" in exprs:
            what = "new identity value"
        return f"Keeps the {what} in {', '.join(f'`{t}`' for t in targets[:3])}."
    if reason == "log entry" and st.nodes:
        rel = next((ctx.relations[ctx.nodes[n].rel] for n in st.nodes if ctx.nodes[n].rel in logging), None)
        if rel is None:
            return ""
        labels = [ctx.nodes[n].expr for n in st.nodes
                  if re.fullmatch(r"N?'(?:[^']|'')*'", (ctx.nodes[n].expr or "").strip())
                  and ctx.nodes[n].col != ROWS]
        label = labels[0].strip().lstrip("N").strip("'").replace("''", "'") if labels else ""
        if st.kind == "update":
            cols = [ctx.nodes[n].col for n in st.nodes if ctx.nodes[n].col not in (ROWS, VALUE)]
            return f"Updates the log row in `{rel.name}`" + (f" ({', '.join(cols[:3])})." if cols else ".")
        return f"Logs “{label}” to `{rel.name}`." if label and len(label) <= 60 else f"Writes a log row to `{rel.name}`."
    return ""
