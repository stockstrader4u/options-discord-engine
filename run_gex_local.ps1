# Local fallback for the daily GEX/VEX post when the hosting platform is down.
# Reads KEY=VALUE lines from .env.gex (gitignored) then runs the production script.
# Needs: ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY, GEX_DISCORD_WEBHOOK.
# Optional: DATABASE_URL (Since Yesterday comparisons), OPS_DISCORD_WEBHOOK.
Set-Location $PSScriptRoot
if (-not (Test-Path .env.gex)) { Write-Error ".env.gex not found"; exit 1 }
Get-Content .env.gex | Where-Object { $_ -match '^\s*[A-Z_]+=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2
    Set-Item -Path "Env:$($k.Trim())" -Value $v.Trim()
}
python run_with_alert.py gex_vex_unified_daily.py
