"""Assemble the JSON payload every output is rendered from.

The payload carries ``schemaVersion`` and a small ``summary`` block (counts, objects read and
written, issue counts) that the library home page reads, so the home keeps working as the
detailed fields grow. Positions are UTF-16 offsets into the embedded texts, which is what
the page's JavaScript indexes; line numbers are the file's own.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from . import GENERATOR, SCHEMA_VERSION, __version__
from .analyzer import Analysis
from .checks import Checker
from .describe import overview, summarize
from .engine import Ctx
from .metrics import complexity
from .model import ROWS, Use
from .program import ENTRY, EXIT
from .syntax import Text
from .textutil import extract_comments
from .trace import backward, base_sources, column_status, output_columns, output_relations

INPUT_KINDS = {"table", "view", "function", "system", "remote", "file", "table-parameter", "dynamic"}


class TextSlices:
    """Embedded texts: the procedure's own lines (with the file's line numbers) and nested code."""

    def __init__(self):
        self.texts: Dict[str, dict] = {}

    def add(self, tid: str, text: Text, span: Optional[Tuple[int, int]] = None, label: str = "", path: str = ""):
        if tid in self.texts:
            return
        if span:
            start = text.text.rfind("\n", 0, span[0]) + 1
            end = text.text.find("\n", span[1])
            end = len(text.text) if end < 0 else end
        else:
            start, end = 0, len(text.text)
        body = text.text[start:end]
        self.texts[tid] = {"id": tid, "label": label, "path": path, "text": body,
                           "lineOffset": text.lines.line(start) - 1,
                           "_u16start": text.umap.to_u16(start), "_text": text}

    def span(self, tid: str, sp) -> Optional[List[int]]:
        if not sp or tid not in self.texts:
            return None
        t = self.texts[tid]
        s, e = t["_text"].to_u16_span(sp)
        base = t["_u16start"]
        return [max(0, s - base), max(0, e - base)]

    def export(self) -> Dict[str, dict]:
        return {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in self.texts.items()}


def _use(u: Use, ts: TextSlices, tid: str) -> dict:
    d = {"rel": u.rel, "col": u.col, "role": u.role, "d": 1 if u.direct else 0, "defs": u.defs}
    sp = ts.span(tid, u.span)
    if sp:
        d["span"] = sp
    if u.status != "resolved":
        d["st"] = u.status
    if u.candidates:
        d["cand"] = u.candidates
    if u.text:
        d["t"] = u.text
    if u.local is not None:
        d["local"] = u.local
    return d


def relation_roles(ctx: Ctx, flow: dict) -> Dict[str, str]:
    written, read = set(), set()
    for st in ctx.steps:
        for nid in st.nodes:
            written.add(ctx.nodes[nid].rel)
        for u in st.uses:
            read.add(u.rel)
        read.update(st.reads)
    for n in ctx.nodes:
        for u in n.uses:
            read.add(u.rel)
    outputs = set(output_relations(ctx, flow))
    roles = {}
    for key, rel in ctx.relations.items():
        if key in outputs:
            roles[key] = "both" if (key in read and rel.kind in INPUT_KINDS) else "output"
        elif rel.kind in ("temp", "table-variable", "cursor", "global-temp") and (rel.created or key in written):
            roles[key] = "intermediate" if rel.created else ("both" if key in read else "output")
        elif rel.kind in ("temp",) and key in read:
            roles[key] = "input"          # read but created by the caller
        elif rel.kind == "parameter":
            roles[key] = "parameter"
        elif rel.kind == "variable":
            roles[key] = "variable"
        elif rel.kind in INPUT_KINDS and key in read:
            roles[key] = "input"
        elif rel.kind == "result":
            roles[key] = "internal"
        elif key in written or key in read:
            roles[key] = "intermediate"
    return roles


def _comments(text: Text, steps, tid: str) -> Dict[str, str]:
    """Comments directly above (or on the line of) each step."""
    comments = extract_comments(text.text)
    if not comments:
        return {}
    out: Dict[str, str] = {}
    ordered = sorted([s for s in steps if s.span and s.text_id == tid], key=lambda s: s.span[0])
    ci = 0
    prev_end = 0
    for st in ordered:
        acc = []
        while ci < len(comments) and comments[ci][0] < st.span[0]:
            s, e, body = comments[ci]
            if s >= prev_end and body and not set(body) <= set("-=*#/ "):
                acc.append(body)
            ci += 1
        if acc:
            out[st.id] = "\n".join(acc[-2:])[:400]
        prev_end = max(prev_end, st.span[1])
    return out


def cfg_edges(analysis: Analysis) -> List[list]:
    ctx, succ = analysis.ctx, analysis.builder.succ
    scopes = analysis.builder.scopes
    edges = []
    for a, bs in succ.items():
        for b in sorted(bs):
            kind = "next"
            sa, sb = ctx.step_by_id.get(a), ctx.step_by_id.get(b)
            if b == EXIT:
                kind = "exit"
            elif a == ENTRY:
                kind = "start"
            elif sa is not None and sa.kind == "if" and sb is not None:
                kind = "then" if any(c.step == a and c.branch == "then" for c in sb.conditions) else "else"
            elif sb is not None and sb.kind == "while" and sa is not None and \
                    any(c.step == b and c.branch == "loop" for c in sa.conditions):
                kind = "back"
            elif sa is not None and sb is not None and scopes[sb.scope].kind == "catch" and \
                    not any(c.branch == "catch" for c in sa.conditions[len(sb.conditions) - 1:]) and \
                    scopes[sa.scope].kind != "catch" and _in_try(scopes, sa.scope):
                kind = "error"
            elif sa is not None and sa.kind == "while":
                kind = "loop" if sb is not None and any(c.step == a for c in sb.conditions) else "done"
            edges.append([a, b, kind])
    return edges


def _in_try(scopes, sid) -> bool:
    while sid:
        if scopes[sid].kind == "try":
            return True
        sid = scopes[sid].parent
    return False


def build(analysis: Analysis, *, title: str, mode: str, source_path: str, inputs_meta: dict,
          callers: List[dict], secrets: int, parse_errors: List[dict], catalog_sources: List[str],
          catalog_size: int) -> dict:
    ctx, flow, unit = analysis.ctx, analysis.flow, analysis.unit
    ts = TextSlices()
    ts.add("main", unit.text, unit.span, unit.display, source_path)
    for tid, text in ctx.texts.items():
        if tid == "main":
            continue
        if tid.startswith("dyn:"):
            ts.add(tid, text, None, f"Dynamic SQL rebuilt for step {tid.split(':')[1]}")
        else:
            obj = None
            for o in ctx.catalog.objects:
                if f"{o.kind if o.kind != 'procedure' else 'proc'}:{o.full.lower()}" == tid or \
                        tid.endswith(":" + o.full.lower()):
                    obj = o
                    break
            span = text.span(obj.node) if obj is not None and obj.node is not None else None
            ts.add(tid, text, span, obj.display if obj else tid, obj.source if obj else "")
    roles = relation_roles(ctx, flow)
    for st in ctx.steps:
        st.summary = summarize(ctx, st, analysis.dynamic)
    comments = {}
    for tid in ctx.texts:
        comments.update(_comments(ctx.texts[tid], ctx.steps, tid))
    issues = Checker(ctx, flow, analysis.dynamic, analysis.builder.succ).run()
    step_issues: Dict[str, List[int]] = defaultdict(list)
    for i, iss in enumerate(issues):
        for s in iss.steps:
            step_issues[s].append(i)

    # ------------------------------------------------------------------ relations
    reads_by_rel: Dict[str, List[str]] = defaultdict(list)
    writes_by_rel: Dict[str, List[str]] = defaultdict(list)
    cols_read: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for st in ctx.steps:
        for k in st.reads:
            reads_by_rel[k].append(st.id)
        for u in st.uses:
            if u.col not in (ROWS,):
                cols_read[u.rel][u.col].append(st.id)
            if u.rel not in st.reads:
                reads_by_rel[u.rel].append(st.id)
        for k in st.writes:
            writes_by_rel[k].append(st.id)
    for n in ctx.nodes:
        if n.step is None:
            continue
        for u in n.uses:
            if u.local is None:
                if u.col != ROWS:
                    cols_read[u.rel][u.col].append(n.step)
                reads_by_rel[u.rel].append(n.step)
    ops_by_rel: Dict[str, List[str]] = defaultdict(list)
    from .describe import OPS
    for n in ctx.nodes:
        if n.step and n.op in OPS:
            op = OPS[n.op]
            if op not in ops_by_rel[n.rel]:
                ops_by_rel[n.rel].append(op)
    for st in ctx.steps:
        if st.kind == "drop-table":
            for k in st.detail.get("targets", []):
                if "DROP" not in ops_by_rel[k]:
                    ops_by_rel[k].append("DROP")
    relations = []
    for key, rel in ctx.relations.items():
        if key.startswith("unresolved:") or roles.get(key) is None and not reads_by_rel.get(key) and not writes_by_rel.get(key):
            continue
        cat = rel.catalog
        relations.append({
            "key": key, "kind": rel.kind, "name": rel.name, "role": roles.get(key, "internal"),
            "columns": [{"name": c.name, "type": c.type, "nullable": c.nullable, "identity": c.identity or None,
                         "computed": c.computed or None, "default": c.default or None} for c in rel.columns],
            "complete": rel.complete, "endpoint": rel.endpoint, "note": rel.note,
            "created": rel.created, "dropped": rel.dropped,
            "reads": sorted(set(reads_by_rel.get(key, [])), key=_order(ctx)),
            "writes": sorted(set(writes_by_rel.get(key, [])), key=_order(ctx)),
            "ops": ops_by_rel.get(key, []),
            "colReads": {c: sorted(set(v), key=_order(ctx)) for c, v in cols_read.get(key, {}).items()},
            "defined": bool(cat is not None and cat.columns) or bool(rel.created),
            "definedIn": cat.source if cat is not None else "",
            "keys": (cat.keys if cat is not None else []) + ctx.keys.get(key, []),
            "dataType": rel.data_type or None, "output": rel.output or None, "default": rel.default or None,
            "starFrom": rel.star_from or None,
        })

    # ------------------------------------------------------------------ steps, scopes, nodes
    steps = []
    reachable = flow["reachable"]
    for st in ctx.steps:
        d = st.detail
        steps.append({
            "id": st.id, "label": st.label, "kind": st.kind, "text": st.text_id,
            "span": ts.span(st.text_id, st.span), "lines": list(st.lines) if st.lines else None,
            "head": _head_span(ts, ctx, st),
            "scope": st.scope, "depth": st.depth,
            "conditions": [{"step": c.step, "text": c.text, "branch": c.branch} for c in st.conditions],
            "reads": st.reads, "writes": st.writes, "summary": st.summary, "comment": comments.get(st.id, ""),
            "nodes": st.nodes, "uses": [_use(u, ts, st.text_id) for u in st.uses],
            "origin": st.origin or None, "parent": st.parent, "issues": step_issues.get(st.id, []),
            "reachable": st.id in reachable or st.kind == "nested-end",
            "inTry": st.in_try or None,
            "detail": {k: d[k] for k in ("where", "on", "joins", "columns", "callee", "args", "actions", "predicate",
                                         "cursor", "variables", "variable", "expression", "dynamic", "partial",
                                         "all_rows", "top", "distinct", "group_by", "options", "on", "isolation",
                                         "message", "severity", "insert_exec", "column_list", "file", "ctes",
                                         "targets", "output_into", "output_result", "system")
                       if k in d and d[k] not in (None, "", [], {})},
        })
        if "dynamic" in steps[-1]["detail"]:
            steps[-1]["detail"]["dynamic"] = {k: v for k, v in steps[-1]["detail"]["dynamic"].items()}
    scopes = [{"id": s.id, "kind": s.kind, "label": s.label, "parent": s.parent, "step": s.step,
               "items": [list(x) for x in s.items]} for s in analysis.builder.scopes.values()]
    nodes = []
    for n in ctx.nodes:
        nd = {"id": n.id, "rel": n.rel, "col": n.col, "step": n.step, "op": n.op}
        if n.expr:
            nd["expr"] = n.expr
        sp = ts.span(n.text_id, n.span)
        if sp:
            nd["span"] = sp
        if n.text_id != "main":
            nd["text"] = n.text_id
        if n.keeps_previous:
            nd["keeps"] = 1
        if n.kills:
            nd["kills"] = 1
        if n.status != "resolved":
            nd["st"] = n.status
        if n.note:
            nd["note"] = n.note
        if n.transforms:
            nd["tf"] = n.transforms
        if n.uses:
            nd["uses"] = [_use(u, ts, n.text_id) for u in n.uses]
        nodes.append(nd)

    # ------------------------------------------------------------------ output columns
    outs = []
    matrix_rels: Dict[str, None] = {}
    for oc in output_columns(ctx, flow):
        sl = backward(ctx, oc["defs"])
        direct_src = base_sources(ctx, sl, True)
        all_src = base_sources(ctx, sl, False)
        indirect_rels = sorted({ctx.nodes[n].rel for n, i in sl.items()
                                if ctx.nodes[n].op == "initial" and not i["direct"]})
        dsteps = sorted({ctx.nodes[n].step for n, i in sl.items() if i["direct"] and ctx.nodes[n].step},
                        key=_order(ctx))
        isteps = sorted({ctx.nodes[n].step for n, i in sl.items() if not i["direct"] and ctx.nodes[n].step}
                        - set(dsteps), key=_order(ctx))
        conds = []
        for s in dsteps:
            for c in ctx.step_by_id[s].conditions:
                if c.step and (c.step, c.branch) not in [(x["step"], x["branch"]) for x in conds]:
                    conds.append({"step": c.step, "text": c.text, "branch": c.branch})
        cells = {}
        for r, c in all_src:
            cells.setdefault(r, set()).add("I")
        for n, i in sl.items():
            nd = ctx.nodes[n]
            if nd.op == "initial":
                cells.setdefault(nd.rel, set()).add("D" if i["direct"] and nd.col != ROWS else "I")
        for r in cells:
            if roles.get(r) in ("input", "both", "parameter") or (ctx.relations.get(r) and ctx.relations[r].kind in INPUT_KINDS):
                matrix_rels[r] = None
        outs.append({
            "key": oc["key"], "rel": oc["rel"], "col": oc["col"], "defs": oc["defs"],
            "status": column_status(ctx, sl), "direct": [list(x) for x in direct_src],
            "indirect": indirect_rels, "steps": dsteps, "indirectCount": len(isteps), "conditions": conds,
            "matrix": {r: "".join(sorted(v)) for r, v in cells.items()},
            "slice": len(sl),
        })

    # ------------------------------------------------------------------ statement-local relations
    locals_ = []
    cols_of: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for n in ctx.nodes:
        if n.op == "local" and n.col != ROWS:
            cols_of[n.rel].append((n.col, n.id))
    for key, info in ctx.locals.items():
        if info["kind"] in ("derived",) and not cols_of.get(key):
            continue
        locals_.append({"key": key, "kind": info["kind"], "name": info["name"], "step": info["step"],
                        "text": info["text"], "columns": [{"name": c, "node": i} for c, i in cols_of.get(key, [])]})

    # ------------------------------------------------------------------ transformations
    transforms = []
    for n in ctx.nodes:
        if not n.step or n.col == ROWS:
            continue
        kinds = [t for t in n.transforms if t != "constant"]
        if not kinds:
            continue
        transforms.append({"node": n.id, "step": n.step, "rel": n.rel, "col": n.col, "kinds": kinds})

    # ------------------------------------------------------------------ calls
    calls = []
    by_name: Dict[str, dict] = {}
    for c in ctx.calls:
        name = c.get("name", "")
        entry = by_name.get(name.lower())
        if entry is None:
            entry = {"name": name, "kind": c["kind"], "steps": [], "supplied": c.get("supplied", False),
                     "expanded": False, "system": c.get("system", False), "linkedServer": c.get("linkedServer"),
                     "dynamicName": c.get("dynamicName", False), "insertExec": False}
            by_name[name.lower()] = entry
            calls.append(entry)
        entry["steps"].append(c["step"])
        entry["expanded"] = entry["expanded"] or bool(ctx.expanded.get(c["step"]))
        entry["insertExec"] = entry["insertExec"] or c.get("insertExec", False)
    for f in ctx.functions.values():
        calls.append({"name": f["name"], "kind": "function", "steps": sorted(set(f["steps"]), key=_order(ctx)),
                      "supplied": f.get("supplied", False), "expanded": f["name"].lower() in ctx.expanded_objects,
                      "table": f.get("table", False)})
    for k, v in ctx.expanded_objects.items():
        if v["kind"] == "view":
            calls.append({"name": v["name"], "kind": "view", "steps": sorted(set(v["steps"]), key=_order(ctx)),
                          "supplied": True, "expanded": True})
    for key, rel in ctx.relations.items():
        if rel.kind == "view" and not any(c["kind"] == "view" and c["name"] == rel.name for c in calls):
            calls.append({"name": rel.name, "kind": "view", "steps": sorted(set(reads_by_rel.get(key, [])), key=_order(ctx)),
                          "supplied": rel.catalog is not None, "expanded": False})

    # ------------------------------------------------------------------ coverage
    dyn = analysis.dynamic
    dyn_ok = sum(1 for d in dyn.values() if d["status"] == "resolved" and d["parsed"])
    dyn_part = sum(1 for d in dyn.values() if d["status"] == "partial" and d["parsed"])
    dyn_bad = len(dyn) - dyn_ok - dyn_part
    unresolved_cols = sorted({u["text"] for u in ctx.unresolved})
    not_supplied = [c for c in calls if c["kind"] in ("procedure", "function") and not c["supplied"]
                    and not c.get("system")]
    branches = sum(1 for st in ctx.steps if st.kind in ("if", "while"))
    partial_cols = sum(1 for o in outs if o["status"] != "resolved" and o["col"] != ROWS)
    coverage = [
        {"check": "Mode", "result": mode, "level": "info",
         "meaning": {"procedure": "Procedure text only: columns are tied to tables by name; SELECT * and unqualified "
                                  "columns may stay Partial.",
                     "schema": "Procedure text plus table and view definitions: columns resolve fully.",
                     "project": "A folder or project: called procedures, views and functions supplied with it are "
                                "expanded, not opaque."}[mode]},
        {"check": "Statements parsed", "result": str(len([s for s in ctx.steps if s.kind != 'nested-end'])),
         "level": "ok", "meaning": f"Parsed by Microsoft ScriptDom {inputs_meta.get('scriptDom') or ''} "
                                   f"({inputs_meta.get('parser') or 'TSql parser'})."},
        {"check": "Parse errors", "result": str(len(parse_errors)), "level": "bad" if parse_errors else "ok",
         "meaning": "; ".join(f"line {e.get('line')}: {e.get('message')}" for e in parse_errors[:3])
                    if parse_errors else "The whole file parsed."},
        {"check": "Unresolved columns", "result": str(len(unresolved_cols)), "level": "warn" if unresolved_cols else "ok",
         "meaning": ("Not tied to a table: " + ", ".join(unresolved_cols[:6]) + ("…" if len(unresolved_cols) > 6 else ""))
         if unresolved_cols else "Every column reference was tied to a table, variable or CTE."},
        {"check": "Output columns not fully resolved", "result": str(partial_cols), "level": "warn" if partial_cols else "ok",
         "meaning": "Partial or Unresolved: some of their sources are unknown, not absent." if partial_cols
         else "Every output column traces back to known sources."},
        {"check": "Dynamic SQL", "result": f"{dyn_ok} rebuilt · {dyn_part} partial · {dyn_bad} unreadable" if dyn else "none",
         "level": "bad" if dyn_bad else ("warn" if dyn_part else "ok"),
         "meaning": "Rebuilt from the code that assembles the string and analysed in place; unreadable dynamic SQL is "
                    "opaque: what it reads and writes is unknown." if dyn else "No dynamic SQL."},
        {"check": "Calls to objects not supplied", "result": str(len(not_supplied)),
         "level": "warn" if not_supplied else "ok",
         "meaning": ("Opaque: " + ", ".join(c["name"] for c in not_supplied[:5]) +
                     ". Supply them (project mode) to expand them.") if not_supplied
         else "Every procedure and function it calls was supplied (or it calls none)."},
        {"check": "SELECT *", "result": f"{ctx.star_expanded} expanded · {ctx.star_unexpanded} not expanded",
         "level": "warn" if ctx.star_unexpanded else "ok",
         "meaning": "A * whose table definition is unknown is shown as 'all columns'." if ctx.star_unexpanded
         else ("Every * was expanded from a known definition." if ctx.star_expanded else "No SELECT *.")},
        {"check": "Branches and loops", "result": str(branches), "level": "info",
         "meaning": "Static analysis cannot know which branch ran: a column written in several branches lists every "
                    "possible source with its condition."},
        {"check": "Table definitions", "result": str(catalog_size), "level": "info",
         "meaning": ("From " + ", ".join(catalog_sources)) if catalog_sources else
         "No definitions supplied: columns are learned from how the procedure uses them."},
        {"check": "Secrets withheld", "result": str(secrets), "level": "info",
         "meaning": "Passwords, keys and tokens in connection strings and options are masked with * in place."},
        {"check": "Run history", "result": "not available", "level": "info",
         "meaning": "Generated from code, not from executions: nothing here says what ran, how often, or with which values."},
    ]

    # ------------------------------------------------------------------ summary for the home page
    counts = {
        "statements": len([s for s in ctx.steps if not s.parent and s.kind != "nested-end"]),
        "inputs": sum(1 for r in relations if r["role"] in ("input", "both") and r["kind"] in INPUT_KINDS),
        "outputs": sum(1 for r in relations if r["role"] in ("output", "both")),
        "temp": sum(1 for r in relations if r["role"] == "intermediate" and r["kind"] in ("temp", "table-variable")),
        "ctes": sum(len(st.detail.get("ctes", [])) for st in ctx.steps),
        "branches": branches,
        "dynamic": len(dyn),
        "variables": sum(1 for r in relations if r["kind"] == "variable"),
        "parameters": len(unit.params),
        "outputColumns": sum(1 for o in outs if o["col"] != ROWS),
        "calls": sum(1 for c in calls if c["kind"] == "procedure"),
    }
    issue_counts = Counter(i.severity for i in issues)
    sources = defaultdict(lambda: {"objects": set(), "read": 0, "written": 0})
    for r in relations:
        if r["kind"] not in ("table", "view", "remote", "function", "system", "file") and not (
                r["kind"] == "global-temp"):
            continue
        ep = r.get("endpoint") or {}
        sk = (ep.get("system") or "SQL Server", ep.get("server") or "", ep.get("database") or "")
        s = sources[sk]
        s["objects"].add(r["name"])
        if r["role"] in ("input", "both"):
            s["read"] += 1
        if r["role"] in ("output", "both"):
            s["written"] += 1
    summary = {
        "counts": counts,
        "issues": {k: issue_counts.get(k, 0) for k in ("high", "medium", "low", "info")},
        "unresolved": len(unresolved_cols),
        "objects": [{"name": r["name"], "kind": r["kind"], "endpoint": r.get("endpoint"),
                     "read": r["role"] in ("input", "both"), "written": r["role"] in ("output", "both"),
                     "ops": r["ops"]} for r in relations
                    if r["kind"] in ("table", "view", "remote", "function", "file", "global-temp")],
        "sources": [{"system": k[0], "server": k[1], "database": k[2], "objects": sorted(v["objects"]),
                     "read": v["read"], "written": v["written"]} for k, v in sources.items()],
        "calls": [c["name"] for c in calls if c["kind"] == "procedure"],
    }

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generator": GENERATOR,
        "generatorVersion": __version__,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "title": title,
        "mode": mode,
        "procedure": {
            "name": unit.display, "schema": unit.schema, "database": unit.database or ctx.default_db or None,
            "kind": unit.kind, "source": source_path, "parameters": unit.params, "options": unit.options,
            "returnCodes": [st.detail.get("value") for st in ctx.steps if st.kind == "return" and st.detail.get("value")
                            and not st.parent],
            "lines": list(unit.text.lines.lines(unit.span)) if unit.span else [1, unit.text.lines.count],
            "endpoint": ctx.endpoint(None, unit.database or ctx.default_db or None, unit.schema or None, unit.name),
        },
        "parser": {"scriptDom": inputs_meta.get("scriptDom"), "parser": inputs_meta.get("parser"),
                   "helper": inputs_meta.get("helperVersion"), "passes": analysis.passes},
        "summary": summary,
        "counts": counts,
        "coverage": coverage,
        "what": overview(ctx, roles, dyn, unit),
        "texts": ts.export(),
        "relations": relations,
        "steps": steps,
        "scopes": scopes,
        "cfg": cfg_edges(analysis),
        "nodes": nodes,
        "outputs": outs,
        "matrixRelations": list(matrix_rels),
        "locals": locals_,
        "transforms": transforms,
        "calls": calls,
        "callers": callers,
        "dynamic": [{"step": k, **{kk: vv for kk, vv in v.items() if kk != "namespace"}} for k, v in dyn.items()],
        "issues": [{"id": i, "rule": x.rule, "severity": x.severity, "title": x.title, "why": x.why,
                    "next": x.next_step, "steps": x.steps, "certainty": x.certainty, "columns": x.columns,
                    "relations": x.relations, "text": x.text_id,
                    "spans": [s for s in (ts.span(x.text_id, sp) for sp in x.spans) if s]}
                   for i, x in enumerate(issues)],
        "complexity": complexity(ctx, unit, analysis.builder.scopes),
        "redactions": secrets,
    }


def _head_span(ts: TextSlices, ctx: Ctx, st) -> Optional[List[int]]:
    """IF / WHILE: from the keyword to the end of the predicate (their span covers the whole block)."""
    if st.kind not in ("if", "while") or not st.span:
        return None
    pred = ctx.texts[st.text_id].span(st.node.get("Predicate"))
    if not pred:
        return None
    return ts.span(st.text_id, (st.span[0], pred[1]))


def _order(ctx: Ctx):
    idx = {s.id: i for i, s in enumerate(ctx.steps)}
    return lambda sid: idx.get(sid, 10 ** 6)
