$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$Ngrok = "C:\Users\tipus\Downloads\ngrok-v3-stable-windows-amd64\ngrok.exe"
$ApiPort = 8000
$DashboardPort = 8001

if (-not (Test-Path $ProjectDir)) {
    Write-Host "TradingBot folder not found:" -ForegroundColor Red
    Write-Host $ProjectDir -ForegroundColor Yellow
    Read-Host "Press Enter to close"
    exit 1
}

if (-not (Test-Path $Python)) {
    Write-Host "TradingBot Python was not found:" -ForegroundColor Red
    Write-Host $Python -ForegroundColor Yellow
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host ""
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "STARTING TRADINGBOT MACHINE" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host ""

# ------------------------------------------------------------
# 1. FASTAPI SERVER
# ------------------------------------------------------------
$port8000 = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue

if ($port8000) {
    Write-Host "Webhook API: already running on port $ApiPort" -ForegroundColor Green
}
else {
    Write-Host "Starting webhook API..." -ForegroundColor Yellow

    $serverCommand = @"
Set-Location '$ProjectDir'
& '$Python' -m uvicorn app:app --host 127.0.0.1 --port $ApiPort
"@

    Start-Process powershell.exe -ArgumentList @(
        "-NoExit",
        "-ExecutionPolicy", "Bypass",
        "-Command", $serverCommand
    )

    Start-Sleep -Seconds 3
}

# ------------------------------------------------------------
# 2. LOCAL OPERATOR DASHBOARD
# ------------------------------------------------------------
$dashboardListener = Get-NetTCPConnection -LocalPort $DashboardPort -State Listen -ErrorAction SilentlyContinue

if ($dashboardListener) {
    Write-Host "Dashboard: already running on http://127.0.0.1:$DashboardPort" -ForegroundColor Green
}
else {
    Write-Host "Starting local operator dashboard..." -ForegroundColor Yellow

    $dashboardCommand = @"
Set-Location '$ProjectDir'
& '$Python' -m uvicorn app:dashboard_app --host 127.0.0.1 --port $DashboardPort
"@

    Start-Process powershell.exe -ArgumentList @(
        "-NoExit",
        "-ExecutionPolicy", "Bypass",
        "-Command", $dashboardCommand
    )

    Start-Sleep -Seconds 3
}

# ------------------------------------------------------------
# 3. DAILY SCANNER PIPELINE
# ------------------------------------------------------------
$scanProcess = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -eq "python.exe" -and
        $_.CommandLine -like "*$ProjectDir*" -and
        $_.CommandLine -match "universe_builder\.py|scanner\.py|scanner_topdown\.py|master_selector\.py|scanner_pullback\.py|slot_selector\.py"
    }

if ($scanProcess) {
    Write-Host "Scanner pipeline: already running" -ForegroundColor Green
}
else {
    Write-Host "Starting scanner pipeline..." -ForegroundColor Yellow

    $ScheduledScan = Join-Path $ProjectDir "scripts\scheduled_daily_scan.ps1"
    $scanCommand = @"
& '$ScheduledScan'
if (`$LASTEXITCODE -ne 0) { Write-Host 'Scan or email notification failed.' -ForegroundColor Red }
Read-Host 'Press Enter to close this scanner window'
"@

    Start-Process powershell.exe -ArgumentList @(
        "-NoExit",
        "-ExecutionPolicy", "Bypass",
        "-Command", $scanCommand
    )
}

# ------------------------------------------------------------
# 4. NGROK WEBHOOK TUNNEL
# ------------------------------------------------------------
$ngrokProcess = Get-Process ngrok -ErrorAction SilentlyContinue

if ($ngrokProcess) {
    Write-Host "ngrok: already running" -ForegroundColor Green
}
else {
    if (Test-Path $Ngrok) {
        Write-Host "Starting ngrok..." -ForegroundColor Yellow

        $ngrokCommand = @"
Set-Location '$ProjectDir'
& '$Ngrok' http $ApiPort
"@

        Start-Process powershell.exe -ArgumentList @(
            "-NoExit",
            "-ExecutionPolicy", "Bypass",
            "-Command", $ngrokCommand
        )
    }
    else {
        Write-Host "ngrok executable not found at:" -ForegroundColor Yellow
        Write-Host $Ngrok -ForegroundColor Yellow
        Write-Host "FastAPI and scanner were still started." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "TradingBot startup launched." -ForegroundColor Green
Write-Host "Dashboard (local only): http://127.0.0.1:$DashboardPort" -ForegroundColor Gray
Write-Host "Webhook API (local): http://127.0.0.1:$ApiPort" -ForegroundColor Gray
Write-Host "ngrok exposes the webhook API only." -ForegroundColor Gray
Write-Host ""
Start-Sleep -Seconds 2
