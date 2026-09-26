param([switch]$SkipModels)
. "$PSScriptRoot\common.ps1"
Set-Location -LiteralPath $projectRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is required. Install uv from https://docs.astral.sh/uv/getting-started/installation/'
}
Write-Host 'Preparing the isolated Python 3.11 environment...'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $projectRoot '.venv-cpu'
uv sync --frozen --python 3.11
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
Push-Location -LiteralPath (Join-Path $projectRoot 'frontend')
try {
    Invoke-ProjectPnpm -PnpmArguments @('install', '--frozen-lockfile')
    Invoke-ProjectPnpm -PnpmArguments @('run', 'build')
} finally { Pop-Location }
if (-not $SkipModels) {
    & (Join-Path $projectRoot '.venv-cpu\Scripts\python.exe') (Join-Path $PSScriptRoot 'prepare_models.py')
    if ($LASTEXITCODE -ne 0) { throw 'Model preparation failed. Run setup again to retry.' }
}
Write-Host 'Setup complete. Open start.cmd to launch the local workbench.'
