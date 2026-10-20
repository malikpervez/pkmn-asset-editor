# One-command setup on Windows 10/11.
#
#   Right-click PowerShell -> "Run as administrator", then:
#     Set-ExecutionPolicy -Scope Process Bypass; .\scripts\install-windows.ps1
#
# Pokémon decomps build on Linux, so this installs WSL (a Linux system inside Windows, made by Microsoft) with
# Ubuntu, then runs scripts/install.sh inside it. Your browser on Windows opens the editor at http://localhost:8765.

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

function Say($text) { Write-Host "`n$text" -ForegroundColor Cyan }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

$distros = (wsl.exe --list --quiet 2>$null) -replace "`0", "" | Where-Object { $_ -match "\S" }
if (-not ($distros -match "Ubuntu")) {
    if (-not $isAdmin) {
        Write-Host "Please run PowerShell as administrator the first time (it needs to turn WSL on)." -ForegroundColor Yellow
        exit 1
    }
    Say "1/3  Turning on WSL and installing Ubuntu (this can take a while)…"
    wsl.exe --install -d Ubuntu
    Write-Host @"

Windows may ask you to restart. After restarting:
  1. Ubuntu opens by itself: pick a user name and password (you'll need the password below).
  2. Run this script again.
"@ -ForegroundColor Yellow
    exit 0
}

Say "2/3  Installing the game tools and pae inside Ubuntu…"
# the repository as Ubuntu sees it, e.g. C:\Users\me\pkmn-asset-editor -> /mnt/c/Users/me/pkmn-asset-editor
$wslRepo = (wsl.exe -d Ubuntu wslpath -a "$repo").Trim()
wsl.exe -d Ubuntu -- bash -lc "bash '$wslRepo/scripts/install.sh'"
if ($LASTEXITCODE -ne 0) { Write-Host "The install inside Ubuntu failed (see above)." -ForegroundColor Red; exit 1 }

Say "3/3  All done!"
Write-Host @"
To edit your game:
  1. Open "Ubuntu" from the Start menu.
  2. Get your game there (faster than using C:\): git clone <your game's repository URL>
  3. cd into it, then run:   pae doctor --fix
                             pae serve
  4. Open http://localhost:8765 in your browser.

Your Ubuntu files are in File Explorer under "Linux" (\\wsl$\Ubuntu\home\<you>).
"@
