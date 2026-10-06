# Development Guide

## Project layout

- `l5x_acd_server.py` - compatibility shim entrypoint
- `logix_mcp/server.py` - MCP runtime entrypoint + eager tool imports
- `logix_mcp/_common.py` - shared runtime, errors, preflights
- `logix_mcp/_xml.py` - L5X parsing/formatting helpers
- `logix_mcp/tools/` - tool modules grouped by capability

## Tool authoring pattern

Use this template:

```python
@mcp.tool()
async def my_tool(...):
    pf = preflight_project_path(path)
    if pf:
        return pf

    async def _do() -> str:
        async with _opened(_resolve(path)) as proj:
            ...
        return "[OK] ..."

    return await _run("my_tool", _do, path=path)
```

## Error model

- Success: `[OK] ...`
- Failure: `_run(...)` converts exceptions to structured `[FAIL]` blocks.
- Add token-to-hint mappings in `_hint_for(...)` for new Rockwell error families.

## Release checklist

- Update `README.md` and `CHANGELOG.md`.
- Verify tools/resources list (PowerShell):

```powershell
py -3.12 -c "import asyncio; import logix_mcp.server; from logix_mcp._common import mcp; print('tools', len(asyncio.run(mcp.list_tools()))); print('resources', len(asyncio.run(mcp.list_resources())))"
```

- Sanity boot:

```powershell
py -3.12 l5x_acd_server.py
```

Shell / agent conventions: see [`../AGENTS.md`](../AGENTS.md) (PowerShell only).

## Future tools — running Designer session

Not implemented. Verified against a v35 Designer that already has a project open. The SDK cannot open that ACD while Designer holds the lock; this path binds to the running process instead.

Bind:

- ProgID `RSLogix5000.Application.35.00` (`GetActiveObject`). 64-bit Python can bind; the server is `LogixDesigner.exe`.
- `GetActiveProject` is dispid 18 on the Application object (same object as Application property 18).
- No type library. `GetTypeInfoCount` is 0. Names below were resolved with `GetIDsOfNames` on the live object.

Project methods to wrap:

| Future tool | Dispid | Notes |
|-------------|--------|--------|
| `attach_running_project` | 18 `GetActiveProject` | Returns the open project. `Modified` is dispid 5, `Revision` is dispid 6, `Controller` is dispid 7. |
| `designer_save` | 9 `Save` | Writes the ACD Designer already has open. Confirm gate. |
| `designer_save_as` | 10 `SaveAs` | Confirm gate. |
| `designer_close` | 11 `Close` | Confirm gate. |
| `designer_download` | 12 `Download` | Confirm gate. Do not call during exploration. |
| `designer_upload` | 13 `Upload` | Confirm gate. |
| `designer_go_offline` | 15 `GoOffline` | Confirm gate. |
| `designer_verify` | 16 `Verify` | |
| `designer_go_online` | 8 `GoOnline` | Confirm gate. Application also exposes `GoOnline` at dispid 15; use the project object. |

`Controller.Tags` (dispid 1) is readable on this same object (`Count`, `Item`, `Name`, `GetValue`, `SetValue`). It is not part of this planned set. Programs, routines, rungs, import, and cross-reference do not resolve on the Application, the project, or the controller.
