"""Narrative Word document (.docx) for a handover pack, written as OOXML with the standard
library (a .docx is a ZIP of XML parts). It is the linear subset of the HTML: what the
procedure does, its signature, what the document can and cannot see, inputs, outputs,
column lineage, review issues and the steps. The HTML remains the working document.

OOXML conventions shared with the other bi-doc engines: A4; DXA widths on the grid and every
cell, summing exactly; headings carry outline levels so navigation works; code is one shaded
paragraph per line.
"""
from __future__ import annotations

import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from .view import ROWS, View, plain

PAGE_W, PAGE_H, MARGIN = 11906, 16838, 1134
CONTENT_W = PAGE_W - 2 * MARGIN
ACCENT = "4F46E5"
GREY_HDR = "E9EEF5"
MUTED = "475569"
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _t(text) -> str:
    return escape(_CTRL.sub("", str(text if text is not None else "")))


def run(text, bold=False, italic=False, color=None, size=None, font=None) -> str:
    props = []
    if font:
        props.append(f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:cs="{font}"/>')
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    if size:
        props.append(f'<w:sz w:val="{size * 2}"/><w:szCs w:val="{size * 2}"/>')
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{_t(text)}</w:t></w:r>'


def rich(text: str, size=None, color=None) -> str:
    """Text with `code` spans rendered in a monospace font."""
    out = []
    for i, part in enumerate(re.split(r"`([^`]*)`", text or "")):
        if not part:
            continue
        out.append(run(part, font="Consolas" if i % 2 else None, size=size, color=color))
    return "".join(out)


def para(runs_xml: str, style=None, shade=None, space_after=None, keep_next=False) -> str:
    props = []
    if style:
        props.append(f'<w:pStyle w:val="{style}"/>')
    if keep_next:
        props.append("<w:keepNext/>")
    if shade:
        props.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>')
    if space_after is not None:
        props.append(f'<w:spacing w:after="{space_after}"/>')
    ppr = f"<w:pPr>{''.join(props)}</w:pPr>" if props else ""
    return f"<w:p>{ppr}{runs_xml}</w:p>"


def heading(text, level: int) -> str:
    return para(run(text), style=f"Heading{level}")


def table(headers, rows, proportions=None) -> str:
    n = len(headers)
    proportions = proportions or [1.0 / n] * n
    widths = [int(CONTENT_W * p) for p in proportions]
    widths[-1] = CONTENT_W - sum(widths[:-1])
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    border = "<w:tblBorders>" + "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="CBD5E1"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")) + "</w:tblBorders>"

    def cell(content, width, shade=None):
        props = [f'<w:tcW w:w="{width}" w:type="dxa"/>']
        if shade:
            props.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>')
        return f"<w:tc><w:tcPr>{''.join(props)}</w:tcPr>{content}</w:tc>"

    def row_xml(cells, header=False):
        tcs = []
        for i, val in enumerate(cells[:n]):
            content = para(run(val, bold=True, size=8) if header else rich(str(val if val is not None else ""), size=8),
                           style="TableText")
            tcs.append(cell(content, widths[i], GREY_HDR if header else None))
        trpr = "<w:trPr><w:tblHeader/></w:trPr>" if header else ""
        return f"<w:tr>{trpr}{''.join(tcs)}</w:tr>"

    body = row_xml(headers, True) + "".join(row_xml(r) for r in rows)
    return (f'<w:tbl><w:tblPr><w:tblW w:w="{CONTENT_W}" w:type="dxa"/>{border}<w:tblLayout w:type="fixed"/></w:tblPr>'
            f"<w:tblGrid>{grid}</w:tblGrid>{body}</w:tbl>" + para("", space_after=80))


STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Segoe UI" w:hAnsi="Segoe UI" w:cs="Segoe UI"/><w:sz w:val="20"/><w:szCs w:val="20"/><w:lang w:val="en-ZA"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="100" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="80"/></w:pPr><w:rPr><w:b/><w:color w:val="0F172A"/><w:sz w:val="40"/><w:szCs w:val="40"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="320" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:color w:val="{ACCENT}"/><w:sz w:val="30"/><w:szCs w:val="30"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="80"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:color w:val="0F172A"/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="160" w:after="60"/><w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:color w:val="334155"/><w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="TableText"><w:name w:val="Table Text"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr><w:rPr><w:sz w:val="16"/><w:szCs w:val="16"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Note"><w:name w:val="Note"/><w:basedOn w:val="Normal"/><w:pPr><w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="B45309"/></w:pBdr><w:ind w:left="200"/></w:pPr><w:rPr><w:color w:val="{MUTED}"/></w:rPr></w:style>
</w:styles>"""


def build_document(payload: dict) -> str:
    v = View(payload)
    p = payload["procedure"]
    b = []
    a = b.append
    a(para(run(payload["title"]), style="Title"))
    a(para(run(f"Stored procedure documentation · generated {payload['generated']} by sql-doc-gen "
               f"{payload['generatorVersion']} · mode: {payload['mode']} · {p['source']} lines {p['lines'][0]}–{p['lines'][1]}",
               color=MUTED, size=9)))
    a(para(run("Design-time truth. ", bold=True) + run(
        "This document is derived statically from the procedure's code. It has no run history: it cannot say which branch "
        "ran, what a dynamic string held at run time, or whether a value is right — only every place that could make it "
        "wrong. Unresolved means unknown, not absent."), style="Note"))
    a(heading("What it does", 1))
    a(para(rich(payload["what"])))
    d = payload.get("details") or {}
    recorded = [(k, d[k]) for k in ("owner", "job", "server", "environment", "runbook", "sourceLocation", "notes") if d.get(k)]
    if recorded:
        names = {"owner": "Owner", "job": "SQL Agent job / schedule", "server": "Server and database", "environment": "Environment",
                 "runbook": "Runbook", "sourceLocation": "Source location", "notes": "Notes"}
        a(table(["Detail", "Value"], [[names[k], val] for k, val in recorded], [0.3, 0.7]))
    a(heading("Signature", 1))
    if p["parameters"]:
        a(table(["Parameter", "Type", "Default", "Direction"],
                [[f"`{x['name']}`", f"`{x['type']}`", x["default"] or "required",
                  "OUTPUT" if x["output"] else ("READONLY table" if x.get("readonly") else "input")] for x in p["parameters"]],
                [0.3, 0.25, 0.25, 0.2]))
    else:
        a(para(run("No parameters.", italic=True)))
    if p["returnCodes"]:
        a(para(run("Return codes: ") + rich(" ".join(f"`{c}`" for c in dict.fromkeys(p["returnCodes"])))))
    a(heading("What this document can and cannot see", 1))
    a(table(["Check", "Result", "Meaning"], [[c["check"], c["result"], c["meaning"]] for c in payload["coverage"]],
            [0.25, 0.2, 0.55]))
    a(heading("Inputs", 1))
    ins = [r for r in payload["relations"] if r["role"] in ("input", "both") and r["kind"] not in ("variable", "parameter")]
    if ins:
        a(table(["Object", "Kind", "Columns used", "Read at steps"],
                [[r["name"], r["kind"], ", ".join(k for k in (r.get("colReads") or {}) if k != "value")[:400],
                  " ".join(v.label(s) for s in r["reads"][:30])] for r in ins], [0.3, 0.15, 0.35, 0.2]))
    else:
        a(para(run("Reads no tables.", italic=True)))
    a(heading("Outputs", 1))
    outs = [r for r in payload["relations"] if r["role"] in ("output", "both")]
    if outs:
        a(table(["Output", "Kind", "How", "Written at steps"],
                [[r["name"], r["kind"], ", ".join(r["ops"]) or "", " ".join(v.label(s) for s in r["writes"][:30])]
                 for r in outs], [0.35, 0.15, 0.25, 0.25]))
    mids = [r for r in payload["relations"] if r["role"] == "intermediate" and r["kind"] != "variable"]
    if mids:
        a(heading("Intermediates", 2))
        a(table(["Name", "Kind", "Columns", "Created at"],
                [[r["name"], r["kind"], ", ".join(c["name"] for c in r["columns"])[:300],
                  " ".join(v.label(s) for s in r["created"][:5])] for r in mids], [0.25, 0.15, 0.45, 0.15]))
    a(heading("Output columns", 1))
    a(para(run("For each output column: the input columns its value is computed from, the steps that write it, the branch "
               "conditions those steps depend on, and how completely it could be traced.", color=MUTED)))
    a(table(["Column", "Status", "Computed from", "Steps", "Conditions"],
            [[v.col_name(o["rel"], o["col"]), o["status"], ", ".join(v.col_name(r, c) for r, c in o["direct"]) or "constants / row counts",
              " ".join(v.label(s) for s in o["steps"]), "; ".join(("NOT " if c["branch"] == "else" else "") + c["text"] for c in o["conditions"])]
             for o in payload["outputs"] if o["col"] != ROWS], [0.25, 0.1, 0.3, 0.15, 0.2]))
    a(heading("Review issues", 1))
    if payload["issues"]:
        for i in payload["issues"]:
            a(heading(f"[{i['severity']}{', possible' if i['certainty'] == 'possible' else ''}] {plain(i['title'])}", 3))
            a(para(run(i["why"])))
            a(para(run("Next step: ", bold=True) + run(i["next"])))
            if i["steps"]:
                a(para(run("Steps: " + ", ".join(v.label(s) for s in i["steps"][:20]), color=MUTED, size=9)))
    else:
        a(para(run("No findings.", italic=True)))
    a(heading("Steps", 1))
    a(table(["Step", "Kind", "Lines", "Summary"],
            [[s["label"], s["kind"], f"{s['lines'][0]}–{s['lines'][1]}" if s["lines"] else "", s["summary"]]
             for s in payload["steps"] if s["kind"] != "nested-end"], [0.08, 0.14, 0.12, 0.66]))
    c = payload["complexity"]
    a(heading("Complexity", 1))
    a(table(["Measure", "Value"], [["Statements", c["statements"]], ["Lines", c["lines"]], ["Lines of code", c["codeLines"]],
                                   ["Deepest nesting", c["nesting"]], ["Cyclomatic complexity", c["cyclomatic"]],
                                   ["Most joins in one statement", c["maxJoins"]]], [0.6, 0.4]))
    sect = (f'<w:sectPr><w:pgSz w:w="{PAGE_W}" w:h="{PAGE_H}"/><w:pgMar w:top="{MARGIN}" w:right="{MARGIN}" '
            f'w:bottom="{MARGIN}" w:left="{MARGIN}" w:header="567" w:footer="567" w:gutter="0"/></w:sectPr>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<w:body>{''.join(b)}{sect}</w:body></w:document>")


def write_docx(payload: dict, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>",
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>",
        "word/_rels/document.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>",
        "word/document.xml": build_document(payload),
        "word/styles.xml": STYLES,
        "docProps/core.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_t(payload['title'])}</dc:title><dc:creator>sql-doc-gen</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>',
        "docProps/app.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        "<Application>sql-doc-gen</Application></Properties>",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return path
