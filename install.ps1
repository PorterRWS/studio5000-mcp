#Requires -Version 5.1
<#
.SYNOPSIS
  Install logix_designer_sdk wheel + studio5000-mcp Python deps (Python 3.12).
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Set-Location -Path $PSScriptRoot

function Get-Python312Path {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $exe = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path -LiteralPath $exe.Trim())) {
            return $exe.Trim()
        }
    }

    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "C:\Python312\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    throw "Python 3.12 not found. Install Python 3.12.x (Rockwell SDK requires >=3.12,<3.13)."
}

$python = Get-Python312Path
$sdkPythonDir = "C:\Users\Public\Documents\Studio 5000\Logix Designer SDK\python"
$wheel = Get-ChildItem -LiteralPath $sdkPythonDir -Filter "logix_designer_sdk-*-py3-none-any.whl" -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending |
    Select-Object -First 1

Write-Host "Installing Logix Designer SDK wheel, mcp, fastmcp, lxml."
Write-Host "Note: Rockwell logix_designer_sdk requires Python 3.12.x (not 3.13)."
Write-Host "Python: $python"

if (-not $wheel) {
    throw "SDK wheel not found under '$sdkPythonDir'. Install Logix Designer SDK 2.01+ first."
}

Write-Host "Wheel: $($wheel.FullName)"
& $python -m pip install $wheel.FullName
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Done."
& $python -m pip show logix-designer-sdk fastmcp mcp lxml
exit $LASTEXITCODE
