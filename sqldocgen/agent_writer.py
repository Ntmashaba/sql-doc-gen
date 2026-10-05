"""Compact markdown for an LLM agent to reason about the procedure.

Claims and orientation come first, so a truncated read still orients correctly; then the
review issues, the steps, column lineage and, last, a trace table per output column.
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

from .view import ROWS, View, plain


def render_agent(payload: dict, max_trace_steps: int = 40) -> str:
    v = View(payload)
    p = payload["procedure"]
    out = []
    w = out.append
    w(f"# {payload['title']} — agent context (sql-doc-gen {payload['generatorVersion']})")
    w("")
    w("## Claims and limits (read first)")
    w(f"- Source: {p['source']} lines {p['lines'][0]}–{p['lines'][1]}; mode `{payload['mode']}`; parsed with Microsoft "
      f"ScriptDom {payload['parser'].get('scriptDom') or ''}.")
    w("- Static analysis only: no run history. Which branch ran, row counts and dynamic SQL values at run time are unknown.")
    w("- A column with several possible sources lists all of them. 'unresolved' / 'partial' mean unknown, not absent.")
    w("- Secrets in the code are masked with '*'.")
    for c in payload["coverage"]:
        if c["level"] in ("warn", "bad"):
            w(f"- Gap: {c['check']}: {c['result']} — {c['meaning']}")
    w("")
    w("## What it does")
    w(plain(payload["what"]))
    w("")
    if p["parameters"]:
        w("## Parameters")
        for prm in p["parameters"]:
            w(f"- `{prm['name']}` {prm['type']}" + (f" = {prm['default']}" if prm["default"] else "") +
              (" OUTPUT" if prm["output"] else "") + (" READONLY" if prm.get("readonly") else ""))
        w("")
    w("## Inputs")
    for r in payload["relations"]:
        if r["role"] in ("input", "both") and r["kind"] not in ("variable", "parameter"):
            cols = ", ".join(k for k in (r.get("colReads") or {}) if k != "value")
            w(f"- {r['name']} ({r['kind']}){': ' + cols if cols else ''}; read at steps "
              f"{' '.join(v.label(s) for s in r['reads'][:20])}")
    w("")
    w("## Outputs")
    for r in payload["relations"]:
        if r["role"] in ("output", "both"):
            w(f"- {r['name']} ({r['kind']}{', ' + '/'.join(r['ops']) if r['ops'] else ''}); written at steps "
              f"{' '.join(v.label(s) for s in r['writes'][:20])}")
    w("")
    if payload["issues"]:
        w("## Review issues (ranked)")
        for i in payload["issues"]:
            w(f"- [{i['severity']}{'/possible' if i['certainty'] == 'possible' else ''}] {plain(i['title'])} — "
              f"{i['next']} (steps {' '.join(v.label(s) for s in i['steps'][:8])})")
        w("")
    w("## Steps")
    w("| step | kind | lines | summary | conditions |")
    w("|---|---|---|---|---|")
    for s in payload["steps"]:
        if s["kind"] == "nested-end":
            continue
        cond = " AND ".join(("NOT " if c["branch"] == "else" else "") + c["text"] for c in s["conditions"])
        lines = f"{s['lines'][0]}-{s['lines'][1]}" if s["lines"] else ""
        w(f"| {s['label']} | {s['kind']} | {lines} | {plain(s['summary']).replace('|', '/')} | {cond.replace('|', '/')} |")
    w("")
    w("## Output column lineage")
    w("| column | status | computed from | steps | conditions |")
    w("|---|---|---|---|---|")
    for o in payload["outputs"]:
        if o["col"] == ROWS:
            continue
        cond = "; ".join(("NOT " if c["branch"] == "else" else "") + c["text"] for c in o["conditions"])
        w(f"| {v.col_name(o['rel'], o['col'])} | {o['status']} | "
          f"{', '.join(v.col_name(r, c) for r, c in o['direct']) or '(constants / row counts)'} | "
          f"{' '.join(v.label(s) for s in o['steps'])} | {cond.replace('|', '/')} |")
    w("")
    w("## Trace tables (backward, in execution order; value = writes the value, rows = decides which rows, cond = branch)")
    for o in payload["outputs"]:
        if o["col"] == ROWS:
            continue
        m = v.trace_model(o["key"], True)
        w(f"### {v.col_name(o['rel'], o['col'])}")
        w(f"computed from: {', '.join(m['bases']) or '(none)'}")
        if m["decided"]:
            w(f"decided by: {', '.join(m['decided'][:25])}{' …' if len(m['decided']) > 25 else ''}")
        for e in m["steps"][:max_trace_steps]:
            st = v.step[e["sid"]]
            writes = [v.nodes[n] for n, d in e["nodes"] if v.nodes[n]["col"] != ROWS and v.nodes[n]["op"] != "local"]
            ex = "; ".join(f"{v.node_label(n['id'])} = {n.get('expr', '')}" for n in writes[:3])
            w(f"- {st['label']} [{e['role']}] {plain(st['summary'])}" + (f" :: {ex}" if ex else ""))
        if len(m["steps"]) > max_trace_steps:
            w(f"- … {len(m['steps']) - max_trace_steps} more steps")
        w("")
    return "\n".join(out) + "\n"


def write_agent(payload: dict, path: Path) -> Tuple[Path, int]:
    text = render_agent(payload)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, len(text) // 4
