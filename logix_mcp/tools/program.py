"""Program-structure tools: routines, rung search/read/diagram, executables (gated)."""
from __future__ import annotations

from logix_mcp._common import (
    _gated_tool_response,
    _has_method,
    _opened,
    _resolve,
    _run,
    mcp,
    preflight_project_path,
)
from logix_mcp._ladder import render_rung_diagram
from logix_mcp._origin import (
    format_rung_tag_table,
    rung_description_lookup,
    rung_tag_descriptions,
)
from logix_mcp._xml import (
    _fmt_table,
    _get_rung,
    _l5x_tree_for_path,
    _routine_rows,
    _search_rungs,
)


@mcp.tool()
async def list_routines(path: str) -> str:
    """List Programs / Routines / rung counts via a temp detailed L5X export."""
    pf = preflight_project_path(path)
    if pf:
        return pf

    async def _do() -> str:
        p = _resolve(path)
        tree = await _l5x_tree_for_path(p)
        rows = _routine_rows(tree.getroot())
        str_rows: list[tuple[str, str, str, str]] = [
            (prog, name, rtype, str(rcount)) for prog, name, rtype, rcount in rows
        ]
        table = _fmt_table(
            str_rows, ("Program", "Routine", "Type", "RungCount")
        )
        return f"[OK] Routines for {p}:\n{table}"

    return await _run("list_routines", _do, path=path)


@mcp.tool()
async def search_rungs(path: str, text: str) -> str:
    """Search rung text + comments for ``text`` (case-insensitive) via temp L5X."""
    pf = preflight_project_path(path)
    if pf:
        return pf

    async def _do() -> str:
        if not text or not text.strip():
            raise ValueError("text is empty; provide a search needle")
        p = _resolve(path)
        tree = await _l5x_tree_for_path(p)
        hits = _search_rungs(tree.getroot(), text)
        if not hits:
            return f"[OK] No rungs matched {text!r} in {p}"
        body = "\n".join(hits[:500])
        more = "" if len(hits) <= 500 else f"\n... ({len(hits) - 500} more hits omitted)"
        return f"[OK] {len(hits)} hit(s) for {text!r} in {p}:\n{body}{more}"

    return await _run("search_rungs", _do, path=path, text=text)


@mcp.tool()
async def get_rung(path: str, program: str, routine: str, number: str) -> str:
    """Return the full neutral text of one ladder rung.

    Use after ``trace_origin`` or ``cross_reference`` when a box or hit names a
    program, routine, and rung number and you need the complete instruction
    text (not the truncated snippet).

    Args:
        path: ``.ACD`` / ``.L5X`` / ``.L5K`` project path.
        program: Program (or AOI) that owns the routine.
        routine: Routine name.
        number: Rung number as shown in Studio / L5X (for example ``0`` or ``41``).
    """
    pf = preflight_project_path(path)
    if pf:
        return pf

    async def _do() -> str:
        prog = (program or "").strip()
        rout = (routine or "").strip()
        num = str(number or "").strip()
        if not prog or not rout or not num:
            raise ValueError("program, routine, and number are required")
        p = _resolve(path)
        tree = await _l5x_tree_for_path(p)
        text = _get_rung(tree.getroot(), prog, rout, num)
        if text is None:
            return f"[FAIL] No rung {num} in {prog}/{rout} for {p}"
        return f"[OK] {prog}/{rout} rung {num} in {p}:\n{text}"

    return await _run(
        "get_rung",
        _do,
        path=path,
        program=program,
        routine=routine,
        number=number,
    )


@mcp.tool()
async def get_rung_diagram(
    path: str,
    program: str,
    routine: str,
    number: str,
    format: str = "both",
) -> str:
    """Draw one ladder rung with Studio-like instruction symbols.

    Parses the rung's neutral text into series and parallel branches, then
    renders contacts (XIC/XIO), coils (OTE/OTL/OTU), specials (ONS/AFI/...),
    and block instructions (MOV/TON/EQU/...) with power rails. L5X tag / bit
    descriptions are soft-wrapped above each instruction (same column width;
    long words hyphen-break) and listed again in a table. Use after
    ``get_rung``, ``trace_origin``, or ``cross_reference`` when you need the
    ladder structure, not only the raw text.

    Args:
        path: ``.ACD`` / ``.L5X`` / ``.L5K`` project path.
        program: Program (or AOI) that owns the routine.
        routine: Routine name.
        number: Rung number as shown in Studio / L5X.
        format: ``text`` / ``ascii`` / ``unicode`` (Unicode ladder),
            ``svg``, or ``both`` (text+svg; default).
    """
    pf = preflight_project_path(path)
    if pf:
        return pf

    async def _do() -> str:
        prog = (program or "").strip()
        rout = (routine or "").strip()
        num = str(number or "").strip()
        if not prog or not rout or not num:
            raise ValueError("program, routine, and number are required")
        p = _resolve(path)
        tree = await _l5x_tree_for_path(p)
        root = tree.getroot()
        text = _get_rung(root, prog, rout, num)
        if text is None:
            return f"[FAIL] No rung {num} in {prog}/{rout} for {p}"
        title = f"{prog}/{rout} rung {num}"
        descs = rung_description_lookup(root, prog, text)
        body = render_rung_diagram(
            text, title=title, format=format, descriptions=descs
        )
        tags = format_rung_tag_table(rung_tag_descriptions(root, prog, text))
        return (
            f"[OK] {title} in {p}\n\nNeutral:\n{text}\n\n{body}\n\n"
            f"Tag descriptions:\n{tags}"
        )

    return await _run(
        "get_rung_diagram",
        _do,
        path=path,
        program=program,
        routine=routine,
        number=number,
        format=format,
    )


@mcp.tool()
async def get_all_executables(path: str) -> str:
    """List every executable element (programs, routines, AOIs) — SDK-gated; needs a newer ``logix_designer_sdk``."""
    if not _has_method("get_all_executables"):
        return _gated_tool_response("get_all_executables")
    pf = preflight_project_path(path)
    if pf:
        return pf

    async def _do() -> str:
        p = _resolve(path)
        async with _opened(p) as proj:
            items = await proj.get_all_executables()
        if not items:
            return f"[OK] get_all_executables({p}) returned 0 entries."
        lines = [f"{i+1:>4}. {item}" for i, item in enumerate(items)]
        return f"[OK] Executables in {p}:\n" + "\n".join(lines)

    return await _run("get_all_executables", _do, path=path)


__all__ = ["list_routines", "search_rungs", "get_rung", "get_rung_diagram", "get_all_executables"]
