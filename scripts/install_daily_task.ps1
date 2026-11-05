$ErrorActionPreference = "Stop"

# Three weekday tasks (local time; the market opens 8:30 AM and closes
# 3:00 PM Central):
#   6:00 PM  TradingBot Daily Scan   full scan on the day's final bars
#   8:45 AM  TradingBot Morning Buy  buys the picks 15 minutes after the open
#   9:30 AM and 8:00 PM  TradingBot Watchdog  emails missed or failed runs
#                                    and positions without a stop order
#   8:00 AM (and at logon)  TradingBot Keep Awake  no sleep until 7:00 PM
$ProjectDir = Split-Path -Parent $PSScriptRoot

# WakeToRun: a sleeping PC wakes up for the run (on 2026-11-03 the 8:45
# buy started late at 9:10). Hidden window: closing a visible task window
# stopped both runs that day. On this PC (Modern Standby) Windows can
# ignore WakeToRun, so sleep must be off while plugged in (PROJECT_LOG.md,
# 2026-11-05).
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
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
        At = @("6:00 PM")
        Description = "Runs the TradingBot scan pipeline on weekdays at 6:00 PM local time, after the close."
    },
    @{
        Name = "TradingBot Morning Buy"
        Script = "scheduled_morning_buy.ps1"
        At = @("8:45 AM")
        Description = "Buys the TradingBot picks on Alpaca paper on weekdays at 8:45 AM local time."
    },
    @{
        Name = "TradingBot Watchdog"
        Script = "scheduled_watchdog.ps1"
        At = @("9:30 AM", "8:00 PM")
        Description = "Emails when a TradingBot run was missed or failed, or a position has no stop order."
    },
    @{
        Name = "TradingBot Keep Awake"
        Script = "keep_awake.ps1"
        At = @("8:00 AM")
        AtLogOn = $true
        TimeLimitHours = 12
        Description = "Keeps the PC from sleeping on weekdays from 8:00 AM to 7:00 PM local time so the TradingBot runs start on time."
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

    $Trigger = foreach ($At in $Task.At) {
        $One = New-ScheduledTaskTrigger `
            -Weekly `
            -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
            -At $At
        # No UTC offset, so the task follows local time across daylight saving
        # (with an offset it ran at 7:30 AM after the November change).
        $One.StartBoundary = (Get-Date $At).ToString("yyyy-MM-ddTHH:mm:ss")
        $One
    }
    if ($Task.AtLogOn) {
        # Logging in during the day starts it too (keep_awake.ps1 exits at
        # once outside 8:00 AM - 7:00 PM on weekdays).
        $Trigger = @($Trigger) + (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME")
    }

    $TaskSettings = $Settings
    if ($Task.TimeLimitHours) {
        $TaskSettings = New-ScheduledTaskSettingsSet `
            -StartWhenAvailable `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Hours $Task.TimeLimitHours)
    }

    try {
        Register-ScheduledTask `
            -ErrorAction Stop `
            -TaskName $Task.Name `
            -Action $Action `
            -Trigger $Trigger `
            -Settings $TaskSettings `
            -Principal $Principal `
            -Description $Task.Description `
            -Force | Out-Null
        Write-Host "Installed '$($Task.Name)' for weekdays at $($Task.At -join ' and ') local time."
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
