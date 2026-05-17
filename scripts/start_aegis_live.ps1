param(
    [int]$DashboardPort = 8501,
    [int]$ScanSeconds = 300,
    [string]$Interval = "1h",
    [int]$LookbackDays = 45,
    [switch]$UseTestnet = $true,
    [switch]$UseDatabase = $false,
    [switch]$UseSocketStream = $true,
    [switch]$InsecureSocketSsl = $false
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$scanArgs = @(
    "scripts\live_scan_binance.py",
    "--loop",
    "--sleep", "$ScanSeconds",
    "--interval", "$Interval",
    "--lookback-days", "$LookbackDays",
    "--transport", "powershell"
)

if ($UseTestnet) {
    $scanArgs += "--testnet"
}
if ($UseDatabase) {
    $scanArgs += "--database"
}

if ($UseSocketStream) {
    $streamArgs = @(
        "scripts\binance_stream.py",
        "--interval", "1m",
        "--write-seconds", "2"
    )
    if ($InsecureSocketSsl) {
        $streamArgs += "--insecure-ssl"
    }
    Start-Process -WindowStyle Hidden -FilePath "python" -ArgumentList $streamArgs -WorkingDirectory $Root
}

Start-Process -WindowStyle Hidden -FilePath "python" -ArgumentList $scanArgs -WorkingDirectory $Root
Start-Process -WindowStyle Hidden -FilePath "python" -ArgumentList @(
    "-m", "streamlit", "run", "aegis_trader/dashboards/app.py",
    "--server.port", "$DashboardPort",
    "--server.address", "localhost"
) -WorkingDirectory $Root

Write-Host "mytradingmind.ai live dashboard: http://localhost:$DashboardPort"
Write-Host "Socket stream: $(if ($UseSocketStream) { 'Binance websocket enabled' } else { 'disabled' })"
Write-Host "Socket TLS: $(if ($InsecureSocketSsl) { 'insecure local dev mode' } else { 'verified' })"
Write-Host "Scanner refresh: every $ScanSeconds seconds"
Write-Host "Market data source: $(if ($UseTestnet) { 'Binance Spot Testnet' } else { 'Binance public market data' })"
Write-Host "MariaDB writes: $(if ($UseDatabase) { 'enabled' } else { 'disabled' })"
