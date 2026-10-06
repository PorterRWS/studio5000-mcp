"""Best-effort offline cross-reference over detailed L5X exports.

Studio's Cross Reference engine lives in native Designer ``xref.dll`` and is
not exposed by Logix Designer SDK. This module approximates the useful parts:

* RLL rung operand hits (token-aware, not naive substring)
* Structured Text line hits
* Tag definitions + AliasFor chains
* Module / connection name hits
* Heuristic destructive vs non-destructive classification for common RLL ops

It will not perfectly match Designer for FBD/SFC wire refs, locked "Source not
available" routines, or verified ST destructive flags.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from logix_mcp._xml import _local, _scope_from_ancestor_chain


# Instructions that only examine operands (non-destructive).
_READ_ONLY: frozenset[str] = frozenset(
    {
        "XIC",
        "XIO",
        "EQU",
        "NEQ",
        "GRT",
        "GEQ",
        "LES",
        "LEQ",
        "LIM",
        "MEQ",
        "CMP",
        "JMP",
        "LBL",
        "JSR",  # argument direction is not mapped; origin trace lists JSR as a gap
        "RET",
        "SBR",
        "AFI",
        "NOP",
        "UID",
        "UIE",
        "EVENT",
        "TND",
        "MCR",
    }
)

# Operand position of the written tag. Unknown instructions stay out of these
# sets so classification returns "?" instead of a guess.
_DEST_FIRST: frozenset[str] = frozenset(
    {"OTE", "OTL", "OTU", "CLR", "CPT", "TON", "TOF", "RTO", "CTU", "CTD", "RES"}
)
_DEST_SECOND: frozenset[str] = frozenset({"COP", "CPS", "FLL"})
_DEST_LAST: frozenset[str] = frozenset(
    {
        "MOV",
        "MVM",
        "ADD",
        "SUB",
        "MUL",
        "DIV",
        "MOD",
        "SQR",
        "NEG",
        "NOT",
        "AND",
        "OR",
        "XOR",
        "TOD",
        "FRD",
        "DEG",
        "RAD",
        "SIN",
        "COS",
        "TAN",
        "ASN",
        "ACS",
        "ATN",
        "LN",
        "LOG",
        "XPY",
        "SWPB",
        "SIZE",
        "FIND",
        "MID",
        "CONCAT",
        "DELETE",
        "INSERT",
        "GSV",
    }
)
_DEST_FIRST_TWO: frozenset[str] = frozenset({"OSR", "OSF"})


def _dest_indexes(instr: str, nargs: int) -> list[int] | None:
    """Indexes of operands this instruction writes.

    ``[]`` means the instruction does not write its operands.
    ``None`` means the instruction is not classified.
    """
    if instr in _DEST_FIRST_TWO:
        return [i for i in (0, 1) if i < nargs]
    if instr in _DEST_FIRST:
        return [0] if nargs else []
    if instr in _DEST_SECOND:
        return [1] if nargs >= 2 else ([0] if nargs else [])
    if instr in _DEST_LAST:
        return [nargs - 1] if nargs else []
    if instr in _READ_ONLY:
        return []
    return None


_INSTR_START = re.compile(r"\b([A-Z][A-Z0-9_]*)\(")


@dataclass(frozen=True)
class XrefHit:
    kind: str  # logic | st | alias | definition | module | connection
    program: str
    routine: str
    location: str
    instruction: str
    destructive: str  # Y | N | ?
    reference: str
    snippet: str


def _token_pattern(name: str) -> re.Pattern[str]:
    """Match ``name``, ``name.Member``, ``name[0]``, ``\\Prog.name``, etc.

    Avoids matching ``Tag10`` when searching ``Tag1``.
    """
    n = re.escape(name.strip())
    # Optional program qualifier: \Prog. or Prog.
    qual = rf"(?:\\?[A-Za-z_][\w]*\.)?"
    # Member / bit / index chain after the base name
    trail = r"(?:\.[A-Za-z_][\w]*|\[[^\]]+\])*"
    return re.compile(
        rf"(?<![A-Za-z0-9_]){qual}{n}{trail}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )


def _balanced_close(text: str, open_paren_idx: int) -> int:
    """Index of matching ``)`` for ``(`` at ``open_paren_idx``, or -1."""
    depth = 0
    for i in range(open_paren_idx, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _instruction_at(text: str, pos: int) -> tuple[str, str, list[str]]:
    """Return (instr, full_call, args) for the innermost call covering ``pos``."""
    covering: list[tuple[int, str, str, list[str]]] = []
    for m in _INSTR_START.finditer(text):
        open_i = m.end() - 1
        close_i = _balanced_close(text, open_i)
        if close_i < 0:
            continue
        if not (m.start() <= pos < close_i):
            continue
        body = text[open_i + 1 : close_i]
        args = _split_args(body)
        covering.append((close_i - m.start(), m.group(1).upper(), text[m.start() : close_i + 1], args))
    if not covering:
        return "", "", []
    covering.sort(key=lambda t: t[0])  # smallest span = innermost
    _, instr, call, args = covering[0]
    return instr, call, args


def _split_args(body: str) -> list[str]:
    """Split instruction args on top-level commas (respect nested [] / () )."""
    args: list[str] = []
    buf: list[str] = []
    depth_paren = 0
    depth_brack = 0
    for ch in body:
        if ch == "(":
            depth_paren += 1
            buf.append(ch)
        elif ch == ")":
            depth_paren -= 1
            buf.append(ch)
        elif ch == "[":
            depth_brack += 1
            buf.append(ch)
        elif ch == "]":
            depth_brack -= 1
            buf.append(ch)
        elif ch == "," and depth_paren == 0 and depth_brack == 0:
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        args.append("".join(buf).strip())
    return args


def _classify_destructive(instr: str, args: list[str], matched: str) -> str:
    if not instr:
        return "?"
    indexes = _dest_indexes(instr, len(args))
    if indexes is None:
        return "?"
    matched_l = matched.lower()
    args_l = [a.lower() for a in args]

    def _arg_is_match(a: str) -> bool:
        # arg may be ``Tag.Member``; matched may be full reference text
        return a == matched_l or a.startswith(matched_l + ".") or matched_l.startswith(a)

    dest = set(indexes)
    saw = False
    for i, arg in enumerate(args_l):
        if not _arg_is_match(arg):
            continue
        saw = True
        if i in dest:
            return "Y"
    if saw or not dest:
        return "N"
    return "?"


def _rung_plain_text(el) -> tuple[str, str]:
    text, comment = "", ""
    for ch in el:
        tag = _local(ch.tag)
        if tag == "Text" and ch.text:
            text = ch.text.strip()
        elif tag == "Comment" and ch.text:
            comment = ch.text.strip()
    if not text:
        text = " ".join(t for t in el.itertext() if t and t.strip())[:2000]
    return text, comment


def _iter_logic_hits(root, pat: re.Pattern[str]) -> Iterable[XrefHit]:
    for el in root.iter():
        if _local(el.tag) != "Rung":
            continue
        text, comment = _rung_plain_text(el)
        blob = text
        prog, rout = _scope_from_ancestor_chain(el)
        num = el.get("Number", "?")
        seen_spans: set[tuple[int, int]] = set()
        for m in pat.finditer(blob):
            span = m.span()
            if span in seen_spans:
                continue
            seen_spans.add(span)
            ref = m.group(0)
            instr, call, args = _instruction_at(blob, m.start())
            dest = _classify_destructive(instr, args, ref)
            snip = call if call else blob[max(0, m.start() - 40) : m.end() + 40]
            yield XrefHit(
                kind="logic",
                program=prog,
                routine=rout,
                location=f"Rung {num}",
                instruction=instr or "-",
                destructive=dest,
                reference=ref,
                snippet=snip[:240],
            )
        if comment:
            for m in pat.finditer(comment):
                yield XrefHit(
                    kind="logic",
                    program=prog,
                    routine=rout,
                    location=f"Rung {num} comment",
                    instruction="-",
                    destructive="N",
                    reference=m.group(0),
                    snippet=comment[:240],
                )


def _iter_st_hits(root, pat: re.Pattern[str]) -> Iterable[XrefHit]:
    for el in root.iter():
        if _local(el.tag) != "Routine":
            continue
        if (el.get("Type") or "").upper() not in ("ST", "STRUCTUREDTEXT"):
            # Still scan STContent if present under mixed routines
            pass
        prog, rout = _scope_from_ancestor_chain(el)
        # ST line containers vary: LineText, Text, STContent/Line
        for node in el.iter():
            tag = _local(node.tag)
            if tag not in ("LineText", "Text", "Comment"):
                continue
            # Skip RLL rung text already handled
            parent = node.getparent()
            if parent is not None and _local(parent.tag) == "Rung":
                continue
            body = (node.text or "").strip()
            if not body:
                continue
            line_no = node.get("Number") or parent.get("Number") if parent is not None else "?"
            for m in pat.finditer(body):
                yield XrefHit(
                    kind="st",
                    program=prog,
                    routine=rout,
                    location=f"ST line {line_no}",
                    instruction="ST",
                    destructive="?",
                    reference=m.group(0),
                    snippet=body[:240],
                )


def _iter_alias_and_defs(root, name: str, pat: re.Pattern[str]) -> Iterable[XrefHit]:
    name_l = name.strip().lower()
    for el in root.iter():
        if _local(el.tag) != "Tag":
            continue
        tag_name = el.get("Name") or ""
        alias_for = el.get("AliasFor") or ""
        dt = el.get("DataType") or ""
        tag_type = el.get("TagType") or ""
        prog, _ = _scope_from_ancestor_chain(el)
        # Definition of the searched name
        if tag_name.lower() == name_l:
            yield XrefHit(
                kind="definition",
                program=prog if prog != "—" else "Controller",
                routine="-",
                location="Tag definition",
                instruction="-",
                destructive="-",
                reference=tag_name,
                snippet=(
                    f"DataType={dt or '-'}; TagType={tag_type or '-'}; "
                    f"AliasFor={alias_for or '-'}"
                ),
            )
        # This tag aliases to the searched name (or a member path)
        if alias_for and pat.search(alias_for):
            yield XrefHit(
                kind="alias",
                program=prog if prog != "—" else "Controller",
                routine="-",
                location="AliasFor",
                instruction="-",
                destructive="-",
                reference=f"{tag_name} -> {alias_for}",
                snippet=f"{tag_name} aliases {alias_for}",
            )
        # Searched name is itself an alias tag pointing elsewhere
        if tag_name.lower() == name_l and alias_for:
            yield XrefHit(
                kind="alias",
                program=prog if prog != "—" else "Controller",
                routine="-",
                location="Alias chain",
                instruction="-",
                destructive="-",
                reference=f"{tag_name} -> {alias_for}",
                snippet=f"base/path: {alias_for}",
            )


def _iter_module_hits(root, pat: re.Pattern[str]) -> Iterable[XrefHit]:
    for el in root.iter():
        tag = _local(el.tag)
        if tag not in ("Module", "Connection"):
            continue
        name = el.get("Name") or el.get("ConnectionPath") or ""
        catalog = el.get("CatalogNumber") or el.get("Type") or ""
        if not name and not catalog:
            continue
        blob = f"{name} {catalog}"
        if not pat.search(blob) and not pat.search(name):
            # also scan child text briefly
            child = " ".join(t for t in el.itertext() if t and t.strip())[:400]
            if not pat.search(child):
                continue
            blob = child
        m = pat.search(blob) or pat.search(name)
        ref = m.group(0) if m else name
        yield XrefHit(
            kind="module" if tag == "Module" else "connection",
            program="I/O",
            routine="-",
            location=tag,
            instruction="-",
            destructive="-",
            reference=ref,
            snippet=f"{name} ({catalog})"[:240],
        )


def cross_reference_l5x(
    root,
    name: str,
    *,
    program: str = "",
    include_aoi: bool = True,
    include_modules: bool = True,
    max_hits: int = 500,
) -> tuple[list[XrefHit], dict[str, int]]:
    """Run offline xref. Returns (hits, counts_by_kind)."""
    needle = (name or "").strip()
    if not needle:
        raise ValueError("name is empty; pass a tag / symbol to cross-reference")
    if max_hits < 1:
        max_hits = 1
    pat = _token_pattern(needle)
    prog_filter = (program or "").strip().lower()

    hits: list[XrefHit] = []
    for hit in _iter_alias_and_defs(root, needle, pat):
        hits.append(hit)
    for hit in _iter_logic_hits(root, pat):
        hits.append(hit)
    for hit in _iter_st_hits(root, pat):
        hits.append(hit)
    if include_modules:
        for hit in _iter_module_hits(root, pat):
            hits.append(hit)

    filtered: list[XrefHit] = []
    for h in hits:
        if not include_aoi and h.program.startswith("AOI:"):
            continue
        if prog_filter:
            # Allow filter on program or AOI:Name
            pl = h.program.lower()
            if prog_filter not in pl and pl != prog_filter:
                continue
        filtered.append(h)

    # Stable order: definitions/aliases first, then logic by program/routine/location
    kind_order = {"definition": 0, "alias": 1, "logic": 2, "st": 3, "module": 4, "connection": 5}

    def _sort_key(h: XrefHit) -> tuple:
        return (
            kind_order.get(h.kind, 9),
            h.program,
            h.routine,
            h.location,
            h.reference,
        )

    filtered.sort(key=_sort_key)
    counts: dict[str, int] = {}
    for h in filtered:
        counts[h.kind] = counts.get(h.kind, 0) + 1
    return filtered[:max_hits], counts


def format_xref_report(
    path: str,
    name: str,
    hits: list[XrefHit],
    counts: dict[str, int],
    truncated: bool,
) -> str:
    if not hits:
        return (
            f"[OK] cross_reference: no references to {name!r} in {path}\n"
            "Note: offline L5X xref — FBD/SFC wire refs and some ST cases may be missed."
        )
    headers = (
        "Kind",
        "Program",
        "Routine",
        "Location",
        "Instr",
        "Dest?",
        "Reference",
        "Snippet",
    )
    rows = [
        (
            h.kind,
            h.program,
            h.routine,
            h.location,
            h.instruction,
            h.destructive,
            h.reference,
            h.snippet.replace("\n", " "),
        )
        for h in hits
    ]
    # Column widths
    w = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            w[i] = max(w[i], min(len(cell), 60 if i < 7 else 80))

    def _cell(s: str, i: int) -> str:
        s = s if len(s) <= w[i] else s[: w[i] - 1] + "…"
        return s.ljust(w[i])

    sep = " | "
    lines = [
        sep.join(headers[i].ljust(w[i]) for i in range(len(headers))),
        "-+-".join("-" * w[i] for i in range(len(headers))),
    ]
    for r in rows:
        lines.append(sep.join(_cell(r[i], i) for i in range(len(headers))))

    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    more = "\n... additional hits truncated (raise max_hits)." if truncated else ""
    return (
        f"[OK] cross_reference for {name!r} in {path}\n"
        f"Hits shown: {len(hits)} ({summary})\n"
        "Dest? Y=likely write, N=read/examine, ?=uncertain, -=n/a\n"
        "Engine: offline L5X (not Studio xref.dll)\n"
        + "\n".join(lines)
        + more
    )
