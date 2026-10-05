"""Export one column's trace as a standalone HTML page and a markdown checklist, to attach to a
bug ticket. The content matches the page's Column trace view."""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import List, Optional

from .view import ROWS, View, plain

ROLE = {"value": "writes the value", "rows": "decides which rows", "cond": "decides the branch"}
_KW = re.compile(r"\b(SELECT|FROM|WHERE|JOIN|INNER|LEFT|RIGHT|FULL|OUTER|CROSS|APPLY|ON|AND|OR|NOT|IN|EXISTS|INSERT|INTO|"
                 r"VALUES|UPDATE|SET|DELETE|MERGE|USING|WHEN|MATCHED|THEN|ELSE|END|CASE|AS|IS|NULL|BEGIN|IF|WHILE|"
                 r"RETURN|DECLARE|CREATE|TABLE|DROP|TRUNCATE|EXEC|EXECUTE|WITH|GROUP|BY|ORDER|HAVING|TOP|DISTINCT|UNION|"
                 r"ALL|OUTPUT|TRY|CATCH|TRANSACTION|TRAN|COMMIT|ROLLBACK|OVER|PARTITION)\b", re.I)


def find_output(payload: dict, want: str) -> Optional[str]:
    return View(payload).find_output(want)


def _lines_of(text: str):
    out, s = [], 0
    for i, ch in enumerate(text + "\n"):
        if i == len(text) or ch == "\n":
            e = i - 1 if i > s and text[i - 1] == "\r" else i
            out.append((s, e))
            s = i + 1
    return out


def _excerpt(payload: dict, tid: str, span, marks) -> str:
    t = payload["texts"].get(tid)
    if not t or not span:
        return ""
    text, off = t["text"], t.get("lineOffset", 0)
    L = _lines_of(text)
    first = next(i for i, (s, e) in enumerate(L) if e >= span[0] or i == len(L) - 1)
    last = next((i for i, (s, e) in enumerate(L) if e >= span[1] - 1), len(L) - 1)
    last = min(last, first + 40)
    rows = []
    for i in range(first, last + 1):
        s, e = L[i]
        pts = sorted({s, e} | {max(s, m[0]) for m in marks if m[1] > s and m[0] < e} |
                     {min(e, m[1]) for m in marks if m[1] > s and m[0] < e})
        body = ""
        for a, b in zip(pts, pts[1:]):
            if b <= a:
                continue
            cls = " ".join(m[2] for m in marks if m[0] < b and m[1] > a)
            seg = html.escape(text[a:b])
            seg = _KW.sub(lambda m: f'<span class="kw">{m.group(0)}</span>', seg) if not cls else seg
            body += f'<span class="{cls}">{seg}</span>' if cls else seg
        rows.append(f'<div class="cl"><span class="ln">{i + 1 + off}</span><span>{body or " "}</span></div>')
    return f'<div class="code">{"".join(rows)}</div>'


CSS = """body{font-family:"Segoe UI",system-ui,sans-serif;background:#F5F7FB;color:#0F172A;margin:0;font-size:14px;line-height:1.5}
main{max-width:1100px;margin:auto;padding:24px}h1{font-size:1.5rem;margin:.2rem 0}.mut{color:#334155;font-size:.85rem}
.card{background:#fff;border:1px solid #CBD5E1;border-radius:12px;padding:14px 16px;margin:0 0 12px}
.role{font-family:Consolas,monospace;font-size:.68rem;text-transform:uppercase;padding:.1rem .45rem;border-radius:4px;background:#E2E8F0}
.role.value{background:#4F46E5;color:#fff}.role.rows{background:#CCFBF1;color:#0F766E}
code,.eq{font-family:Consolas,monospace;font-size:.85em;background:#F1F4F9;border:1px solid #E2E8F0;border-radius:4px;padding:0 .3em}
.code{font-family:Consolas,monospace;font-size:.76rem;background:#0F172A;color:#E2E8F0;border-radius:8px;padding:.5rem 0;margin:.5rem 0;overflow-x:auto}
.cl{display:flex;white-space:pre-wrap}.ln{color:#64748B;min-width:3.5rem;text-align:right;padding-right:.7rem;margin-right:.6rem;border-right:1px solid #1E293B}
.kw{color:#93C5FD;font-weight:600}.m-w{background:#78350F;box-shadow:0 0 0 1px #F59E0B}.m-d{background:#3730A3;box-shadow:0 0 0 1px #818CF8}
.m-i{background:#134E4A;box-shadow:0 0 0 1px #2DD4BF}.badge{font-family:Consolas,monospace;font-size:.68rem;padding:.08rem .45rem;border-radius:99px;background:#FEF3C7;color:#B45309}
.legend span{margin-right:1rem}.sw{display:inline-block;width:14px;height:10px;border-radius:3px;margin-right:.3rem;vertical-align:middle}"""


def render_trace_html(payload: dict, key: str) -> str:
    v = View(payload)
    m = v.trace_model(key, True)
    o = m["output"]
    name = v.col_name(o["rel"], o["col"])
    e = html.escape
    parts = [f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>Trace: {e(name)}</title><style>{CSS}</style></head><body><main>",
             f"<div class='mut'>COLUMN TRACE · {e(payload['title'])} · generated {e(payload['generated'])} by sql-doc-gen</div>",
             f"<h1>{e(name)}</h1><p class='mut'>Status: <b>{e(o['status'])}</b> · static analysis: every step that can affect this "
             f"column, in execution order. Unknown is not absent.</p>",
             f"<div class='card'><b>Computed from:</b> {e(', '.join(m['bases']) or 'no input column (constants, system values or row counts)')}"
             + (f"<br><b>Decided by:</b> {e(', '.join(m['decided'][:40]))}" if m["decided"] else "") + "</div>",
             "<p class='legend mut'><span><i class='sw m-w'></i>value written</span><span><i class='sw m-d'></i>computed from</span>"
             "<span><i class='sw m-i'></i>joins, filters, conditions</span></p>"]
    for ent in m["steps"]:
        st = v.step[ent["sid"]]
        marks = []
        body = []
        for nid, direct in ent["nodes"]:
            n = v.nodes[nid]
            if n["col"] == ROWS or n["op"] == "local":
                continue
            if n.get("span") and not n.get("text"):
                marks.append((n["span"][0], n["span"][1], "m-w" if direct else "m-i"))
            for u in n.get("uses", []):
                if u.get("span") and not n.get("text"):
                    marks.append((u["span"][0], u["span"][1], "m-d" if u["d"] else "m-i"))
            frm = ", ".join(f"{v.col_name(u['rel'], u['col'])} ({', '.join(v.version(d) for d in u['defs']) or 'no earlier value'})"
                            for u in n.get("uses", []) if u["d"])
            body.append(f"<div>Writes <b>{e(v.node_label(nid))}</b> = <span class='eq'>{e(n.get('expr', ''))}</span>"
                        + (" <span class='badge'>only some rows</span>" if n.get("keeps") else "")
                        + (f"<div class='mut'>from {e(frm)}</div>" if frm else "") + "</div>")
        for u in st["uses"]:
            if u.get("span"):
                marks.append((u["span"][0], u["span"][1], "m-i"))
        d = st.get("detail", {})
        if d.get("where"):
            body.append(f"<div class='mut'>rows where <code>{e(d['where'])}</code></div>")
        if d.get("on"):
            body.append(f"<div class='mut'>matched on <code>{e(d['on'])}</code></div>")
        for c in st["conditions"]:
            body.append(f"<div class='mut'>only when {'NOT ' if c['branch'] == 'else' else ''}<code>{e(c['text'])}</code></div>")
        for i in st.get("issues", []):
            iss = payload["issues"][i]
            body.append(f"<div class='mut'>⚠ <b>{e(iss['severity'])}</b>: {e(plain(iss['title']))} — {e(iss['next'])}</div>")
        span = st.get("head") or st.get("span")
        parts.append(f"<div class='card'><div><b>Step {e(st['label'])}</b> <span class='role {ent['role']}'>{ROLE[ent['role']]}</span> "
                     f"<span class='mut'>{e(v.lines(st))}</span></div><div>{e(plain(st['summary']))}</div>{''.join(body)}"
                     f"{_excerpt(payload, st['text'], span, marks)}</div>")
    parts.append("</main></body></html>")
    return "".join(parts)


def render_trace_md(payload: dict, key: str) -> str:
    v = View(payload)
    m = v.trace_model(key, True)
    o = m["output"]
    out = [f"# Column trace: {v.col_name(o['rel'], o['col'])}", "",
           f"{payload['title']} · generated {payload['generated']} by sql-doc-gen · status: {o['status']}", "",
           f"Computed from: {', '.join(m['bases']) or 'no input column'}", ""]
    if m["decided"]:
        out += [f"Decided by: {', '.join(m['decided'][:40])}", ""]
    out += ["## Checklist", ""]
    for ent in m["steps"]:
        st = v.step[ent["sid"]]
        out.append(f"- [ ] **Step {st['label']}** ({v.lines(st)}) — {ROLE[ent['role']]}: {plain(st['summary'])}")
        for nid, direct in ent["nodes"]:
            n = v.nodes[nid]
            if n["col"] != ROWS and n["op"] != "local":
                out.append(f"  - {v.node_label(nid)} = `{' '.join((n.get('expr') or '').split())}`"
                           + (" (only some rows)" if n.get("keeps") else ""))
        d = st.get("detail", {})
        if d.get("where"):
            out.append(f"  - rows where `{d['where']}`")
        if d.get("on"):
            out.append(f"  - matched on `{d['on']}`")
        for c in st["conditions"]:
            out.append(f"  - only when {'NOT ' if c['branch'] == 'else' else ''}`{c['text']}`")
        for i in st.get("issues", []):
            iss = payload["issues"][i]
            out.append(f"  - check: {plain(iss['title'])} — {iss['next']}")
    return "\n".join(out) + "\n"


def write_trace(payload: dict, key: str, folder: Path) -> List[Path]:
    v = View(payload)
    o = v.out[key]
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", v.col_name(o["rel"], o["col"])).strip("_")
    proc = re.sub(r"[^A-Za-z0-9_.-]+", "_", payload["title"]).strip("_")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    h = folder / f"trace-{proc}-{base}.html"
    h.write_text(render_trace_html(payload, key), encoding="utf-8")
    md = folder / f"trace-{proc}-{base}.md"
    md.write_text(render_trace_md(payload, key), encoding="utf-8")
    return [h, md]
