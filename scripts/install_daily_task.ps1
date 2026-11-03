$ErrorActionPreference = "Stop"

# Two weekday tasks (local time; the market opens 8:30 AM and closes
# 3:00 PM Central):
#   6:00 PM  TradingBot Daily Scan   full scan on the day's final bars
#   8:45 AM  TradingBot Morning Buy  buys the picks 15 minutes after the open
$ProjectDir = Split-Path -Parent $PSScriptRoot

# WakeToRun: a sleeping PC wakes up for the run (on 2026-11-03 the 8:45
# buy started late at 9:10). Hidden window: closing a visible task window
# stopped both runs that day.
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$Tasks = @(
    @{
        Name = "TradingBot Daily Scan"
        Script = "scheduled_daily_scan.ps1"
        At = "6:00 PM"
        Description = "Runs the TradingBot scan pipeline on weekdays at 6:00 PM local time, after the close."
    },
    @{
        Name = "TradingBot Morning Buy"
        Script = "scheduled_morning_buy.ps1"
        At = "8:45 AM"
        Description = "Buys the TradingBot picks on Alpaca paper on weekdays at 8:45 AM local time."
    }
)

foreach ($Task in $Tasks) {
    $Script = Join-Path $PSScriptRoot $Task.Script
    if (-not (Test-Path $Script)) {
        throw "Scheduled script not found: $Script"
    }

    $Action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Script`"" `
        -WorkingDirectory $ProjectDir

    $Trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
        -At $Task.At
    # No UTC offset, so the task follows local time across daylight saving
    # (with an offset it ran at 7:30 AM after the November change).
    $Trigger.StartBoundary = (Get-Date $Task.At).ToString("yyyy-MM-ddTHH:mm:ss")

    try {
        Register-ScheduledTask `
            -ErrorAction Stop `
            -TaskName $Task.Name `
            -Action $Action `
            -Trigger $Trigger `
            -Settings $Settings `
            -Principal $Principal `
            -Description $Task.Description `
            -Force | Out-Null
        Write-Host "Installed '$($Task.Name)' for weekdays at $($Task.At) local time."
    }
    catch {
        $Failed = $true
        Write-Warning "Could not install '$($Task.Name)': $_"
    }
}

if ($Failed) {
    Write-Warning "Run this again as administrator (right-click 'Install Daily Scan Task.bat' > Run as administrator)."
    exit 1
}

Write-Host "The scheduled tasks run only while this Windows user is logged in."
Write-Host "Verify them in Task Scheduler before relying on alerts."
