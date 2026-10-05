"""Complexity figures for the Complexity view."""
from __future__ import annotations

from collections import Counter
from typing import Dict, List

from .engine import Ctx
from .syntax import typ, walk


def complexity(ctx: Ctx, unit, scopes) -> Dict:
    main = [s for s in ctx.steps if not s.parent and s.kind != "nested-end"]
    text = unit.text
    decisions = Counter()
    joins_max, joins_max_step = 0, None
    for st in main:
        if st.kind == "if":
            decisions["IF"] += 1
        elif st.kind == "while":
            decisions["WHILE"] += 1
        joins = 0
        for x in walk(st.node, stop=lambda y: typ(y) in ("IfStatement", "WhileStatement", "BeginEndBlockStatement",
                                                         "TryCatchStatement")):
            t = typ(x)
            if t in ("SearchedWhenClause", "SimpleWhenClause"):
                decisions["CASE WHEN"] += 1
            elif t == "IIfCall":
                decisions["IIF"] += 1
            elif t in ("QualifiedJoin", "UnqualifiedJoin"):
                joins += 1
        if joins > joins_max:
            joins_max, joins_max_step = joins, st.id
    decisions["CATCH"] = sum(1 for s in scopes.values() if s.kind == "catch")
    cyclomatic = 1 + sum(decisions.values())
    if unit.span:
        first, last = text.lines.lines(unit.span)
        body = text.text[unit.span[0]:unit.span[1]]
    else:
        first, last = 1, text.lines.count
        body = text.text
    code_lines = 0
    from .textutil import scan_tokens
    stripped = list(body)
    for kind, s, e in scan_tokens(body):
        if kind == "comment":
            for i in range(s, e):
                if stripped[i] not in "\r\n":
                    stripped[i] = " "
    for line in "".join(stripped).splitlines():
        if line.strip():
            code_lines += 1
    longest = sorted(main, key=lambda s: -((s.lines[1] - s.lines[0] + 1) if s.lines else 0))[:5]
    per_scope: List[dict] = []
    for sid, sc in scopes.items():
        steps = [x for k, x in sc.items if k == "step"]
        nested = [x for k, x in sc.items if k == "scope"]
        lines = [ctx.step_by_id[s].lines for s in steps if ctx.step_by_id[s].lines]
        span = (min(l[0] for l in lines), max(l[1] for l in lines)) if lines else None
        per_scope.append({"scope": sid, "kind": sc.kind, "label": sc.label, "steps": len(steps),
                          "scopes": len(nested), "lines": span})
    return {
        "statements": len(main),
        "nestedStatements": sum(1 for s in ctx.steps if s.parent and s.kind != "nested-end"),
        "lines": (last - first + 1) if first else 0,
        "codeLines": code_lines,
        "firstLine": first,
        "nesting": max((s.depth for s in ctx.steps), default=0),
        "cyclomatic": cyclomatic,
        "decisions": dict(decisions),
        "maxJoins": joins_max,
        "maxJoinsStep": joins_max_step,
        "longest": [{"step": s.id, "lines": (s.lines[1] - s.lines[0] + 1) if s.lines else 0} for s in longest],
        "scopes": per_scope,
        "kinds": dict(Counter(s.kind for s in main)),
    }
