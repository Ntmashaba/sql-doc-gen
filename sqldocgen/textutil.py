"""Text handling: decoding SQL files, line numbers, UTF-16 offsets, comments and secrets.

ScriptDom reports positions as UTF-16 code units. Python strings index code points, and the
page's JavaScript indexes UTF-16 again. ``Utf16Map`` converts in both directions; for text
without characters outside the Basic Multilingual Plane (nearly all SQL) it is the identity.
"""
from __future__ import annotations

import bisect
import codecs
import re
from typing import List, Optional, Tuple


def decode_sql_bytes(data: bytes) -> Tuple[str, str]:
    """Decode a .sql file the way SSMS writes them: UTF-8 or UTF-16 with or without a BOM,
    falling back to Windows-1252. Returns (text, encoding name). Line endings are kept."""
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith(codecs.BOM_UTF16_LE):
        return data[2:].decode("utf-16-le", errors="replace"), "utf-16"
    if data.startswith(codecs.BOM_UTF16_BE):
        return data[2:].decode("utf-16-be", errors="replace"), "utf-16-be"
    if len(data) >= 4 and data[1] == 0 and data[3] == 0 and data[0] != 0:
        return data.decode("utf-16-le", errors="replace"), "utf-16-le"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "cp1252"


class LineIndex:
    """Offset -> 1-based line number for one text."""

    def __init__(self, text: str):
        self.starts = [0] + [m.end() for m in re.finditer(r"\n", text)]
        self.count = len(self.starts)

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, max(0, offset))

    def lines(self, span: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
        if not span:
            return None
        s, e = span
        return self.line(s), self.line(max(s, e - 1))


class Utf16Map:
    """Convert between UTF-16 code-unit offsets (ScriptDom, JavaScript) and code points (Python)."""

    def __init__(self, text: str):
        self.astral = [i for i, ch in enumerate(text) if ord(ch) > 0xFFFF]
        self.identity = not self.astral
        # UTF-16 index of the second code unit of each astral character
        self._second = [pos + k + 1 for k, pos in enumerate(self.astral)]

    def to_cp(self, u16: int) -> int:
        if self.identity or u16 < 0:
            return u16
        return u16 - bisect.bisect_left(self._second, u16)

    def to_u16(self, cp: int) -> int:
        if self.identity or cp < 0:
            return cp
        return cp + bisect.bisect_left(self.astral, cp)


# --------------------------------------------------------------------------- lexical scan

_SCAN = re.compile(
    r"""(?P<line>--[^\r\n]*)
      | (?P<block>/\*)
      | (?P<str>N?'(?:[^']|'')*'?)
      | (?P<bracket>\[(?:[^\]]|\]\])*\]?)
      | (?P<dq>"(?:[^"]|"")*"?)
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _block_comment_end(text: str, start: int) -> int:
    """End of a (nestable) T-SQL block comment starting at ``start``."""
    depth, i, n = 0, start, len(text)
    while i < n:
        if text.startswith("/*", i):
            depth += 1
            i += 2
        elif text.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return n


def scan_tokens(text: str):
    """Yield (kind, start, end) for comments ('comment'), string literals ('string') and
    quoted identifiers ('quoted') in source order; everything else is skipped."""
    i, n = 0, len(text)
    while i < n:
        m = _SCAN.search(text, i)
        if not m:
            return
        kind = m.lastgroup
        if kind == "block":
            end = _block_comment_end(text, m.start())
            yield "comment", m.start(), end
            i = end
        elif kind == "line":
            yield "comment", m.start(), m.end()
            i = m.end()
        elif kind == "str":
            yield "string", m.start(), m.end()
            i = m.end()
        else:
            yield "quoted", m.start(), m.end()
            i = m.end()


def extract_comments(text: str) -> List[Tuple[int, int, str]]:
    """Comments with their spans and their text without the comment markers."""
    out = []
    for kind, s, e in scan_tokens(text):
        if kind != "comment":
            continue
        raw = text[s:e]
        if raw.startswith("--"):
            body = raw[2:]
        else:
            body = raw[2:-2] if raw.endswith("*/") else raw[2:]
        body = "\n".join(line.strip(" \t*-=#") for line in body.strip().splitlines()).strip()
        out.append((s, e, body))
    return out


# --------------------------------------------------------------------------- secrets

# Secret values are masked in place with '*' so every offset, line and column stays exact.
_KV_SECRET = re.compile(
    r"(?P<key>\b(?:password|pwd|passwd|secret|accountkey|account\s+key|sharedaccesskey|"
    r"shared\s+access\s+key|sig|client_secret|api[_-]?key|token)\s*=\s*)(?P<val>[^;'\"\s][^;'\"]*)",
    re.IGNORECASE)
_STATEMENT_SECRET = re.compile(
    r"(?P<lead>\b(?:PASSWORD|SECRET|DECRYPTION\s+BY\s+PASSWORD|ENCRYPTION\s+BY\s+PASSWORD)\s*=\s*N?)"
    r"(?P<lit>'(?:[^']|'')*')",
    re.IGNORECASE)
_PROC_SECRET = re.compile(
    r"(?P<lead>@(?:rmtpassword|password|new_password|passwd|secret)\s*=\s*N?)(?P<lit>'(?:[^']|'')*')",
    re.IGNORECASE)
_POSITIONAL_PROCS = {
    # procedure name -> zero-based position of the password argument
    "sp_addlinkedsrvlogin": 4,
    "sp_addlogin": 1,
    "sp_password": 1,
    "sp_change_users_login": None,
}
_BEARER = re.compile(r"(?P<lead>\bBearer\s+)(?P<val>[A-Za-z0-9._~+/=-]{12,})", re.IGNORECASE)


def _mask(chars: list, start: int, end: int) -> bool:
    changed = False
    for k in range(start, end):
        if chars[k] not in "*\r\n":
            chars[k] = "*"
            changed = True
    return changed


def mask_secrets(text: str) -> Tuple[str, int]:
    """Mask passwords, keys and tokens found in the text. Returns (masked text, count).

    Only the secret value is masked (same length, '*'), so names, structure and every
    position are kept. Connection-string pairs are looked for inside string literals;
    statement options such as ``PASSWORD = '...'`` and procedure arguments such as
    ``@rmtpassword = '...'`` anywhere outside comments.
    """
    chars = list(text)
    count = 0
    literals = [(s, e) for kind, s, e in scan_tokens(text) if kind == "string"]
    for s, e in literals:
        lit = text[s:e]
        for m in _KV_SECRET.finditer(lit):
            if _mask(chars, s + m.start("val"), s + m.end("val")):
                count += 1
        for m in _BEARER.finditer(lit):
            if _mask(chars, s + m.start("val"), s + m.end("val")):
                count += 1
    comment_spans = [(s, e) for kind, s, e in scan_tokens(text) if kind == "comment"]

    def in_comment(pos: int) -> bool:
        k = bisect.bisect_right(comment_spans, (pos, 10 ** 12)) - 1
        return k >= 0 and comment_spans[k][0] <= pos < comment_spans[k][1]

    for rx in (_STATEMENT_SECRET, _PROC_SECRET):
        for m in rx.finditer(text):
            if in_comment(m.start()):
                continue
            lit_s, lit_e = m.start("lit"), m.end("lit")
            if _mask(chars, lit_s + 1, lit_e - 1):
                count += 1
    # positional password arguments: EXEC sp_addlinkedsrvlogin 'srv', 'false', NULL, 'user', 'pw'
    for name, position in _POSITIONAL_PROCS.items():
        if position is None:
            continue
        for m in re.finditer(r"\b(?:EXEC(?:UTE)?\s+)?(?:\w+\.)?" + name + r"\b", text, re.IGNORECASE):
            if in_comment(m.start()):
                continue
            args = [(s, e) for s, e in literals if s > m.end()][: position + 4]
            # stop at the end of the statement: arguments must follow each other with only , NULL and spaces
            seq, prev = [], m.end()
            for s, e in args:
                gap = text[prev:s]
                if not re.fullmatch(r"[\s,]*(?:(?:NULL|@\w+\s*=\s*N?|N)\s*,?\s*)*", gap, re.IGNORECASE):
                    break
                seq.append((s, e, gap))
                prev = e
            # count positions including NULL arguments in the gaps
            pos_idx, hit = 0, None
            for s, e, gap in seq:
                pos_idx += len(re.findall(r"\bNULL\b", gap, re.IGNORECASE))
                if pos_idx == position:
                    hit = (s, e)
                    break
                pos_idx += 1
            if hit and _mask(chars, hit[0] + 1, hit[1] - 1):
                count += 1
    return "".join(chars), count


def scrub_text(value: str) -> Tuple[str, int]:
    """Replace secrets in free text (maintained details, generated strings) with [redacted].

    Unlike ``mask_secrets`` this looks for key=value pairs anywhere, not only inside SQL
    string literals. Values that are already masked ('*') are left alone."""
    count = 0

    def repl(m):
        nonlocal count
        if set(m.group("val").strip()) <= {"*"}:
            return m.group(0)
        count += 1
        return m.group("key") + "[redacted]"

    out = _KV_SECRET.sub(repl, value)
    out = _BEARER.sub(lambda m: (m.group("lead") + "[redacted]"), out) if _BEARER.search(out) else out
    count += len(_BEARER.findall(value))
    return out, count
