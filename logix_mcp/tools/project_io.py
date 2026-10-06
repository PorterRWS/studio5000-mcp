"""Project I/O tools: open / save / export / read / convert / create / processor list.

Every tool is registered against the shared ``mcp`` instance from
``logix_mcp._common`` via ``@mcp.tool()`` so ``logix_mcp.server`` auto-loads
them through its eager-import sweep.

Tool bodies follow the universal pattern:

* run all ``preflight_*`` validators first and short-circuit on rejection,
* wrap the SDK work in ``await _run("<tool_name>", _do)`` so the layered
  error handler converts SDK / OS exceptions into ``[FAIL] ...`` blocks,
* return ``[OK] ...`` strings on success.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import winreg
from pathlib import Path

from logix_designer_sdk import LogixProject  # pyright: ignore[reportMissingImports]

from logix_mcp._common import (
    MCPEventLogger,
    _opened,
    _resolve,
    _run,
    mcp,
    preflight_output_path,
    preflight_project_path,
)
from logix_mcp._xml import _fmt_table, _l5x_quick_open_summary


_PROJECT_EXTS = {".ACD", ".L5X", ".L5K"}


def _program_copies_dir() -> Path:
    """Folder for lock-safe working copies, inside the MCP install root."""
    root = Path(__file__).resolve().parents[2]
    folder = root / "ProgramCopies"
    folder.mkdir(parents=True, exist_ok=True)
    return folder

_LOADER_FALLBACKS = (
    r"C:\Program Files (x86)\Rockwell Software\RSLogix 5000\Common\RSLogix5000Loader.exe",
    r"C:\Program Files\Rockwell Software\RSLogix 5000\Common\RSLogix5000Loader.exe",
)

# Drive path to RSLogix5000Loader.exe; allows spaces (e.g. "RSLogix 5000").
_LOADER_IN_CMD = re.compile(
    r'(?P<p>[A-Za-z]:\\[^\t"%]*?RSLogix5000Loader\.exe)',
    re.IGNORECASE,
)


def _parse_loader_from_command(cmd: str) -> Path | None:
    """Extract RSLogix5000Loader.exe from a shell ``open`` command string."""
    m = _LOADER_IN_CMD.search(str(cmd or ""))
    if not m:
        return None
    p = Path(m.group("p"))
    return p if p.is_file() else None


def _designer_loader_path() -> Path:
    """Resolve RSLogix5000Loader.exe (version-aware Studio/Logix Designer launcher)."""
    # Prefer ACD/L5X associations — some machines map .L5K to Notepad.
    prog_ids = ("acdfile", "l5xfile")
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Classes"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Classes"),
        (winreg.HKEY_CURRENT_USER, r"Software\Classes"),
    )
    for hive, root in roots:
        for prog_id in prog_ids:
            try:
                with winreg.OpenKey(hive, rf"{root}\{prog_id}\shell\open\command") as key:
                    cmd, _ = winreg.QueryValueEx(key, None)
            except OSError:
                continue
            found = _parse_loader_from_command(str(cmd))
            if found is not None:
                return found
    for fb in _LOADER_FALLBACKS:
        p = Path(fb)
        if p.is_file():
            return p
    raise FileNotFoundError(
        "RSLogix5000Loader.exe not found. Install Studio 5000 Logix Designer "
        "or repair the .ACD file association."
    )


@mcp.tool()
async def copy_project(path: str, output: str = "", force: bool = False) -> str:
    """Copy a ``.ACD`` / ``.L5X`` / ``.L5K`` on disk without opening it in the SDK.

    Use this when Studio already has the original project open (exclusive lock):
    copy into ``ProgramCopies`` under the MCP install, then call other tools
    against the copy. With no ``output``, writes
    ``ProgramCopies/{stem}_mcp{suffix}``. Pass ``force=true`` to overwrite an
    existing destination.
    """
    pf = preflight_project_path(path)
    if pf:
        return pf

    src = _resolve(path)
    if output and str(output).strip():
        out = _resolve(output)
    else:
        out = _program_copies_dir() / f"{src.stem}_mcp{src.suffix}"

    pf = preflight_output_path(str(out), _PROJECT_EXTS)
    if pf:
        return pf
    if out.suffix.lower() != src.suffix.lower():
        return (
            "[FAIL] copy_project\n"
            "code:    INVALID_INPUT\n"
            "class:   ValueError\n"
            f"message: output extension {out.suffix!r} must match source "
            f"{src.suffix!r}\n"
            "hint:    Keep the same project extension on the copy.\n"
            f"context: path={src} output={out}"
        )
    if out.resolve() == src.resolve():
        return (
            "[FAIL] copy_project\n"
            "code:    INVALID_INPUT\n"
            "class:   ValueError\n"
            "message: output path is the same as the source path\n"
            "hint:    Pick a different destination path for the copy.\n"
            f"context: path={src} output={out}"
        )
    if out.exists() and not force:
        return (
            "[FAIL] copy_project\n"
            "code:    CONFIRM_REQUIRED\n"
            "class:   FileExistsError\n"
            f"message: destination already exists: {out}\n"
            "hint:    Re-run with force=true to overwrite, or pick a new output path.\n"
            f"context: path={src} output={out} force={force}"
        )

    async def _do() -> str:
        # Binary file copy — does not open Logix Designer / the SDK.
        shutil.copy2(src, out)
        size_mb = out.stat().st_size / (1024 * 1024)
        return (
            f"[OK] Copied {src} -> {out}\n"
            f"Size: {size_mb:.2f} MB\n"
            "Note: Studio may still hold an exclusive lock on the source; "
            "use the copy path for subsequent MCP tools."
        )

    return await _run(
        "copy_project", _do, path=str(src), output=str(out), force=force
    )


@mcp.tool()
async def launch_designer(path: str, wait: bool = False) -> str:
    """Open a project in the Studio 5000 Logix Designer **GUI** (not the headless SDK).

    Uses ``RSLogix5000Loader.exe`` (same as double-clicking the file) so the
    correct Designer major revision is selected for the ACD/L5X/L5K. This is
    for human review after MCP edits — it does **not** replace ``open_project``
    for API work.

    Tip: finish SDK tools first (they close the project handle). If Designer
    reports the file is in use, retry after MCP operations complete. Pass
    ``wait=true`` only if you want the tool call to block until Designer exits.
    """
    pf = preflight_project_path(path)
    if pf:
        return pf
    if sys.platform != "win32":
        return (
            "[FAIL] launch_designer\n"
            "code:    OS_ERROR\n"
            "class:   NotImplementedError\n"
            "message: launch_designer is only supported on Windows\n"
            "hint:    Run the MCP host on a Windows machine with Logix Designer installed.\n"
            f"context: platform={sys.platform}"
        )

    async def _do() -> str:
        p = _resolve(path)
        loader = _designer_loader_path()
        # Detached GUI process — do not use shell=True.
        proc = subprocess.Popen(  # noqa: S603 — fixed loader + validated project path
            [str(loader), str(p)],
            close_fds=True,
        )
        if wait:
            rc = proc.wait()
            return (
                f"[OK] Designer exited for {p}\n"
                f"Loader: {loader}\n"
                f"ExitCode: {rc}"
            )
        return (
            f"[OK] Launched Logix Designer GUI for {p}\n"
            f"Loader: {loader}\n"
            f"Pid: {proc.pid}\n"
            "Note: headless SDK open_project is separate; this is GUI-only."
        )

    return await _run("launch_designer", _do, path=path, wait=wait)


@mcp.tool()
async def open_project(path: str, sdk_open: bool = False) -> str:
    """Open a Logix project. ``.L5X`` defaults to a fast lxml peek; pass ``sdk_open=True`` for a full SDK open. ``.ACD`` always uses the SDK."""
    pf = preflight_project_path(path)
    if pf:
        return pf

    async def _do() -> str:
        p = _resolve(path)
        if p.suffix.lower() == ".l5x" and not sdk_open:
            return _l5x_quick_open_summary(p)
        async with _opened(p) as proj:
            comm = ""
            try:
                comm = await proj.get_communications_path()
            except Exception:  # noqa: BLE001 -- SDK may reject when no comm path set
                comm = "(not set)"
            return (
                f"[OK] Opened {p}\n"
                f"Mode: full SDK open\n"
                f"CommunicationsPath: {comm or '(not set)'}"
            )

    return await _run("open_project", _do, path=path, sdk_open=sdk_open)


@mcp.tool()
async def save_project(path: str, output: str = "") -> str:
    """Save the project. With no ``output`` calls ``save()``; otherwise ``save_as`` (``detailed_l5x`` flips on for ``.L5X`` outputs)."""
    pf = preflight_project_path(path)
    if pf:
        return pf
    if output:
        pf = preflight_output_path(output, _PROJECT_EXTS)
        if pf:
            return pf

    async def _do() -> str:
        p = _resolve(path)
        async with _opened(p) as proj:
            if output:
                out = _resolve(output)
                await proj.save_as(
                    str(out),
                    force=True,
                    detailed_l5x=out.suffix.lower() == ".l5x",
                )
                return f"[OK] Saved {p} -> {out}"
            await proj.save()
            return f"[OK] Saved {p}"

    return await _run("save_project", _do, path=path, output=output)


@mcp.tool()
async def export_l5x(path: str, output: str) -> str:
    """Export the full project to an ``.L5X`` via ``save_as(detailed_l5x=True)``."""
    pf = preflight_project_path(path)
    if pf:
        return pf
    pf = preflight_output_path(output, {".L5X"})
    if pf:
        return pf

    async def _do() -> str:
        p = _resolve(path)
        out = _resolve(output)
        async with _opened(p) as proj:
            await proj.save_as(str(out), force=True, detailed_l5x=True)
        return f"[OK] Exported {p} -> {out}"

    return await _run("export_l5x", _do, path=path, output=output)


@mcp.tool()
async def read_l5x(path: str) -> str:
    """Return an lxml-only summary of a ``.L5X`` file without opening it in the SDK."""
    pf = preflight_project_path(path)
    if pf:
        return pf

    async def _do() -> str:
        p = _resolve(path)
        if p.suffix.lower() != ".l5x":
            raise ValueError("read_l5x requires a .L5X file")
        return _l5x_quick_open_summary(p)

    return await _run("read_l5x", _do, path=path)


@mcp.tool()
async def convert_project(path: str, major_revision: int, output: str) -> str:
    """Convert between ``.ACD`` / ``.L5X`` / ``.L5K`` for a target major revision via ``LogixProject.convert`` + ``save_as``."""
    pf = preflight_project_path(path)
    if pf:
        return pf
    pf = preflight_output_path(output, _PROJECT_EXTS)
    if pf:
        return pf

    async def _do() -> str:
        p = _resolve(path)
        out = _resolve(output)
        rev = int(major_revision)
        proj = await LogixProject.convert(str(p), rev, MCPEventLogger())
        try:
            await proj.save_as(
                str(out),
                force=True,
                detailed_l5x=out.suffix.lower() == ".l5x",
            )
        finally:
            proj.close()
        return f"[OK] Converted {p} -> {out} (major_revision={rev})"

    return await _run(
        "convert_project",
        _do,
        path=path,
        output=output,
        major_revision=major_revision,
    )


@mcp.tool()
async def create_new_project(
    output: str,
    major_revision: int,
    processor_type: str,
    controller_name: str,
) -> str:
    """Create a new ``.ACD`` project for ``processor_type`` at ``major_revision`` via ``LogixProject.create_new_project``.

    Discover valid ``processor_type`` values by calling
    ``get_processor_types(major_revision=<rev>)`` first; pass the ``Name``
    column verbatim (e.g. ``"1756-L85E"``, ``"5069-L320ERMS3"``). Pass an
    integer major revision Logix Designer is installed for (e.g. ``37``).
    """
    pf = preflight_output_path(output, {".ACD"})
    if pf:
        return pf

    async def _do() -> str:
        out = _resolve(output)
        rev = int(major_revision)
        ptype = str(processor_type).strip()
        cname = str(controller_name).strip()
        if not ptype:
            raise ValueError("processor_type is empty")
        if not cname:
            raise ValueError("controller_name is empty")
        proj = await LogixProject.create_new_project(
            str(out), rev, ptype, cname, MCPEventLogger()
        )
        try:
            await proj.save()
        finally:
            proj.close()
        return (
            f"[OK] Created {out} (major_revision={rev}, "
            f"processor_type={ptype}, controller_name={cname})"
        )

    return await _run(
        "create_new_project",
        _do,
        output=output,
        major_revision=major_revision,
        processor_type=processor_type,
        controller_name=controller_name,
    )


@mcp.tool()
async def get_processor_types(major_revision: int) -> str:
    """Enumerate every legal ``processor_type`` for ``create_new_project`` / ``change_controller_type`` at the given Logix Designer major revision.

    Returns a ``Name | ProductCode | ProductType | Id`` table. The ``Name``
    column is what you pass back as ``processor_type``. Common ``major_revision``
    values: 33, 34, 35, 36, 37 (must be installed in Studio 5000).
    An empty result means that major revision is not installed on this machine.
    """

    async def _do() -> str:
        rev = int(major_revision)
        items = await LogixProject.get_processor_types(rev)

        # SDK returns ``dict[str, ProcessorType]`` keyed by name. The
        # ``ProcessorType`` named tuple exposes ``name`` / ``product_code`` /
        # ``product_type`` / ``id``. Iterating ``items`` (without .items())
        # would yield the keys only and silently render empty rows -- which
        # caused early callers to misread the table as "0 processors".
        rows: list[tuple[str, str, str, str]] = []
        if isinstance(items, dict):
            entries = list(items.values())
        else:
            entries = list(items or [])
        for it in entries:
            rows.append(
                (
                    str(_field(it, "name", "Name")),
                    str(_field(it, "product_code", "ProductCode")),
                    str(_field(it, "product_type", "ProductType")),
                    str(_field(it, "id", "Id")),
                )
            )
        if not rows:
            return (
                f"[OK] get_processor_types(major_revision={rev}) returned 0 "
                f"processors. Confirm Logix Designer {rev} is installed."
            )
        table = _fmt_table(rows, ("Name", "ProductCode", "ProductType", "Id"))
        return (
            f"[OK] Processor types for major_revision={rev} "
            f"(count={len(rows)}):\n{table}"
        )

    return await _run(
        "get_processor_types", _do, major_revision=major_revision
    )


def _field(obj: object, *names: str) -> object:
    """Return the first attribute / mapping key match across ``names``."""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    if isinstance(obj, dict):
        for n in names:
            if n in obj:
                return obj[n]
    return ""


__all__ = [
    "copy_project",
    "launch_designer",
    "open_project",
    "save_project",
    "export_l5x",
    "read_l5x",
    "convert_project",
    "create_new_project",
    "get_processor_types",
]


# Reference for type checkers — make Path import explicit so future contributors
# notice that ``_resolve`` always returns a ``Path``.
_: type = Path
