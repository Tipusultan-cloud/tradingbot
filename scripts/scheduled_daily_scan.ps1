# Evening scan (6:00 PM Central, after the close): builds tomorrow's picks
# from the day's final bars. Buying is scheduled_morning_buy.ps1.
$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectDir "logs"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$LogFile = Join-Path $LogDir "daily_scan_$Timestamp.log"
$ScanMutex = [System.Threading.Mutex]::new(
    $false,
    "Local\TradingBotDailyScan"
)
$HasScanLock = $false

Set-Location $ProjectDir

Start-Transcript -Path $LogFile -Append | Out-Null

try {
    $HasScanLock = $ScanMutex.WaitOne(0)
    if (-not $HasScanLock) {
        Write-Host "A TradingBot scan is already running; skipping this run."
        exit 0
    }

    Write-Host ""
    Write-Host "================================"
    Write-Host "TRADINGBOT EVENING SCAN (after the close)"
    Write-Host "================================"
    Write-Host "Started: $(Get-Date)"
    Write-Host ""

    & $Python ".\universe_builder.py"
    if ($LASTEXITCODE -ne 0) {
        throw "universe_builder.py failed with exit code $LASTEXITCODE"
    }

    & $Python ".\scanner.py"
    if ($LASTEXITCODE -ne 0) {
        throw "scanner.py failed with exit code $LASTEXITCODE"
    }

    & $Python ".\scanner_topdown.py"
    if ($LASTEXITCODE -ne 0) {
        throw "scanner_topdown.py failed with exit code $LASTEXITCODE"
    }

    & $Python ".\master_selector.py"
    if ($LASTEXITCODE -ne 0) {
        throw "master_selector.py failed with exit code $LASTEXITCODE"
    }

    # Close paper trades at stop / target / 20 days before slots are refilled.
    & $Python ".\paper_exits.py"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Paper exit check failed for some trades; they stay open until the next run."
    }

    # Record Alpaca fills and stop / target / 20-day exits (paper account).
    & $Python ".\auto_trader.py" --sync
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Alpaca sync failed; trades are checked again on the next run."
    }

    # A failed pullback scan leaves only the A+ pick for the slots.
    & $Python ".\scanner_pullback.py"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Pullback scan failed; slots will use the A+ pick only."
    }

    # Bot 3: S&P 500 + 200 most-traded US stocks that jumped on huge volume.
    & $Python ".\scanner_momentum.py"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Momentum scan failed; Bot 3 buys nothing this run."
    }

    & $Python ".\slot_selector.py"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Slot selection failed; pullback BUY alerts will be rejected."
    }

    # The picks are bought by scheduled_morning_buy.ps1 at 8:45 AM Central,
    # 15 minutes after the next open.

    Write-Host ""
    Write-Host "Updating shadow exit comparison for paper trades..."
    & $Python ".\shadow_exits.py"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Shadow exit update failed; the scan result is unaffected."
    }

    # Weekly report card: printed every run, emailed on Fridays.
    Write-Host ""
    & $Python ".\report_card.py" --friday
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Report card failed; the scan result is unaffected."
    }

    Write-Host ""
    Write-Host "Sending scan summary email if configured..."
    & $Python ".\scan_notifications.py"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Scan completed, but the email notification failed. See the error above."
        exit 1
    }

    Write-Host ""
    Write-Host "================================"
    Write-Host "DAILY SCAN COMPLETE"
    Write-Host "================================"
    Write-Host "Finished: $(Get-Date)"
}
catch {
    Write-Host ""
    Write-Host "SCAN FAILED:"
    Write-Host $_
    exit 1
}
finally {
    if ($HasScanLock) {
        $ScanMutex.ReleaseMutex()
    }
    $ScanMutex.Dispose()
    Stop-Transcript | Out-Null
}
