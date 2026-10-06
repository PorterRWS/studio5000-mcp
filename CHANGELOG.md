# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Added
- Modular server package (`logix_mcp/`) with area-based tool modules.
- Full SDK-facing MCP tool surface for project I/O, build, tags, program queries,
  partial import/export, online ops, safety, SD card, and gated protection APIs.
- Creation helpers: `create_udt`, `create_tag`, `create_program`, `add_io_module`.
- Discovery helpers: `get_processor_types`, `list_options`, `logix://sdk/info`.
- `copy_project` for lock-safe on-disk ACD/L5X/L5K working copies (no SDK open). Default destination is `ProgramCopies/` in the MCP install.
- `launch_designer` to open a project in the Logix Designer GUI (`RSLogix5000Loader`).
- `cross_reference` offline where-used via detailed L5X (aliases, RLL/ST, modules; heuristic Dest?).
- `trace_origin` offline backward trace from a tag through ladder writes and their feeder tags to literals and tags the project never writes. A CLR or literal-0 write is listed in text and left out of the diagram; the branch is kept up to that write and cut off there. The Mermaid flowchart groups each remaining rung in a box: the written tag plus the inputs that continue to another rung, with L5X descriptions and bit comments. Default output is text plus JSON (`format=both`) with boxes, edges, zero-writes, and gaps. Narrow with `focus` / `from_box`; omit Mermaid with `diagram=false`.
- `get_rung` returns the full neutral text of one program/routine/rung for drill-down after xref or origin trace.
- `get_rung_diagram` draws one rung as Unicode box-drawing text and SVG with Studio-like contact/coil/block symbols and branch structure. L5X tag/bit descriptions are soft-wrapped above each instruction (column width unchanged; long words hyphen-break) and listed in a table.
- `install.ps1` PowerShell installer; `install.bat` now wraps it.
- `AGENTS.md` + Cursor rule: PowerShell-only agent shell (no bash heredocs).

### Changed
- Replaced monolithic server file with `l5x_acd_server.py` shim that forwards to `logix_mcp.server.main()`.
- Standardized error output to structured `[FAIL]` blocks.
- Improved hint mapping for XML import error tokens.
- Tightened XPath preflight validation.
- Docs/quickstart use PowerShell examples and Python 3.12 path fallbacks.

### Fixed
- Origin trace no longer treats a UDT member name as its own tag. `Source9.AirBlow` stays one reference, so a contact on that member does not pull in every other `.AirBlow` unlatch. Dead-end writes of the same tag share one diagram box with a short Mermaid title (`Routine · N writers`) so long rung lists do not overflow the subgraph border.
- Corrected `get_processor_types` parsing to iterate SDK `ProcessorType` values (not dict keys).
- Added safer software revision handling for generated UDT fragments.

### Planned
- Designer-session tools, not implemented yet. Bind with `GetActiveObject("RSLogix5000.Application.35.00")` and `GetActiveProject` (dispid 18). On that project object: `Save`, `SaveAs`, `Close`, `Download`, `Upload`, `Verify`, `GoOnline`, `GoOffline`. These talk to the already-open Designer process; they are separate from the SDK tools of the same names. `Download`, `Upload`, `GoOnline`, `GoOffline`, `Close`, and `Save` need the same confirm gate as other destructive tools.
