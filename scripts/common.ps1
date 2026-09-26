$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

function Invoke-ProjectPnpm {
    param([string[]]$PnpmArguments)
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCommand) { throw 'Please install Node.js 22 or later, then run setup again.' }
    $pnpmCommand = Get-Command pnpm -ErrorAction SilentlyContinue
    if ($pnpmCommand) {
        & $pnpmCommand.Source @PnpmArguments
    } else {
        $bundledPnpm = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\pnpm\bin\pnpm.cjs'
        if (Test-Path -LiteralPath $bundledPnpm) {
            & $nodeCommand.Source $bundledPnpm @PnpmArguments
        } else {
            $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
            if (-not $npmCommand) { throw 'pnpm is not available. Install pnpm 11, then run setup again.' }
            & $npmCommand.Source exec --yes --package=pnpm@11.19.0 -- pnpm @PnpmArguments
        }
    }
    if ($LASTEXITCODE -ne 0) { throw 'The frontend command failed. See the message above.' }
}
