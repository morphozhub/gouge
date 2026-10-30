"""Deterministic local repair (rule sets r1 and r2, pure Python, fixed rules; R19).

r1 is the v0 rule set and never changes (old records must replay to the same final JS). r2 = r1 plus
  - token rewrite ``unary_pow``: a unary ``-``/``+``/``!``/``~`` directly before an exponentiation operand,
    ``-X ** Y`` (a JavaScript SyntaxError), becomes ``-(X ** Y)`` (the maths reading; C1-P6, E1: 2 grey boxes);
  - structural check ``unary_before_exponent``: any such pattern left over is rejected (retry, not a grey box).
r3 (pipeline v2, R1_refine) = r2 (same rewrites, frozen) + the structural check also rejects the three patterns C2
found r2 missed: a unary operator at statement start after an ``if/while/for/with (...)`` head or after a block
``}`` (r2's position test calls those binary: ``if (c) -a ** 2;``, ``if (c) {} -a ** 2;``) and a ``new`` operand
(``-new Foo().x ** 2``). Binary ``a - b ** 2`` and object-literal ``{} - x ** 2`` are not flagged.

Pipeline for one response text:
    extract_js -> normalise -> token rewrites -> restructure (fit wrapper | hoist) -> structural check

* Token rewrites (each a fixed textual edit at token offsets):
  - drop top-level ``import`` statements (imports are forbidden; models often add ``import * as THREE``);
  - drop the ``export`` keyword of non-default exports, drop ``export {...}`` / ``export * from``;
  - ``Math.random`` -> ``__sn17_rand`` (fixed-seed mulberry32 helper appended);
  - ``Date.now()`` / ``performance.now()`` -> ``0``;
  - ``console.<fn>(...)`` -> ``void 0``;
  - ``A.constructor === B`` (simple member chains) -> ``(A instanceof B)``; any other access to a
    production-forbidden property name (.constructor, .__proto__, .__defineGetter__ ...) -> an inert
    ``.__sn17_blocked_<name>`` (the access is removed, never re-routed through computed access);
  - ``THREE.LuminanceFormat`` -> ``THREE.RedFormat`` (allow-listed but not exported by r183).
* Restructure, fit mode (default): the model's default export becomes ``function __sn17_model(THREE)``
  in place, and a new ``export default function generate(THREE)`` is appended LAST. Every original
  top-level statement therefore runs before generate() in both the validation path (``new Function``,
  where code after the export never runs: the TDZ trap F5) and the render path. The wrapper refreshes
  all cached bounding boxes, centres the object and scales it uniformly so its largest side is
  ``fit_target`` of the cube.
  Hoist mode (``fit_wrapper=False``): everything after the default export is moved above it.
* Structural check (pure Python, used for retry decisions): code present, tokenizes, one top-level
  export default that is a non-async function, no import/async/await tokens, no forbidden identifier
  references, size <= 1 MiB.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .contract import FORBIDDEN_IDENTIFIERS, FORBIDDEN_PROPERTY_NAMES, MAX_FILE_BYTES
from .jsscan import ScanError, Tok, is_ref, match_close, match_open, scan

REPAIR_VERSION = "r1"          # the v0 rule set (default of the functions below, so v0 callers are unchanged)
REPAIR_VERSIONS = ("r1", "r2", "r3")
_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+.-]*)[ \t]*\r?\n(.*?)(?:\r?\n)?[ \t]*```", re.S)
_OPEN_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+.-]*)[ \t]*\r?\n", re.S)

RAND_HELPER = """function __sn17_rand() {
  const st = __sn17_rand.st || (__sn17_rand.st = { s: 305419896 });
  st.s = (st.s + 1831565813) | 0;
  let t = st.s;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}
"""

FIT_HELPER = """function __sn17_fit(THREE, root) {
  const holder = new THREE.Group();
  holder.add(root);
  holder.updateMatrixWorld(true);
  holder.traverse(function (o) {
    if (o.geometry && o.geometry.isBufferGeometry) {
      o.geometry.computeBoundingBox();
      o.geometry.computeBoundingSphere();
    }
    if (o.isInstancedMesh) {
      o.computeBoundingBox();
      o.computeBoundingSphere();
    }
  });
  const box = new THREE.Box3().setFromObject(holder);
  if (box.isEmpty()) return holder;
  const size = new THREE.Vector3();
  const center = new THREE.Vector3();
  box.getSize(size);
  box.getCenter(center);
  const m = Math.max(size.x, size.y, size.z);
  if (!(m > 0) || !isFinite(m)) return holder;
  const s = __FIT__ / m;
  holder.scale.set(s, s, s);
  holder.position.set(-center.x * s, -center.y * s, -center.z * s);
  holder.updateMatrixWorld(true);
  return holder;
}
"""

WRAPPER = """export default function generate(THREE) {
  return __sn17_fit(THREE, __sn17_model(THREE));
}
"""


@dataclass
class RepairResult:
    js: str | None
    ok: bool                      # passes the pure-Python structural check
    reasons: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)   # rules that fired, in application order
    extracted_from: str = ""

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.js.encode("utf-8")).hexdigest() if self.js is not None else None


# ----------------------------------------------------------------------------------- extraction
def extract_js(text: str) -> tuple[str | None, str]:
    """Pick the module source from a model reply. Fixed preference order:
    1. the LAST complete fenced block that contains 'export default' (else 'function generate');
    2. the longest complete fenced block;
    3. an unterminated last fence (truncated reply): from the fence to the end;
    4. the whole text if it contains 'export default' or 'function generate'."""
    if not text or not text.strip():
        return None, "empty"
    blocks = [m.group(2) for m in _FENCE.finditer(text)]
    for needle in ("export default", "function generate"):
        hits = [b for b in blocks if needle in b]
        if hits:
            return hits[-1], f"fence:{needle}"
    if blocks:
        longest = max(range(len(blocks)), key=lambda k: (len(blocks[k]), -k))
        return blocks[longest], "fence:longest"
    opens = list(_OPEN_FENCE.finditer(text))
    if opens:
        tail = text[opens[-1].end():]
        if "export default" in tail or "function generate" in tail:
            return tail, "fence:unterminated"
    if "export default" in text or "function generate" in text:
        return text, "raw"
    return None, "no_code"


def normalise(js: str) -> str:
    js = js.replace("\r\n", "\n").replace("\r", "\n")
    if js.startswith("﻿"):
        js = js[1:]
    js = js.strip("\n")
    return js + "\n"


# ----------------------------------------------------------------------------------- edits
def _apply(src: str, edits: list[tuple[int, int, str]]) -> str:
    edits = sorted(edits, key=lambda e: (e[0], e[1]))
    out, pos = [], 0
    for start, end, rep in edits:
        if start < pos:
            continue  # overlapping edit: the earlier one wins (deterministic)
        out.append(src[pos:start])
        out.append(rep)
        pos = end
    out.append(src[pos:])
    return "".join(out)


def _chain_back(toks: list[Tok], k: int) -> int | None:
    """toks[k] is the last identifier of a simple member chain ident(.ident)*; return the index of
    its first identifier, or None if the chain is not simple."""
    j = k
    while j >= 2 and toks[j - 1].text == "." and toks[j - 2].kind == "ident":
        j -= 2
    if toks[j].kind != "ident":
        return None
    if j > 0 and toks[j - 1].text in (".", "?.", ")", "]"):
        return None
    if any(toks[q].text in FORBIDDEN_PROPERTY_NAMES for q in range(j, k + 1)):
        return None
    return j


def _chain_forward(toks: list[Tok], k: int) -> int | None:
    """toks[k] starts a simple chain ident(.ident)*; return the index of its last identifier if the chain
    is not followed by a call, index or further member access."""
    if k >= len(toks) or toks[k].kind != "ident":
        return None
    j = k
    while j + 2 < len(toks) and toks[j + 1].text == "." and toks[j + 2].kind == "ident":
        j += 2
    if j + 1 < len(toks) and toks[j + 1].text in ("(", "[", ".", "?."):
        return None
    if any(toks[q].text in FORBIDDEN_PROPERTY_NAMES for q in range(k, j + 1)):
        return None
    return j


def token_rewrites(src: str) -> tuple[str, list[str]]:
    toks = scan(src)
    edits: list[tuple[int, int, str]] = []
    rules: list[str] = []
    n = len(toks)

    def fire(rule: str) -> None:
        if rule not in rules:
            rules.append(rule)

    k = 0
    while k < n:
        t = toks[k]
        nxt = toks[k + 1] if k + 1 < n else None
        # top-level import statement (not import() / import.meta)
        if t.kind == "ident" and t.text == "import" and t.depth == 0 and nxt is not None and nxt.text not in ("(", "."):
            j = k + 1
            while j < n and not (toks[j].kind == "str" and toks[j].depth == 0):
                j += 1
            if j < n:
                end = toks[j + 1].end if j + 1 < n and toks[j + 1].text == ";" else toks[j].end
                edits.append((t.start, end, ""))
                fire("drop_import")
                k = j + 1
                continue
        # non-default exports
        if t.kind == "ident" and t.text == "export" and t.depth == 0 and nxt is not None and nxt.text != "default":
            if nxt.text in ("{", "*"):
                j = k + 1
                if nxt.text == "{":
                    j = match_close(toks, k + 1) + 1
                else:
                    j = k + 2
                if j < n and toks[j].text == "from" and j + 1 < n and toks[j + 1].kind == "str":
                    j += 2
                elif nxt.text == "*":
                    while j < n and not (toks[j].kind == "str" and toks[j].depth == 0):
                        j += 1
                    j += 1
                end_tok = toks[j - 1]
                end = toks[j].end if j < n and toks[j].text == ";" else end_tok.end
                edits.append((t.start, end, ""))
                fire("drop_export_list")
                k = j
                continue
            edits.append((t.start, nxt.start, ""))
            fire("drop_named_export_keyword")
            k += 1
            continue
        if t.kind == "ident" and is_ref(toks, k) and nxt is not None and nxt.text == ".":
            prop = toks[k + 2] if k + 2 < n else None
            if prop is not None and prop.kind == "ident":
                if t.text == "Math" and prop.text == "random":
                    edits.append((t.start, prop.end, "__sn17_rand"))
                    fire("math_random")
                    k += 3
                    continue
                if t.text in ("Date", "performance") and prop.text == "now" and k + 4 < n \
                        and toks[k + 3].text == "(" and toks[k + 4].text == ")":
                    edits.append((t.start, toks[k + 4].end, "0"))
                    fire("clock_now_zero")
                    k += 5
                    continue
                if t.text == "console" and k + 3 < n and toks[k + 3].text == "(":
                    close = match_close(toks, k + 3)
                    edits.append((t.start, toks[close].end, "void 0"))
                    fire("drop_console")
                    k = close + 1
                    continue
                if t.text == "THREE" and prop.text == "LuminanceFormat":
                    edits.append((prop.start, prop.end, "RedFormat"))
                    fire("luminance_to_red")
                    k += 3
                    continue
        if t.kind == "punct" and t.text in (".", "?.") and nxt is not None and nxt.kind == "ident" \
                and nxt.text in FORBIDDEN_PROPERTY_NAMES:
            done = False
            if nxt.text == "constructor" and k + 3 < n and toks[k + 2].text in ("===", "==", "!==", "!="):
                a0 = _chain_back(toks, k - 1) if k >= 1 else None
                b1 = _chain_forward(toks, k + 3)
                if a0 is not None and b1 is not None:
                    a_txt = src[toks[a0].start:toks[k - 1].end]
                    b_txt = src[toks[k + 3].start:toks[b1].end]
                    neg = toks[k + 2].text.startswith("!")
                    rep = f"{'!' if neg else ''}({a_txt} instanceof {b_txt})"
                    edits.append((toks[a0].start, toks[b1].end, rep))
                    fire("constructor_eq_to_instanceof")
                    k = b1 + 1
                    done = True
            if not done:
                edits.append((nxt.start, nxt.end, "__sn17_blocked_" + nxt.text.strip("_")))
                fire("block_forbidden_property")
                k += 2
            continue
        k += 1
    return _apply(src, edits), rules


# ----------------------------------------------------------------------------------- r2: unary before **
_UNARY_PUNCT = ("-", "+", "!", "~")
_UNARY_WORDS = ("typeof", "void", "delete", "await")
# identifiers after which an expression starts (so a following '(' / '[' is not a call / index, and a
# following '-' is unary)
_EXPR_KEYWORDS = frozenset((
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else",
    "yield", "await", "if", "while", "for", "switch", "catch", "with", "function", "class", "extends",
    "let", "const", "var", "export", "default", "import",
))


def _unary_position(toks: list[Tok], k: int) -> bool:
    """toks[k] is a '-'/'+'/'!'/'~' token; True when it is a prefix (unary) operator. Conservative: after
    ')', ']', '}', '++', '--', a literal or a plain identifier it is treated as binary."""
    if k == 0:
        return True
    p = toks[k - 1]
    if p.kind == "punct":
        return p.text not in (")", "]", "}", "++", "--")
    if p.kind == "ident":
        return p.text in _EXPR_KEYWORDS
    if p.kind == "tmpl":
        return p.text.endswith("${")
    return False


def _operand_start(toks: list[Tok], k: int) -> int | None:
    """toks[k] is the last token of the left operand of '**'; return the index of the operand's first token
    (member chains, calls, indexing, parentheses, postfix ++/--), or None if the operand is not simple."""
    j = k
    if toks[j].text in ("++", "--") and toks[j].kind == "punct":
        j -= 1
        if j < 0:
            return None
    while True:
        t = toks[j]
        if t.kind == "punct" and t.text in (")", "]"):
            try:
                j = match_open(toks, j)
            except ScanError:
                return None
        elif t.kind in ("ident", "num", "str", "regex"):
            if t.kind == "ident" and t.text in _EXPR_KEYWORDS:
                return None
        elif t.kind == "tmpl" and t.text.startswith("`") and t.text.endswith("`") and len(t.text) >= 2:
            pass
        else:
            return None
        if j == 0:
            return j
        p = toks[j - 1]
        if p.kind == "punct" and p.text in (".", "?.") and toks[j].kind == "ident":
            if j - 2 < 0:
                return None
            j -= 2
            continue
        if toks[j].text in ("(", "[") and toks[j].kind == "punct" and (
                (p.kind == "ident" and p.text not in _EXPR_KEYWORDS) or (p.kind == "punct" and p.text in (")", "]"))
                or p.kind in ("str", "tmpl")):
            j -= 1
            continue
        return j


def _operand_end(toks: list[Tok], k: int) -> int | None:
    """toks[k] is the first token of an operand; return the index of its last token, or None."""
    n = len(toks)
    if k >= n:
        return None
    t = toks[k]
    j = k
    if t.kind == "punct" and t.text in ("(", "["):
        try:
            j = match_close(toks, k)
        except ScanError:
            return None
    elif t.kind == "ident":
        if t.text in _EXPR_KEYWORDS:
            return None
    elif t.kind in ("num", "str", "regex"):
        pass
    elif t.kind == "tmpl" and t.text.startswith("`") and t.text.endswith("`") and len(t.text) >= 2:
        pass
    else:
        return None
    while j + 1 < n:
        nx = toks[j + 1]
        if nx.kind == "punct" and nx.text in (".", "?.") and j + 2 < n and toks[j + 2].kind == "ident":
            j += 2
            continue
        if nx.kind == "punct" and nx.text == "?." and j + 2 < n and toks[j + 2].text in ("(", "["):
            try:
                j = match_close(toks, j + 2)
            except ScanError:
                return None
            continue
        if nx.kind == "punct" and nx.text in ("(", "["):
            try:
                j = match_close(toks, j + 1)
            except ScanError:
                return None
            continue
        break
    if j + 1 < n and toks[j + 1].kind == "punct" and toks[j + 1].text in ("++", "--") and not toks[j + 1].line_break_before:
        j += 1
    return j


def _exponent_rhs_end(toks: list[Tok], m: int) -> int | None:
    """toks[m] is '**'; return the index of the last token of its right operand (a right-associative
    chain ``a ** b ** c`` is taken whole), or None."""
    n = len(toks)
    j = m + 1
    prefixed = False
    while j < n and ((toks[j].kind == "punct" and toks[j].text in _UNARY_PUNCT + ("++", "--"))
                     or (toks[j].kind == "ident" and toks[j].text in _UNARY_WORDS)):
        prefixed = True
        j += 1
    e = _operand_end(toks, j)
    if e is None:
        return None
    if not prefixed and e + 1 < n and toks[e + 1].kind == "punct" and toks[e + 1].text == "**":
        return _exponent_rhs_end(toks, e + 1)
    return e


def _statement_start_after(toks: list[Tok], k: int) -> bool:
    """r3: toks[k] is ')' or '}'. True when the next token starts a statement (so a following '-' is unary):
    ')' closes an ``if/while/for/with`` head; '}' closes a block (not an object literal): the token before its
    '{' is ')', '=>', else/do/try/finally, ';', '{', '}', a non-keyword identifier (class body) or nothing."""
    try:
        o = match_open(toks, k)
    except ScanError:
        return False
    p = toks[o - 1] if o > 0 else None
    if toks[k].text == ")":
        return p is not None and p.kind == "ident" and p.text in ("if", "while", "for", "with")
    if p is None:
        return True
    if p.kind == "punct":
        return p.text in (")", "=>", ";", "{", "}")
    if p.kind == "ident":
        return p.text in ("else", "do", "try", "finally") or p.text not in _EXPR_KEYWORDS
    return False


def _unary_pow_sites_r3_extra(toks: list[Tok]) -> list[int]:
    """r3: '**' sites r2 misses (see the module docstring). Returns the '**' token indices."""
    out = []
    for m, t in enumerate(toks):
        if not (t.kind == "punct" and t.text == "**") or m == 0:
            continue
        s = _operand_start(toks, m - 1)
        if s is None or s == 0:
            continue
        while s > 0 and toks[s - 1].kind == "ident" and toks[s - 1].text == "new":
            s -= 1                                   # `new Foo().x ** 2`: the operand starts at `new`
        if s == 0:
            continue
        u = toks[s - 1]
        if u.kind == "punct" and u.text in _UNARY_PUNCT:
            q = s - 1
            unary = _unary_position(toks, q) or (q > 0 and toks[q - 1].kind == "punct"
                                                 and toks[q - 1].text in (")", "}") and _statement_start_after(toks, q - 1))
        else:
            unary = u.kind == "ident" and u.text in _UNARY_WORDS and is_ref(toks, s - 1)
        if unary:
            out.append(m)
    return out


def _unary_pow_sites(toks: list[Tok]) -> list[tuple[int, int, int, bool]]:
    """Every '**' whose left operand is directly preceded by a unary operator in unary position.
    Returns (pow index, operand start, rhs end or -1, fixable) in token order."""
    out = []
    for m, t in enumerate(toks):
        if not (t.kind == "punct" and t.text == "**") or m == 0:
            continue
        s = _operand_start(toks, m - 1)
        if s is None or s == 0:
            continue
        u = toks[s - 1]
        unary = (u.kind == "punct" and u.text in _UNARY_PUNCT and _unary_position(toks, s - 1)) or \
                (u.kind == "ident" and u.text in _UNARY_WORDS and is_ref(toks, s - 1))
        if not unary:
            continue
        e = _exponent_rhs_end(toks, m)
        fixable = e is not None and u.kind == "punct"
        out.append((m, s, e if e is not None else -1, fixable))
    return out


def fix_unary_pow(src: str, max_passes: int = 500) -> tuple[str, list[str]]:
    """r2 rewrite: ``-X ** Y`` -> ``-(X ** Y)`` (also for '+', '!', '~'), one site per pass, re-scanning after
    each edit so nested sites stay correct. Sites it cannot parse are left for the structural check."""
    rules: list[str] = []
    for _ in range(max_passes):
        toks = scan(src)
        site = next((x for x in _unary_pow_sites(toks) if x[3]), None)
        if site is None:
            break
        _, s, e, _ = site
        src = src[:toks[s].start] + "(" + src[toks[s].start:toks[e].end] + ")" + src[toks[e].end:]
        if "unary_pow" not in rules:
            rules.append("unary_pow")
    return src, rules


# ----------------------------------------------------------------------------------- restructure
@dataclass
class _ModelFn:
    kind: str                     # function | arrow | ident | bare
    export_start: int | None      # offset of 'export' (None for bare 'function generate')
    head_end: int                 # end of the text replaced by the new head
    body_end: int                 # end offset of the function/expression
    stmt_end: int                 # end of the statement (after an optional ';')
    name: str | None


def _find_model_fn(src: str, toks: list[Tok]) -> _ModelFn | None:
    n = len(toks)
    for k, t in enumerate(toks):
        if not (t.kind == "ident" and t.text == "export" and t.depth == 0 and k + 1 < n and toks[k + 1].text == "default"):
            continue
        j = k + 2
        if j >= n:
            return None
        head = toks[j]
        if head.text == "async":
            return None
        if head.text == "function":
            q = j + 1
            if q < n and toks[q].text == "*":
                return None
            name = None
            if q < n and toks[q].kind == "ident":
                name = toks[q].text
                q += 1
            if q >= n or toks[q].text != "(":
                return None
            pclose = match_close(toks, q)
            if pclose + 1 >= n or toks[pclose + 1].text != "{":
                return None
            bclose = match_close(toks, pclose + 1)
            end = toks[bclose].end
            stmt_end = toks[bclose + 1].end if bclose + 1 < n and toks[bclose + 1].text == ";" else end
            head_end = toks[q - 1].end if name else head.end
            return _ModelFn("function", t.start, head_end, end, stmt_end, name)
        # arrow: (params) => ... or ident => ...
        q = j
        if toks[q].text == "(":
            q = match_close(toks, q) + 1
        elif toks[q].kind == "ident" and q + 1 < n and toks[q + 1].text == "=>":
            q = q + 1
        else:
            if toks[q].kind == "ident" and q + 1 < n and toks[q + 1].text in (";",):
                return _ModelFn("ident", t.start, toks[q].end, toks[q].end, toks[q + 1].end, toks[q].text)
            if toks[q].kind == "ident" and (q + 1 >= n or toks[q + 1].line_break_before):
                return _ModelFn("ident", t.start, toks[q].end, toks[q].end, toks[q].end, toks[q].text)
            return None
        if q >= n or toks[q].text != "=>":
            return None
        if q + 1 < n and toks[q + 1].text == "{":
            bclose = match_close(toks, q + 1)
            end = toks[bclose].end
            stmt_end = toks[bclose + 1].end if bclose + 1 < n and toks[bclose + 1].text == ";" else end
            return _ModelFn("arrow", t.start, toks[k + 1].end, end, stmt_end, None)
        r = q + 1
        stops = {"function", "export", "const", "let", "var", "class", "import"}
        while r < n and not (toks[r].depth == 0 and (toks[r].text == ";" or (toks[r].line_break_before and toks[r].text in stops))):
            r += 1
        end = toks[r - 1].end
        stmt_end = toks[r].end if r < n and toks[r].text == ";" else end
        return _ModelFn("arrow", t.start, toks[k + 1].end, end, stmt_end, None)
    # no default export: a top-level 'function generate(' is taken as the model function
    for k, t in enumerate(toks):
        if t.kind == "ident" and t.text == "function" and t.depth == 0 and k + 2 < n \
                and toks[k + 1].text == "generate" and toks[k + 2].text == "(":
            return _ModelFn("bare", None, toks[k + 1].end, toks[k + 1].end, toks[k + 1].end, "generate")
    return None


def _rename_refs(src: str, name: str, new: str, skip_start: int | None = None) -> str:
    toks = scan(src)
    edits = [(t.start, t.end, new) for k, t in enumerate(toks)
             if t.kind == "ident" and t.text == name and is_ref(toks, k) and t.start != skip_start]
    return _apply(src, edits)


def restructure(src: str, *, fit_wrapper: bool, fit_target_milli: int, need_rand: bool,
                version: str = REPAIR_VERSION) -> tuple[str | None, list[str]]:
    toks = scan(src)
    fn = _find_model_fn(src, toks)
    rules: list[str] = []
    if fn is None:
        return None, ["no_model_function"]
    if not fit_wrapper:
        if fn.kind not in ("function", "arrow") or fn.export_start is None:
            return None, ["hoist_needs_export_default_function"]
        export_stmt = src[fn.export_start:fn.stmt_end]
        tail = src[fn.stmt_end:]
        out = src[:fn.export_start]
        if tail.strip():
            out = out + tail.strip("\n") + "\n\n"
            rules.append("hoist_tail_above_export")
        out = out + export_stmt + "\n"
        if need_rand:
            out = RAND_HELPER + "\n" + out
        return normalise(out), rules
    # fit-wrapper mode
    if fn.kind == "function":
        name = fn.name
        new_src = src[:fn.export_start] + "function __sn17_model" + src[fn.head_end:]
        if name:
            # the declaration's own name token was replaced; rename remaining references (recursion etc.)
            new_src = _rename_refs(new_src, name, "__sn17_model")
        rules.append("export_to_model_fn")
    elif fn.kind == "arrow":
        body = src[fn.head_end:fn.body_end].strip()
        new_src = src[:fn.export_start] + "const __sn17_model = " + body + ";" + src[fn.stmt_end:]
        rules.append("arrow_export_to_model_const")
    elif fn.kind == "ident":
        name = fn.name or ""
        new_src = src[:fn.export_start] + src[fn.stmt_end:]
        new_src = _rename_refs(new_src, name, "__sn17_model")
        rules.append("ident_export_to_model_fn")
    else:  # bare 'function generate('
        new_src = _rename_refs(src, "generate", "__sn17_model")
        rules.append("bare_generate_to_model_fn")
    # our wrapper is named 'generate'; any remaining user binding of that name would collide
    re_toks = scan(new_src)
    if any(t.kind == "ident" and t.text == "generate" and is_ref(re_toks, k) for k, t in enumerate(re_toks)):
        new_src = _rename_refs(new_src, "generate", "__sn17_user_generate")
        rules.append("rename_user_generate")
    helpers = []
    if need_rand:
        helpers.append(RAND_HELPER)
    target = f"{fit_target_milli // 1000}.{fit_target_milli % 1000:03d}"
    helpers.append(FIT_HELPER.replace("__FIT__", target))
    out = (
        new_src.rstrip("\n")
        + "\n\n// --- sn17miner " + version + ": fit the returned object to the unit cube (deterministic) ---\n"
        + "\n".join(helpers)
        + "\n"
        + WRAPPER
    )
    rules.append("fit_wrapper")
    return normalise(out), rules


# ----------------------------------------------------------------------------------- structural check
def structural_check(js: str, version: str = REPAIR_VERSION) -> list[str]:
    if version not in REPAIR_VERSIONS:
        raise ValueError(f"unknown repair version {version!r}")
    reasons: list[str] = []
    if len(js.encode("utf-8")) > MAX_FILE_BYTES:
        reasons.append("file_too_large")
    try:
        toks = scan(js)
    except ScanError as e:
        return reasons + [f"scan_error:{e}"]
    n = len(toks)
    exports = [k for k, t in enumerate(toks)
               if t.kind == "ident" and t.text == "export" and t.depth == 0 and is_ref(toks, k)]
    defaults = [k for k in exports if k + 1 < n and toks[k + 1].text == "default"]
    if len(defaults) != 1:
        reasons.append(f"default_exports:{len(defaults)}")
    if len(exports) != len(defaults):
        reasons.append("extra_exports")
    for k in defaults:
        if k + 2 < n and toks[k + 2].text == "async":
            reasons.append("async_default_export")
    forbidden = set(FORBIDDEN_IDENTIFIERS)
    seen: list[str] = []
    for k, t in enumerate(toks):
        if t.kind != "ident":
            continue
        if t.text in ("async", "await") and is_ref(toks, k):
            seen.append("async_or_await")
        elif t.text == "import" and is_ref(toks, k):
            seen.append("import")
        elif t.text in forbidden and is_ref(toks, k):
            seen.append(f"forbidden_identifier:{t.text}")
        elif t.text == "random" and k >= 2 and toks[k - 1].text == "." and toks[k - 2].text == "Math":
            seen.append("math_random")
    if version != "r1" and _unary_pow_sites(toks):
        seen.append("unary_before_exponent")    # r2: a SyntaxError in JS (-x ** y); r1 never checked it
    elif version == "r3" and _unary_pow_sites_r3_extra(toks):
        seen.append("unary_before_exponent")    # r3: also after if(...)/blocks and before `new` operands (C2)
    for s in seen:
        if s not in reasons:
            reasons.append(s)
    return reasons


# ----------------------------------------------------------------------------------- entry point
def repair(text: str, *, fit_wrapper: bool = True, fit_target_milli: int = 970,
           version: str = REPAIR_VERSION) -> RepairResult:
    if version not in REPAIR_VERSIONS:
        raise ValueError(f"unknown repair version {version!r}")
    raw, how = extract_js(text)
    if raw is None:
        return RepairResult(None, False, [how], [], how)
    js = normalise(raw)
    rules: list[str] = []
    try:
        js, r1 = token_rewrites(js)
        rules += r1
        if version in ("r2", "r3"):
            js, rp = fix_unary_pow(js)
            rules += rp
        out, r2 = restructure(js, fit_wrapper=fit_wrapper, fit_target_milli=fit_target_milli,
                              need_rand="math_random" in r1, version=version)
    except ScanError as e:
        return RepairResult(normalise(js), False, [f"scan_error:{e}"], rules, how)
    if out is None:
        return RepairResult(normalise(js), False, r2, rules, how)
    rules += r2
    reasons = structural_check(out, version)
    return RepairResult(out, not reasons, reasons, rules, how)


def model_source(text: str) -> str | None:
    """The module source the model wrote in a reply (extraction + newline normalisation only, before any repair
    rule). The refine call (pipeline v2) sends this back as "the current module". Pure function of the text."""
    raw, _ = extract_js(text)
    return normalise(raw) if raw is not None else None


def fallback_module(stem: str, version: str = REPAIR_VERSION) -> str:
    """Guaranteed-valid placeholder (validated by the production validator in tests/test_gate.py).
    Neutral grey 0.8 box: without the image bytes (R10: the pipeline never downloads the prompt image)
    no colour can be derived; the stem appears only in a comment, so bytes differ per prompt."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", stem)[:80]
    return (
        f"// sn17miner fallback ({version}) stem={safe}: neutral placeholder, emitted only when no\n"
        "// candidate passed the production validator.\n"
        "export default function generate(THREE) {\n"
        "  const group = new THREE.Group();\n"
        "  const material = new THREE.MeshStandardMaterial({ color: 0x8c8c8c, roughness: 0.6, metalness: 0.1 });\n"
        "  const body = new THREE.Mesh(new THREE.BoxGeometry(0.8, 0.8, 0.8), material);\n"
        "  group.add(body);\n"
        "  return group;\n"
        "}\n"
    )
