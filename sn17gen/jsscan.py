"""Minimal, deterministic JavaScript tokenizer (pure Python, no third-party parser; rule R19).

It is not a full ES parser. It recognises comments, strings, template literals (with nested ``${}``),
regular-expression literals (by the previous significant token), numbers, identifiers and punctuators,
and records the bracket depth of every token. That is enough to find top-level statements, the default
export, imports, and simple member chains for the fixed repair rules in ``repair.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class ScanError(ValueError):
    pass


@dataclass(frozen=True)
class Tok:
    kind: str   # ident | num | str | tmpl | regex | punct
    text: str
    start: int
    end: int
    depth: int
    line_break_before: bool


_PUNCT = sorted(
    """>>>= ... === !== **= <<= >>= >>> &&= ||= ??= => == != <= >= && || ?? ?. ++ -- += -= *= /= %= &= |= ^= ** << >>
    { } ( ) [ ] ; , < > + - * / % & | ^ ! ~ ? : = . @ #""".split(),
    key=len,
    reverse=True,
)
_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {")", "]", "}"}
_IDENT_START = re.compile(r"[A-Za-z_$\u0080-￿\\]")
_IDENT_PART = re.compile(r"[A-Za-z0-9_$\u0080-￿‌‍]*")
_WS = " \t\r\n\f\v ﻿  "
_REGEX_AFTER_KEYWORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else",
    "yield", "await",
}


def _regex_allowed(prev: Tok | None) -> bool:
    if prev is None:
        return True
    if prev.kind == "punct":
        return prev.text not in (")", "]", "}", "++", "--")
    if prev.kind == "ident":
        return prev.text in _REGEX_AFTER_KEYWORDS
    return False


def scan(src: str) -> list[Tok]:
    toks: list[Tok] = []
    stack: list[str] = []  # "(", "[", "{", or "${" (template substitution)
    i, n = 0, len(src)
    prev: Tok | None = None
    newline = False

    def add(kind: str, start: int, end: int, depth: int) -> None:
        nonlocal prev, newline
        t = Tok(kind, src[start:end], start, end, depth, newline)
        toks.append(t)
        prev = t
        newline = False

    def template_chars(j: int) -> tuple[int, str]:
        """Scan template characters from j; return (index after stop, 'end'|'expr')."""
        while j < n:
            c = src[j]
            if c == "\\":
                j += 2
                continue
            if c == "`":
                return j + 1, "end"
            if c == "$" and j + 1 < n and src[j + 1] == "{":
                return j + 2, "expr"
            j += 1
        raise ScanError("unterminated template literal")

    while i < n:
        c = src[i]
        if c in _WS:
            if c in "\n\r  ":
                newline = True
            i += 1
            continue
        if src.startswith("//", i):
            j = i + 2
            while j < n and src[j] not in "\n\r  ":
                j += 1
            i = j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise ScanError("unterminated block comment")
            if any(ch in src[i:j] for ch in "\n\r  "):
                newline = True
            i = j + 2
            continue
        depth = len(stack)
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] in "\n\r":
                    raise ScanError("unterminated string literal")
                j += 1
            if j >= n:
                raise ScanError("unterminated string literal")
            add("str", i, j + 1, depth)
            i = j + 1
            continue
        if c == "`":
            j, how = template_chars(i + 1)
            add("tmpl", i, j, depth)
            if how == "expr":
                stack.append("${")
            i = j
            continue
        if c == "}" and stack and stack[-1] == "${":
            stack.pop()
            j, how = template_chars(i + 1)
            add("tmpl", i, j, len(stack))
            if how == "expr":
                stack.append("${")
            i = j
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            j = i
            if src.startswith(("0x", "0X", "0b", "0B", "0o", "0O"), i):
                j = i + 2
                while j < n and (src[j].isalnum() or src[j] == "_"):
                    j += 1
            else:
                while j < n and (src[j].isdigit() or src[j] in "._"):
                    j += 1
                if j < n and src[j] in "eE":
                    j += 1
                    if j < n and src[j] in "+-":
                        j += 1
                    while j < n and src[j].isdigit():
                        j += 1
                if j < n and src[j] == "n":
                    j += 1
            add("num", i, j, depth)
            i = j
            continue
        if _IDENT_START.match(c):
            j = i + 1
            if c == "\\":  # unicode escape in identifier: \uXXXX or \u{...}
                m = re.compile(r"u(\{[0-9a-fA-F]+\}|[0-9a-fA-F]{4})").match(src, i + 1)
                if not m:
                    raise ScanError("bad identifier escape")
                j = m.end()
            m = _IDENT_PART.match(src, j)
            j = m.end() if m else j
            add("ident", i, j, depth)
            i = j
            continue
        if c == "/" and _regex_allowed(prev):
            j, in_class = i + 1, False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch in "\n\r":
                    raise ScanError("unterminated regex literal")
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                j += 1
            if j >= n:
                raise ScanError("unterminated regex literal")
            j += 1
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            add("regex", i, j, depth)
            i = j
            continue
        for p in _PUNCT:
            if src.startswith(p, i):
                if p == "?." and i + 2 < n and src[i + 2].isdigit():
                    continue  # a ? .5 : b
                break
        else:
            raise ScanError(f"unexpected character {c!r} at offset {i}")
        if p in _OPEN:
            add("punct", i, i + 1, depth)
            stack.append(p)
        elif p in _CLOSE:
            if not stack or stack[-1] == "${" or _OPEN[stack[-1]] != p:
                raise ScanError(f"unbalanced {p!r} at offset {i}")
            stack.pop()
            add("punct", i, i + 1, len(stack))
        else:
            add("punct", i, i + len(p), depth)
        i += len(p)
    if stack:
        raise ScanError(f"unclosed {stack[-1]!r} at end of input")
    return toks


def match_close(toks: list[Tok], idx: int) -> int:
    """Index of the token that closes the opener at idx (same depth, first one after it)."""
    opener = toks[idx]
    want = _OPEN[opener.text]
    for k in range(idx + 1, len(toks)):
        t = toks[k]
        if t.depth == opener.depth and t.kind == "punct" and t.text == want:
            return k
        if t.depth < opener.depth:
            break
    raise ScanError("no matching close")


_CLOSE_TO_OPEN = {")": "(", "]": "[", "}": "{"}


def match_open(toks: list[Tok], idx: int) -> int:
    """Index of the opener of the closer at idx (same depth, last one before it). Used by repair r2."""
    closer = toks[idx]
    want = _CLOSE_TO_OPEN[closer.text]
    for k in range(idx - 1, -1, -1):
        t = toks[k]
        if t.depth == closer.depth and t.kind == "punct" and t.text == want:
            return k
        if t.depth < closer.depth:
            break
    raise ScanError("no matching open")


def is_ref(toks: list[Tok], k: int) -> bool:
    """Identifier token k is not a property name after '.' / '?.' and not an object-literal key."""
    if toks[k].kind != "ident":
        return False
    if k > 0 and toks[k - 1].kind == "punct" and toks[k - 1].text in (".", "?."):
        return False
    if k + 1 < len(toks) and toks[k + 1].text == ":" and k > 0 and toks[k - 1].text in ("{", ","):
        return False
    return True
