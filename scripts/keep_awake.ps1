# Keeps the PC from sleeping on trading days from 8:00 AM to 7:00 PM local
# (Central) time, so the 8:45 AM buy, 9:30 AM watchdog and 6:00 PM scan
# run on time. The screen can still turn off. Started by the
# "TradingBot Keep Awake" task at 8:00 AM and at logon; exits at 7:00 PM
# (or at once outside that window), and Windows may sleep again after.
#
# Why: this PC sleeps after 5 minutes idle and uses Modern Standby, which
# can ignore the tasks' wake timers (runs started at 9:10 and 9:14, and the
# 2026-11-04 evening scan never ran). See PROJECT_LOG.md, 2026-11-05.
$ErrorActionPreference = "Stop"

$StartHour = 8
$EndHour = 19

$Now = Get-Date
$End = $Now.Date.AddHours($EndHour)
$Weekday = $Now.DayOfWeek -notin @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)
if (-not $Weekday -or $Now.Hour -lt $StartHour -or $Now -ge $End) {
    exit 0
}

# Market holidays too (Alpaca calendar; weekdays count if it can't be asked).
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
Set-Location $ProjectDir
& $Python ".\market_clock.py" trading-day
if ($LASTEXITCODE -eq 1) {
    exit 0
}

Add-Type -Namespace TradingBot -Name Power -MemberDefinition @"
[DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
"@

$ES_CONTINUOUS = [uint32]"0x80000000"
$ES_SYSTEM_REQUIRED = [uint32]"0x00000001"

# Held while this process runs; released when it exits.
[void][TradingBot.Power]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)
try {
    while ((Get-Date) -lt $End) {
        # Re-assert now and then, in case Windows dropped the request.
        [void][TradingBot.Power]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)
        Start-Sleep -Seconds 300
    }
}
finally {
    [void][TradingBot.Power]::SetThreadExecutionState($ES_CONTINUOUS)
}
