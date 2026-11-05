# Watchdog (9:30 AM and 8:00 PM Central, weekdays): emails when the last
# morning buy or evening scan did not finish OK, or when a position on
# Alpaca has no stop order. Each problem is emailed once (run_health.py).
$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectDir "logs"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$LogFile = Join-Path $LogDir "watchdog_$Timestamp.log"

Set-Location $ProjectDir

Start-Transcript -Path $LogFile -Append | Out-Null
try {
    Write-Host "TRADINGBOT WATCHDOG - $(Get-Date)"
    & $Python ".\run_health.py" check
    $Code = $LASTEXITCODE
}
finally {
    Stop-Transcript | Out-Null
}
exit $Code
