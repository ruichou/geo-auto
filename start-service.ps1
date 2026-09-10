$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$python = (Resolve-Path -LiteralPath (Join-Path $projectRoot '.venv\Scripts\python.exe')).Path
$logDir = Join-Path $projectRoot 'data\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$stdout = Join-Path $logDir 'service.stdout.log'
$stderr = Join-Path $logDir 'service.stderr.log'
Set-Location -LiteralPath $projectRoot
& $python -m hongtu_geo.cli dashboard --no-open 1>> $stdout 2>> $stderr
