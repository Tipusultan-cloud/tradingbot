$ErrorActionPreference = "Stop"

$ScheduledScan = Join-Path $PSScriptRoot "scripts\scheduled_daily_scan.ps1"
& $ScheduledScan
exit $LASTEXITCODE
