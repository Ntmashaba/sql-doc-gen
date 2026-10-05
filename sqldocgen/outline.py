"""The procedure's own outline, from the section comments its authors wrote.

Long procedures are usually divided by comments: a banner block per part
(``/* ==== 2. Reference data ==== */``) and numbered line comments per step
(``-- 2.1 Calendar for the load window``). Those headings are the best table of contents
there is, so the Steps view is grouped by them.

A comment is a heading when it stands on its own line(s) inside the procedure body and
either starts with a section number (``2``, ``2.1``, ``4.3.12``, optionally after ``Step``,
``Section``, ``Part``) or is a short banner framed by a line of ``=``, ``-``, ``*`` or ``#``.
Numbered headings take their level from the number; unnumbered banners are level 1.
Fewer than two headings: no outline.
"""
from __future__ import annotations

import re
import bisect
from typing import Dict, List, Tuple

from .syntax import Text
from .textutil import scan_tokens

# "2.1 Calendar", "2. Reference data", "Step 3: merge" - but not "1 row per order line"
NUMBERED = re.compile(r"^(?:(?:step|section|part|phase|stage)\s+(?P<a>\d{1,3}(?:\.\d{1,3}){0,4})[.):]?"
                      r"|(?P<b>\d{1,3}(?:\.\d{1,3}){1,4})[.):]?|(?P<c>\d{1,3})[.)])\s+(?P<t>\S.{0,118})$", re.IGNORECASE)
RULE = re.compile(r"[=\-*#~]{5,}")
DECOR = " \t*-=#~/"


def _clean(raw: str) -> List[str]:
    body = raw[2:] if raw.startswith("--") else (raw[2:-2] if raw.endswith("*/") else raw[2:])
    lines = [ln.strip(DECOR) for ln in body.splitlines()]
    return [ln for ln in lines if ln and not RULE.fullmatch(ln)]


def headings(text: str, start: int, end: int) -> List[dict]:
    out = []
    for kind, s, e in scan_tokens(text):
        if kind != "comment" or s < start or s >= end:
            continue
        line_start = text.rfind("\n", 0, s) + 1
        if text[line_start:s].strip():
            continue                                    # trails code on the same line
        raw = text[s:e]
        lines = _clean(raw)
        if not lines:
            continue
        first = lines[0]
        m = NUMBERED.match(first)
        banner = bool(RULE.search(raw)) and len(lines) <= 3 and len(first) <= 90
        if m:
            number, title = m.group("a") or m.group("b") or m.group("c"), m.group("t").strip()
            level = number.count(".") + 1
        elif banner:
            number, title, level = "", first, 1
        else:
            continue
        title = re.sub(r"\s+", " ", title).rstrip(" .:")
        out.append({"number": number, "title": title, "level": min(level, 4), "pos": s})
    return out


def outline(unit, ctx) -> Tuple[List[dict], Dict[str, str]]:
    """([{id, number, title, level, line, parent}], {step id: section id})."""
    text: Text = unit.text
    node = unit.node or {}
    body = text.span(node.get("StatementList")) if node.get("StatementList") else None
    if body is None and unit.statements:
        body = text.span(unit.statements[0])
    if body is None:
        return [], {}
    end = unit.span[1] if unit.span else len(text.text)
    found = headings(text.text, body[0], end)
    if len(found) < 2:
        return [], {}
    sections, stack = [], []
    for i, h in enumerate(found):
        sid = f"sec{i + 1}"
        while stack and stack[-1]["level"] >= h["level"]:
            stack.pop()
        parent = stack[-1]["id"] if stack else None
        sec = {"id": sid, "number": h["number"], "title": h["title"], "level": len(stack) + 1 if not h["number"]
               else h["level"], "line": text.lines.line(h["pos"]), "parent": parent, "pos": h["pos"]}
        sections.append(sec)
        stack.append(sec)
    # levels follow nesting, so a "7.2" under an unnumbered banner still indents once
    depth = {}
    for sec in sections:
        depth[sec["id"]] = depth[sec["parent"]] + 1 if sec["parent"] else 1
        sec["level"] = depth[sec["id"]]
    starts = [s["pos"] for s in sections]
    step_section: Dict[str, str] = {}
    by_id = ctx.step_by_id
    for st in ctx.steps:
        top = st
        while top.parent and top.parent in by_id:
            top = by_id[top.parent]                     # nested code belongs where its EXEC is
        if top.text_id != "main" or not top.span:
            continue
        k = bisect.bisect_right(starts, top.span[0]) - 1
        if k >= 0:
            step_section[st.id] = sections[k]["id"]
    for s in sections:
        s.pop("pos", None)
    return sections, step_section
