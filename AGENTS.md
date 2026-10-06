# Agent guide — studio5000-mcp

Windows-only Rockwell / Logix MCP server. Use **PowerShell** for every shell command.

## Shell (hard rules)

- Do **not** use bash, Git Bash, WSL, `sh`, or bash heredocs (`<<'EOF'`, `<<'PY'`, `$(cat <<'EOF')`).
- Do **not** call `bash.exe` / `wsl.exe` to work around PowerShell syntax.
- Prefer PowerShell-native constructs: here-strings (`@'...'@`, `@"..."@`), pipelines, `Get-ChildItem`, `Join-Path`, `Test-Path`.
- For multi-line Python, write a temporary `.py` file (e.g. under `$env:TEMP`) and run it — never pipe a bash heredoc into `python`.
- Resolve Python 3.12 as:
  1. `py -3.12` if the launcher exists, else
  2. `"$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"` (or another discovered 3.12 path).
- Install deps with `.\install.ps1` (or `.\install.bat`, which wraps it). Prefer documenting PowerShell in commits/PRs.

### Commit messages on PowerShell

```powershell
git commit -m @'
Short summary of why.

'@
```

## Workflow notes

- Prefer MCP tools (`user-studio5000-mcp` / `studio5000-mcp`) over ad-hoc SDK scripts.
- When Studio has an `.ACD` open, use `copy_project` then operate on the copy — do not fight the exclusive lock. `copy_project` writes `ProgramCopies/{stem}_mcp{suffix}` inside the MCP install.
- Typical edit → review flow: MCP tools against a working copy, then `launch_designer(path)` so the operator can inspect in the GUI. `open_project` is headless SDK only; `launch_designer` is GUI only.
- After `trace_origin`, use the JSON boxes/edges for follow-up questions. Narrow with `focus` or `from_box`, read a rung with `get_rung`, draw it with `get_rung_diagram`, and use `search_rungs` / `cross_reference` on a node. Set `diagram=false` when the tree is large.
- Destructive online tools require `confirm=true`.
- Do not write gateway passwords or plant credentials into project resources.
