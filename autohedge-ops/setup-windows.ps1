# AutoHedge runner installer for Windows. Run in PowerShell (no admin needed):
#   irm https://raw.githubusercontent.com/wardawger/turn-caddie/claude/amazing-mendel-mcxtn5/autohedge-ops/setup-windows.ps1 | iex
# Re-run to upgrade; existing keys in bot\.env are kept.
$ErrorActionPreference = "Stop"
$Branch = "claude/amazing-mendel-mcxtn5"
$Root = Join-Path $env:USERPROFILE "autohedge"

foreach ($pkg in @(@{cmd="python"; id="Python.Python.3.12"}, @{cmd="git"; id="Git.Git"})) {
  if (-not (Get-Command $pkg.cmd -ErrorAction SilentlyContinue)) {
    winget install -e --id $pkg.id --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
  }
}

if (Test-Path "$Root\repo\.git") {
  git -C "$Root\repo" fetch -q origin $Branch; git -C "$Root\repo" reset -q --hard "origin/$Branch"
} else {
  New-Item -ItemType Directory -Force $Root | Out-Null
  git clone -q -b $Branch https://github.com/wardawger/turn-caddie "$Root\repo"
}
$Ops = "$Root\repo\autohedge-ops"
if (-not (Test-Path "$Root\.venv")) { python -m venv "$Root\.venv" }
& "$Root\.venv\Scripts\pip" install -q -r "$Ops\requirements.txt"

$EnvFile = "$Ops\bot\.env"
if (-not (Test-Path $EnvFile)) {
  function Ask($name) {
    $s = Read-Host "$name" -AsSecureString
    [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
  }
  $lines = @(
    "SUPABASE_URL=https://paeredzsrygvrhblpobj.supabase.co",
    "SUPABASE_SERVICE_KEY=$(Ask 'Supabase service_role key')",
    "OPENAI_API_KEY=$(Ask 'OpenAI API key')",
    "EXA_API_KEY=$(Ask 'Exa API key')"
  )
  Set-Content -Path $EnvFile -Value $lines -Encoding ascii
  icacls $EnvFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
}

# Start at logon, restart on failure, keep running on battery.
$action   = New-ScheduledTaskAction -Execute "$Root\.venv\Scripts\python.exe" -Argument "`"$Ops\bot\runner.py`"" -WorkingDirectory "$Ops\bot"
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "AutoHedge Runner" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Stop-ScheduledTask -TaskName "AutoHedge Runner" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "AutoHedge Runner"

powercfg /change standby-timeout-ac 0
Write-Host "Done. Check the dashboard: it should show ACTIVE within a minute. Plug in the laptop and keep it logged in."
