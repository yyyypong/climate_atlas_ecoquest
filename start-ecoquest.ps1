$ErrorActionPreference = 'Stop'
$ecoquestRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ecoquestScript = Join-Path $ecoquestRoot 'serve_game.py'
$ecoquestPython = $null
$ecoquestBundled = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
if (Test-Path -LiteralPath $ecoquestBundled) { $ecoquestPython = $ecoquestBundled }
if (-not $ecoquestPython) {
    $ecoquestCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($ecoquestCommand -and $ecoquestCommand.Source -notlike '*WindowsApps*') { $ecoquestPython = $ecoquestCommand.Source }
}
if (-not $ecoquestPython) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show('Please install Python 3, or open Ecoquest-Climate-Atlas.html to play offline.','Ecoquest') | Out-Null
    exit 1
}
Start-Process -FilePath $ecoquestPython -ArgumentList @(('"' + $ecoquestScript + '"'),'--open') -WorkingDirectory $ecoquestRoot -WindowStyle Hidden
