"""Plain-language sentences built only from facts the analysis found.

Names are wrapped in backticks; the page renders them as code, the Word and markdown writers
keep or strip them.
"""
from __future__ import annotations

from collections import Counter
from typing import Dict, List

from .engine import Ctx
from .model import ROWS, Step

OPS = {"insert": "INSERT", "update": "UPDATE", "merge-update": "MERGE", "merge-insert": "MERGE", "merge-delete": "MERGE",
       "delete": "DELETE", "truncate": "TRUNCATE", "select-into": "SELECT INTO", "output-into": "OUTPUT INTO",
       "create": "CREATE", "alter-add": "ALTER"}       # "default" (columns an INSERT leaves out) is part of the INSERT


def q(name: str) -> str:
    return f"`{name}`"


def _names(ctx: Ctx, keys: List[str], limit: int = 3) -> str:
    names = []
    for k in keys:
        rel = ctx.relations.get(k)
        names.append(q(rel.name if rel else k.split(":", 2)[-1].split(":")[-1]))
    names = list(dict.fromkeys(names))
    if len(names) > limit:
        return ", ".join(names[:limit]) + f" and {len(names) - limit} more"
    if len(names) > 1:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return names[0] if names else ""


def _cols(cols: List[str], limit: int = 4) -> str:
    cols = [c for c in cols if c and c != ROWS]
    if not cols:
        return ""
    if len(cols) > limit:
        return ", ".join(q(c) for c in cols[:limit]) + f" and {len(cols) - limit} more"
    return ", ".join(q(c) for c in cols)


def _sources(ctx: Ctx, d: dict) -> str:
    keys = d.get("source_keys") or []
    labels = d.get("sources") or []
    out = []
    for k, lab in zip(keys, labels):
        rel = ctx.relations.get(k)
        if rel is not None:
            out.append(q(rel.name))
        elif k.startswith("cte:"):
            out.append(q(lab) + " (CTE)")
        else:
            out.append(q(lab))
    out = list(dict.fromkeys(out))
    if not out:
        return ""
    if len(out) > 4:
        return ", ".join(out[:4]) + f" and {len(out) - 4} more"
    return " joined to ".join(out) if len(out) <= 2 else ", ".join(out[:-1]) + " and " + out[-1]


def summarize(ctx: Ctx, st: Step, dynamic: Dict[str, dict]) -> str:
    d = st.detail
    k = st.kind
    rel = ctx.relations.get(d.get("target") or "")
    tname = q(rel.name) if rel else (q(d["target"].split(":")[-1]) if d.get("target") else "")
    where = f" where {d['where']}" if d.get("where") else ""
    src = _sources(ctx, d)
    if k == "select-into":
        n = len([c for c in d.get("columns", []) if c != "*"])
        cols = f"{n} column{'s' if n != 1 else ''}" if n else "all columns of its source"
        return f"Creates {tname} ({cols}) from {src or 'an expression list'}{where}."
    if k == "select":
        return f"Returns {tname.replace('`', '') if rel else 'a result set'} ({_cols(d.get('columns', []), 5)}) from {src or 'expressions'}{where}."
    if k == "select-assign":
        return f"Sets {', '.join(q(v) for v in d.get('assigns', []))} from {src or 'an expression'}{where}."
    if k == "insert":
        what = f" ({_cols(d.get('columns', []))})" if d.get("columns") else ""
        if d.get("values_rows"):
            frm = f"{d['values_rows']} row{'s' if d['values_rows'] != 1 else ''} of values"
        elif d.get("default_values"):
            frm = "default values"
        else:
            frm = src or "a query"
        return f"Inserts into {tname}{what} from {frm}{where}."
    if k == "insert-exec":
        return f"Inserts the rows returned by {q(d.get('insert_exec', 'a procedure'))} into {tname}."
    if k == "update":
        cols = _cols(d.get("columns", []))
        rows = f"for rows where {d['where']}" if d.get("where") else ("for every row" if not d.get("joins") else "")
        joins = f" joined to {_sources(ctx, d)}" if d.get("joins") else ""
        return f"Updates {cols} in {tname}{joins} {rows}".rstrip() + "."
    if k == "delete":
        if d.get("all_rows"):
            return f"Deletes every row from {tname}."
        return f"Deletes rows from {tname}{where}."
    if k == "merge":
        parts = []
        for a in d.get("actions", []):
            cols = f" {_cols(a['columns'], 3)}" if a["columns"] else ""
            when = a["when"]
            head, _, cond = when.partition(" AND ")
            when = head.lower() + (f" and {cond}" if cond else "")
            parts.append(f"{a['action'].lower()}s{cols} {when}")
        on = f" on {d['on']}" if d.get("on") else ""
        return f"Merges {src or 'a source'} into {tname}{on}: " + "; ".join(parts) + "."
    if k == "truncate":
        return f"Empties {tname}."
    if k == "create-table":
        n = len(d.get("columns", []))
        return f"Creates {tname} ({n} column{'s' if n != 1 else ''})."
    if k == "drop-table":
        return f"Drops {_names(ctx, d.get('targets', []))}" + (" if it exists." if d.get("if_exists") else ".")
    if k == "alter-table":
        return f"Adds {_cols(d.get('columns', []))} to {tname}."
    if k == "declare":
        vs = d.get("variables", [])
        valued = [(ctx.relations[ctx.nodes[n].rel].name, ctx.nodes[n].expr) for n in st.nodes
                  if ctx.nodes[n].expr not in ("", "NULL") and ctx.nodes[n].rel in ctx.relations]
        if valued and len(vs) == 1:
            name, expr = valued[0]
            return f"Declares {q(name)} = {expr[:120]}."
        return f"Declares {', '.join(q(v) for v in vs[:6])}{' and more' if len(vs) > 6 else ''}."
    if k == "declare-table":
        n = len(d.get("columns", []))
        return f"Declares table variable {tname} ({n} column{'s' if n != 1 else ''})."
    if k == "declare-cursor":
        return f"Declares cursor {q(d.get('cursor', ''))}" + (f" over {src}" if src else "") + "."
    if k == "open-cursor":
        return f"Opens cursor {q(d.get('cursor', ''))}" + (f", which reads {src}{where}" if src else "") + "."
    if k == "fetch":
        vs = d.get("variables", [])
        return f"Fetches the next row of {q(d.get('cursor', ''))} into {', '.join(q(v) for v in vs)}."
    if k in ("close-cursor", "deallocate-cursor"):
        return ("Closes" if k == "close-cursor" else "Releases") + " the cursor."
    if k == "set":
        op = d.get("operator")
        if op == "+=" and d.get("text_variable"):
            return f"Appends {d.get('expression', '')} to {q(d.get('variable', ''))}."
        if op == "+=":
            return f"Adds {d.get('expression', '')} to {q(d.get('variable', ''))}."
        if op:
            return f"Sets {q(d.get('variable', ''))} {op} {d.get('expression', '')}."
        return f"Sets {q(d.get('variable', ''))} = {d.get('expression', '')}."
    if k == "if":
        return f"If {d.get('predicate', '')}"
    if k == "while":
        return f"Repeats while {d.get('predicate', '')}"
    if k == "exec":
        callee = d.get("callee", "a procedure")
        args = d.get("args", [])
        outs = [a["value"] for a in args if a.get("output")]
        tail = f", receiving {', '.join(q(o) for o in outs)}" if outs else ""
        exp = " (expanded below)" if ctx.expanded.get(st.id) else ""
        return f"Runs {q(callee)}{tail}{exp}."
    if k == "exec-dynamic":
        info = dynamic.get(st.label) or {}
        status = info.get("status", "unresolved")
        form = (d.get("dynamic") or {}).get("form", "EXEC")
        if status == "resolved":
            return f"Runs dynamic SQL ({form}), rebuilt from the code and analysed below."
        if status == "partial":
            return f"Runs dynamic SQL ({form}) that depends on {', '.join(q(p) for p in info.get('placeholders', [])[:3])}; its shape is analysed below."
        return f"Runs dynamic SQL ({form}) that cannot be rebuilt from the code."
    if k == "return":
        return f"Returns {d['value']}." if d.get("value") else "Returns."
    if k == "begin-tran":
        return "Begins a transaction."
    if k == "commit":
        return "Commits the transaction."
    if k == "rollback":
        return "Rolls back the transaction."
    if k == "save-tran":
        return "Sets a transaction savepoint."
    if k == "print":
        return "Prints a message."
    if k == "raiserror":
        sev = d.get("severity")
        return f"Raises an error{f' (severity {sev})' if sev is not None else ''}: {d.get('message', '')}."
    if k == "throw":
        return f"Throws: {d.get('message', '')}."
    if k == "nested-end":
        return f"End of {d.get('callee') or 'the nested code'}" + (": output parameters are copied back." if ctx.nested.get(st.parent, {}).get("outputs") else ".")
    if k == "break":
        return "Leaves the loop."
    if k == "continue":
        return "Starts the next loop iteration."
    if k == "goto":
        return "Jumps to a label."
    if k == "label":
        return "Label."
    if k == "use":
        return f"Switches to database {q(d.get('database', ''))}."
    if k == "bulk-insert":
        return f"Loads {tname} from file {q(d.get('file', ''))}."
    text = ctx.texts[st.text_id].squeeze(st.node, 100)
    return text or k


def overview(ctx: Ctx, roles: Dict[str, str], dynamic: Dict[str, dict], unit, logging=frozenset()) -> str:
    """'Reads 6 tables from `Sales` and `Ref`, stages through 3 temp tables, writes ... and returns 1 result set.'"""
    rels = ctx.relations
    inputs = [k for k, r in roles.items() if r in ("input", "both") and rels[k].kind in
              ("table", "view", "function", "remote", "system", "file")]
    groups = Counter()
    for k in inputs:
        r = rels[k]
        ep = r.endpoint or {}
        groups[ep.get("schema") or ep.get("server") or r.kind] += 1
    parts = []
    if inputs:
        kinds = Counter(rels[k].kind for k in inputs)
        noun = "table" if set(kinds) <= {"table"} else "object"
        g = [q(x) for x, _ in groups.most_common(3)]
        frm = (" from " + (", ".join(g[:-1]) + " and " + g[-1] if len(g) > 1 else g[0])) if g else ""
        parts.append(f"reads {len(inputs)} {noun}{'s' if len(inputs) != 1 else ''}{frm}")
    params = [r for r in rels.values() if r.kind == "parameter" and r.key.startswith("var:@")]
    temps = [r for k, r in rels.items() if r.kind in ("temp", "table-variable") and r.created and roles.get(k) == "intermediate"]
    if temps:
        nt = sum(1 for r in temps if r.kind == "temp")
        nv = len(temps) - nt
        bits = []
        if nt:
            bits.append(f"{nt} temp table{'s' if nt != 1 else ''}")
        if nv:
            bits.append(f"{nv} table variable{'s' if nv != 1 else ''}")
        parts.append("stages data through " + " and ".join(bits))
    ctes = sum(len(st.detail.get("ctes", [])) for st in ctx.steps)
    if ctes:
        parts.append(f"uses {ctes} CTE{'s' if ctes != 1 else ''}")
    writes = [k for k, r in roles.items() if r in ("output", "both") and k not in logging and
              rels[k].kind in ("table", "view", "remote", "global-temp", "temp", "dynamic")]
    if writes:
        ops_by: Dict[str, List[str]] = {}
        for n in ctx.nodes:
            if n.rel in writes and n.step:
                op = OPS.get(n.op)
                if op and op not in ("CREATE", "ALTER"):
                    ops_by.setdefault(n.rel, [])
                    if op not in ops_by[n.rel]:
                        ops_by[n.rel].append(op)
        ws = [f"{q(rels[k].name)} ({', '.join(ops_by.get(k, [])) or 'written'})" for k in writes[:4]]
        more = f" and {len(writes) - 4} more" if len(writes) > 4 else ""
        parts.append("writes " + (", ".join(ws[:-1]) + " and " + ws[-1] if len(ws) > 1 else ws[0]) + more)
    logs = [k for k in logging if k in rels]
    if logs:
        names = sorted(q(rels[k].name) for k in logs)
        parts.append("logs its progress to " + (", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]))
    results = [k for k, r in roles.items() if r == "output" and rels[k].kind == "result"]
    if results:
        parts.append(f"returns {len(results)} result set{'s' if len(results) != 1 else ''}")
    outp = [r for r in params if r.output]
    if outp:
        parts.append(f"sets output parameter{'s' if len(outp) != 1 else ''} {', '.join(q(r.name) for r in outp)}")
    calls = [c for c in ctx.calls if c["kind"] == "procedure"]
    if calls:
        names = list(dict.fromkeys(c["name"] for c in calls))
        parts.append(f"calls {', '.join(q(n) for n in names[:3])}{' and more' if len(names) > 3 else ''}")
    if dynamic:
        res = sum(1 for d in dynamic.values() if d["status"] == "resolved" and d["parsed"])
        parts.append(f"runs {len(dynamic)} dynamic SQL statement{'s' if len(dynamic) != 1 else ''}"
                     + (f" ({res} rebuilt)" if res else ""))
    branches = [st for st in ctx.steps if st.kind == "if"]
    loops = [st for st in ctx.steps if st.kind == "while"]
    if branches or loops:
        bits = []
        if branches:
            bits.append(f"{len(branches)} IF branch{'es' if len(branches) != 1 else ''}")
        if loops:
            bits.append(f"{len(loops)} loop{'s' if len(loops) != 1 else ''}")
        parts.append("has " + " and ".join(bits))
    if not parts:
        return "Has no statements that read or write data."
    sentence = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    return sentence[0].upper() + sentence[1:] + "."
