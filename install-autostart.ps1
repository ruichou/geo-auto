$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$pwsh = (Get-Command pwsh.exe).Source
$serviceScript = (Resolve-Path -LiteralPath (Join-Path $projectRoot 'start-service.ps1')).Path
$action = New-ScheduledTaskAction -Execute $pwsh -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$serviceScript`"" -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 3650) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -TaskName 'Hongtu GEO Autopilot' -Action $action -Trigger $trigger -Settings $settings -Description '宏图商机 GEO 全自动研究、发布与监测服务' -RunLevel Limited -Force | Out-Null
Write-Output 'Hongtu GEO Autopilot 已设置为当前用户登录后自动启动。'
