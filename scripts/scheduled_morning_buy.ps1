# Morning buy (8:45 AM Central, 15 minutes after the open): records fills
# and exits, raises Bot 3 trailing stops, buys the evening scan's picks and
# parks idle cash in SPY. The picks come from scheduled_daily_scan.ps1.
$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectDir "logs"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$LogFile = Join-Path $LogDir "morning_buy_$Timestamp.log"

Set-Location $ProjectDir

Start-Transcript -Path $LogFile -Append | Out-Null

# Watchdog record (run_health.py): a failure emails at once, a run that
# never finishes is reported by the TradingBot Watchdog task.
& $Python ".\run_health.py" start morning_buy

try {
    Write-Host ""
    Write-Host "================================"
    Write-Host "TRADINGBOT MORNING BUY"
    Write-Host "================================"
    Write-Host "Started: $(Get-Date)"

    # Does nothing unless AUTO_TRADING=true and the market is open.
    & $Python ".\auto_trader.py"
    if ($LASTEXITCODE -ne 0) {
        throw "auto_trader.py failed with exit code $LASTEXITCODE"
    }

    Write-Host ""
    Write-Host "Finished: $(Get-Date)"
    $Result = "ok"
    $Message = ""
}
catch {
    Write-Host ""
    Write-Host "MORNING BUY FAILED:"
    Write-Host $_
    $Result = "failed"
    $Message = "$_"
}
finally {
    Stop-Transcript | Out-Null
}

if (-not $Result) {
    $Result = "failed"
    $Message = "The run stopped before it finished."
}
& $Python ".\run_health.py" finish morning_buy $Result $Message --log $LogFile
if ($Result -ne "ok") {
    exit 1
}
