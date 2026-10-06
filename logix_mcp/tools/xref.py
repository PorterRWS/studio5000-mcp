"""Offline cross-reference tool (L5X-based approximate of Studio Cross Reference)."""
from __future__ import annotations

from logix_mcp._common import _resolve, _run, mcp, preflight_project_path
from logix_mcp._origin import trace_origin_l5x
from logix_mcp._xml import _l5x_tree_for_path
from logix_mcp._xref import cross_reference_l5x, format_xref_report


@mcp.tool()
async def cross_reference(
    path: str,
    name: str,
    program: str = "",
    include_aoi: bool = True,
    include_modules: bool = True,
    max_hits: int = 500,
) -> str:
    """Offline cross-reference for a tag/symbol via detailed L5X export.

    Returns definition / AliasFor links, RLL rung hits (token-aware), ST line
    hits, and optional module/connection name hits. ``Dest?`` is a heuristic
    (Y/N/?) for common RLL instructions — not Studio's verified destructive
    flag. Does **not** use Designer ``xref.dll`` (unavailable via SDK).

    Args:
        path: ``.ACD`` / ``.L5X`` / ``.L5K`` project path.
        name: Tag or symbol to find (e.g. ``BT_CipActive``, ``Batch_Start``).
        program: Optional program/AOI name filter (substring, case-insensitive).
        include_aoi: Include hits inside Add-On Instruction definitions.
        include_modules: Also scan I/O Module / Connection names.
        max_hits: Cap on returned rows (default 500).
    """
    pf = preflight_project_path(path)
    if pf:
        return pf

    async def _do() -> str:
        needle = (name or "").strip()
        if not needle:
            raise ValueError("name is empty; pass a tag / symbol to cross-reference")
        p = _resolve(path)
        tree = await _l5x_tree_for_path(p)
        cap = max(1, int(max_hits))
        all_hits, all_counts = cross_reference_l5x(
            tree.getroot(),
            needle,
            program=program,
            include_aoi=include_aoi,
            include_modules=include_modules,
            max_hits=10**9,
        )
        truncated = len(all_hits) > cap
        hits = all_hits[:cap]
        return format_xref_report(str(p), needle, hits, all_counts, truncated)

    return await _run(
        "cross_reference",
        _do,
        path=path,
        name=name,
        program=program,
        include_aoi=include_aoi,
        include_modules=include_modules,
        max_hits=max_hits,
    )


@mcp.tool()
async def trace_origin(
    path: str,
    name: str,
    program: str = "",
    max_depth: int = 8,
    max_origins: int = 50,
    format: str = "both",
    diagram: bool = True,
    focus: str = "",
    from_box: str = "",
) -> str:
    """Trace a tag backward to the sources that write it.

    Starts at ``name``, finds ladder instructions that write it, and follows
    the tags that feed each write (source operands and the contacts that gate
    it) until a literal or a tag this project never writes. A CLR or a
    literal 0 ends that branch: the rungs before it stay, and the clear is
    named in the text instead of drawn. Every such path is returned, plus a
    Mermaid flowchart and a JSON structure of boxes/edges. Tag descriptions
    and bit comments from the L5X are included. This is an offline L5X
    approximation, not Designer xref.dll.

    For a follow-up on one tag in the tree: ``focus`` keeps paths from the
    start tag to each box that writes that tag (one box per writer branch).
    It does not expand origins below the focus. ``from_box`` starts the
    diagram at that box (without the original start-tag root) and keeps the
    origin boxes it links to. Both include full rung text. Use ``get_rung`` /
    ``get_rung_diagram`` for one program/routine/number. Set
    ``diagram=false`` on large trees. ``format`` is ``text``, ``json``, or
    ``both`` (default).

    A file Designer has open must be copied first (``copy_project``); the SDK
    cannot open the locked ACD. Structured text, FBD/SFC, JSR, and unknown
    instructions are listed as gaps and are not followed.

    Args:
        path: ``.ACD`` / ``.L5X`` / ``.L5K`` project path.
        name: Tag or symbol to trace (for example ``Program_Select`` or ``Word.3``).
        program: Scope to use when the same name exists in more than one program.
        max_depth: How many feeder tags to follow past the start tag (default 8).
        max_origins: Cap on origin paths returned (default 50).
        format: ``text``, ``json``, or ``both`` (default ``both``).
        diagram: Include the Mermaid flowchart (default true; ignored for ``json``).
        focus: Keep paths from the start tag to boxes that write this tag.
        from_box: Start the diagram at this box id (omit the original start tag).
    """
    pf = preflight_project_path(path)
    if pf:
        return pf

    async def _do() -> str:
        needle = (name or "").strip()
        if not needle:
            raise ValueError("name is empty; pass a tag / symbol to trace")
        p = _resolve(path)
        tree = await _l5x_tree_for_path(p)
        return trace_origin_l5x(
            tree.getroot(),
            needle,
            program=program,
            max_depth=max_depth,
            max_origins=max_origins,
            project_path=str(p),
            format=format,
            diagram=diagram,
            focus=focus,
            from_box=from_box,
        )

    return await _run(
        "trace_origin",
        _do,
        path=path,
        name=name,
        program=program,
        max_depth=max_depth,
        max_origins=max_origins,
        format=format,
        diagram=diagram,
        focus=focus,
        from_box=from_box,
    )


__all__ = ["cross_reference", "trace_origin"]
