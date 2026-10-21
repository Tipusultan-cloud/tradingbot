$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$ScheduledScan = Join-Path $PSScriptRoot "scheduled_daily_scan.ps1"
$TaskName = "TradingBot Daily Scan"

if (-not (Test-Path $ScheduledScan)) {
    throw "Scheduled scan script not found: $ScheduledScan"
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScheduledScan`"" `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At "8:30 AM"

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Runs the TradingBot scan pipeline on weekdays at 8:30 AM local time." `
    -Force | Out-Null

Write-Host "Installed '$TaskName' for weekdays at 8:30 AM local time."
Write-Host "The scheduled task runs only while this Windows user is logged in."
Write-Host "Verify it in Task Scheduler before relying on alerts."
