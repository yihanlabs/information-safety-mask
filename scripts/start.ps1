. "$PSScriptRoot\common.ps1"
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv-cpu\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run setup.cmd first to prepare the application.' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'frontend\dist\index.html'))) { throw 'The UI is missing. Run setup.cmd first.' }
& $pythonPath -m safety_mask.launcher
if ($LASTEXITCODE -ne 0) { throw 'The local application stopped unexpectedly.' }
