"""lxml helpers for L5X parsing and rendering.

Moved verbatim from the legacy single-file `l5x_acd_server.py`. Phase 2 tool
modules depend on these functions behaving exactly the way they did before,
so do not refactor them here without coordinating with the per-area tools.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from lxml import etree

from logix_mcp._common import _opened, _resolve


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


async def _l5x_tree_for_path(path: Path) -> etree._ElementTree:
    if path.suffix.lower() == ".l5x":
        return etree.parse(str(path))
    td = tempfile.mkdtemp(prefix="logix_mcp_")
    out = Path(td) / "_inspect.L5X"
    try:
        async with _opened(path) as proj:
            await proj.save_as(str(out), force=True, detailed_l5x=True)
        return etree.parse(str(out))
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _tag_rows(root) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for el in root.iter():
        if _local(el.tag) != "Tag":
            continue
        name = el.get("Name", el.get("AliasFor", ""))
        dt = el.get("DataType", "")
        scope = el.get("ExternalAccess", "") or el.get("TagType", "")
        val = ""
        for ch in el.iter():
            if _local(ch.tag) == "Data" and ch.text:
                val = (ch.text or "").strip()[:80]
                break
        if not val and el.text:
            val = (el.text or "").strip()[:80]
        rows.append((name, dt, scope or "—", val or "—"))
    return rows


def _scope_from_ancestor_chain(el) -> tuple[str, str]:
    """Resolve Program (or AOI) and Routine names by walking up (L5X order varies)."""
    prog, rout = "—", "—"
    p: etree._Element | None = el
    while p is not None:
        t = _local(p.tag)
        if t == "Routine" and rout == "—":
            rout = p.get("Name", "?")
        elif t in ("Program", "SafetyProgram") and prog == "—":
            prog = p.get("Name", "?")
        elif t in ("AddOnInstructionDefinition", "AddOnInstruction") and prog == "—":
            prog = "AOI:" + p.get("Name", "?")
        p = p.getparent()  # type: ignore[assignment]
    return prog, rout


def _rung_count_for_routine(routine_el) -> int:
    """Count Rungs that belong to this Routine (Rungs may be under RLLContent, SFC, etc.)."""
    n = 0
    for node in routine_el.iter():
        if _local(node.tag) != "Rung":
            continue
        r = node
        while r is not None and _local(r.tag) != "Routine":
            r = r.getparent()  # type: ignore[assignment]
        if r is routine_el:
            n += 1
    return n


def _routine_rows(root) -> list[tuple[str, str, str, int]]:
    out: list[tuple[str, str, str, int]] = []
    for el in root.iter():
        if _local(el.tag) != "Routine":
            continue
        prog, rname = _scope_from_ancestor_chain(el)
        typ = (el.get("Type") or "").strip()
        rc = _rung_count_for_routine(el)
        rtype = typ or ("RLL" if rc else "—")
        out.append((prog, rname, rtype, rc))
    return out


def _rung_text(el) -> str:
    parts: list[str] = []
    for ch in el.iter():
        if _local(ch.tag) in ("Text", "Comment") and ch.text:
            parts.append(ch.text)
    if parts:
        return " ".join(parts)
    return " ".join(t for t in el.itertext() if t and t.strip())[:500]


def _search_rungs(root, needle: str) -> list[str]:
    needle_l = needle.lower()
    hits: list[str] = []
    for el in root.iter():
        if _local(el.tag) != "Rung":
            continue
        txt = _rung_text(el)
        if needle_l not in txt.lower():
            continue
        prog, rout = _scope_from_ancestor_chain(el)
        num = el.get("Number", "?")
        hits.append(f"{prog} / {rout} / Rung {num}: {txt[:300]}")
    return hits


def _get_rung(root, program: str, routine: str, number: str | int) -> str | None:
    """Return full text for one rung, or None if it is not found."""
    prog_l = (program or "").strip().lower()
    rout_l = (routine or "").strip().lower()
    num = str(number).strip()
    if not prog_l or not rout_l or not num:
        return None
    for el in root.iter():
        if _local(el.tag) != "Rung":
            continue
        if str(el.get("Number", "")).strip() != num:
            continue
        prog, rout = _scope_from_ancestor_chain(el)
        if prog.lower() != prog_l or rout.lower() != rout_l:
            continue
        owner = el
        while owner is not None and _local(owner.tag) != "Routine":
            owner = owner.getparent()
        # Prefer the matching routine name already checked via scope.
        text = (_rung_text(el) or "").strip()
        return text or None
    return None


def _fmt_table(rows: list[tuple[str, str, str, str]], headers: tuple[str, ...]) -> str:
    if not rows:
        return "(no tags found in L5X export)"
    w = [max(len(h), max((len(r[i]) for r in rows), default=0)) for i, h in enumerate(headers)]
    sep = " | "
    head = sep.join(h.ljust(w[i]) for i, h in enumerate(headers))
    bar = "-+-".join("-" * w[i] for i in range(len(headers)))
    lines = [head, bar]
    for r in rows[:2000]:
        lines.append(sep.join(str(r[i]).ljust(w[i]) for i in range(len(headers))))
    if len(rows) > 2000:
        lines.append(f"... ({len(rows) - 2000} more rows omitted)")
    return "\n".join(lines)


def _l5x_quick_open_summary(p: Path) -> str:
    """Read Controller metadata without LogixProject.open (avoids long SDK loads on big L5X)."""
    size_mb = p.stat().st_size / (1024 * 1024)
    cname = ptype = target = "—"
    try:
        # huge_tree: parse large L5X without blowing memory; clear elements as we go
        for _event, el in etree.iterparse(str(p), events=("end",), huge_tree=True):
            if _local(el.tag) == "Controller":
                cname = el.get("Name", "—")
                ptype = el.get("ProcessorType", "—")
                target = el.get("TargetName", "—")
                el.clear()
                break
            el.clear()
    except etree.XMLSyntaxError as e:
        return f"Invalid L5X: {e}"
    return (
        f"L5X quick view (no full SDK open): {p}\n"
        f"File size: {size_mb:.1f} MB\n"
        f"Controller Name: {cname}\n"
        f"ProcessorType: {ptype}\n"
        f"TargetName: {target}\n"
        "Default for .L5X skips the SDK to prevent hangs on large exports.\n"
        "To open in the full Logix API (get CommunicationsPath, etc.), use:\n"
        "  open_project(path, sdk_open=True)  — this can take many minutes for big files."
    )


def re_int(s: str) -> bool:
    s = s.strip()
    return s.isdigit() or (s.startswith("-") and s[1:].isdigit())


def re_float(s: str) -> bool:
    try:
        float(s.strip())
        return True
    except ValueError:
        return False


def _resolve_l5x_input(l5x_path: str, l5x_text: str) -> tuple[str, Path | None]:
    """Return file path to L5X and optional temp path to delete."""
    t = (l5x_text or "").strip()
    if t:
        fd, name = tempfile.mkstemp(suffix=".L5X", prefix="logiximp_")
        os.close(fd)
        tmp = Path(name)
        tmp.write_text(t, encoding="utf-8", newline="")
        return str(tmp), tmp
    p = (l5x_path or "").strip()
    if not p:
        raise ValueError("Provide l5x_path to an L5X file or l5x_text (XML body).")
    return str(_resolve(p)), None
