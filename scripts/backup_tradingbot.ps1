$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$BackupDir = Join-Path $ProjectDir "backups"

if (-not (Test-Path $ProjectDir)) {
    Write-Host "TradingBot folder not found." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$TempDir = Join-Path $env:TEMP "TradingBot_backup_$Timestamp"
$ZipFile = Join-Path $BackupDir "TradingBot_backup_$Timestamp.zip"

New-Item -ItemType Directory -Force -Path $TempDir | Out-Null

Write-Host ""
Write-Host "Saving TradingBot project backup..." -ForegroundColor Cyan

$ExcludeNames = @(
    ".venv",
    "__pycache__",
    "backups",
    ".git"
)

Get-ChildItem -Path $ProjectDir -Force | ForEach-Object {
    if ($ExcludeNames -notcontains $_.Name -and $_.Name -ne ".env") {
        Copy-Item $_.FullName -Destination $TempDir -Recurse -Force
    }
}

Compress-Archive -Path (Join-Path $TempDir "*") -DestinationPath $ZipFile -Force

Remove-Item $TempDir -Recurse -Force

Write-Host ""
Write-Host "BACKUP COMPLETE" -ForegroundColor Green
Write-Host $ZipFile -ForegroundColor Yellow
Write-Host ""
Read-Host "Press Enter to close"
