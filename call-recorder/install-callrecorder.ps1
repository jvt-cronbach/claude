<#
.SYNOPSIS
    Einmalige Installation von CallRecorder als Git-Arbeitsordner (ersetzt das Entpacken von ZIP-Dateien).

.DESCRIPTION
    Danach aktualisiert sich CallRecorder aus der App heraus ("Hilfe > Nach Updates suchen ...").
    - prüft Git (Git für Windows, enthält den Git Credential Manager für private Repositories)
    - klont das Repository nach -InstallDir (nur der Ordner call-recorder, Dateien bei Bedarf)
    - liest call-recorder/release.json vom Zweig und checkt GENAU den dort freigegebenen Stand aus
      (nie den neuesten Stand des Zweigs); prüft, dass er zum Zweig gehört und dieselbe Version meldet
    - führt call-recorder\setup.ps1 aus (venv, Abhängigkeiten, Verknüpfung, ggf. Autostart)
    Einstellungen, API-Schlüssel, Modelle, Stimmprofile und Aufnahmen liegen außerhalb des
    Programmordners (%APPDATA%, %LOCALAPPDATA%, Aufnahmeordner) und bleiben unverändert.
    Bestehender Autostart-Eintrag und Startmenü-Verknüpfung werden auf den neuen Ordner umgestellt.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install-callrecorder.ps1
    powershell -ExecutionPolicy Bypass -File .\install-callrecorder.ps1 -InstallDir "C:\Tools\CallRecorder" -NoCuda
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\CallRecorder"),
    [string]$RepoUrl = "https://github.com/jvt-cronbach/claude",
    [string]$Branch = "claude/cool-maxwell-zmzy87",
    [string]$PythonVersion = "3.12",
    [switch]$NoCuda,
    [switch]$WithCuda,
    [switch]$Autostart
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:GIT_HTTP_LOW_SPEED_LIMIT = "1000"
$env:GIT_HTTP_LOW_SPEED_TIME = "30"
$TipRef = "refs/callrec/update-tip"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Invoke-Git([string[]]$Arguments) {
    $out = & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') fehlgeschlagen (Exit-Code $LASTEXITCODE). Ist GitHub erreichbar (VPN/Proxy)? Bei einem privaten Repository: im Anmeldefenster von Git bei GitHub anmelden."
    }
    return $out
}

if ($RepoUrl -notmatch '^https://github\.com/[\w.-]+/[\w.-]+(\.git)?$') {
    throw "Nur HTTPS-Adressen auf github.com sind erlaubt: $RepoUrl"
}

# --- 1. Voraussetzungen -----------------------------------------------------------
Write-Step "Prüfe Git"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git ist nicht installiert. Bitte installieren: winget install Git.Git - danach PowerShell neu öffnen und dieses Skript erneut starten."
}
& git --version

$running = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'callrec\s+tray' })
if ($running.Count -gt 0) {
    throw "CallRecorder läuft noch. Bitte zuerst beenden (Infobereich: Rechtsklick > Beenden) und dann erneut starten."
}

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$oldAuto = (Get-ItemProperty -Path $runKey -Name "CallRecorder" -ErrorAction SilentlyContinue).CallRecorder
if ($oldAuto) {
    Write-Host "Vorhandener Autostart-Eintrag: $oldAuto (wird auf den neuen Ordner umgestellt)"
    $Autostart = $true
}

# --- 2. Klonen / holen ------------------------------------------------------------
if (Test-Path (Join-Path $InstallDir ".git")) {
    Write-Step "Vorhandenen Git-Ordner verwenden: $InstallDir"
} else {
    if ((Test-Path $InstallDir) -and (Get-ChildItem $InstallDir -Force | Select-Object -First 1)) {
        throw "$InstallDir existiert und ist nicht leer. Bitte einen leeren oder neuen Ordner angeben (-InstallDir)."
    }
    Write-Step "Klone $RepoUrl ($Branch) nach $InstallDir"
    Invoke-Git @("clone", "--filter=blob:none", "--no-checkout", "--branch", $Branch, $RepoUrl, $InstallDir) | Out-Host
    Invoke-Git @("-C", $InstallDir, "sparse-checkout", "init", "--cone") | Out-Host
    Invoke-Git @("-C", $InstallDir, "sparse-checkout", "set", "call-recorder") | Out-Host
}

Write-Step "Lese die Freigabe (release.json)"
# same named remote the app uses ("callrec-update"); its URL always comes from here / config.toml
# (PowerShell 5.1: redirecting stderr of a native command under "Stop" would throw - relax it here)
$ErrorActionPreference = "Continue"
& git -C $InstallDir remote get-url callrec-update 2>&1 | Out-Null
$hasRemote = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $hasRemote) {
    Invoke-Git @("-C", $InstallDir, "remote", "add", "callrec-update", $RepoUrl) | Out-Host
} else {
    Invoke-Git @("-C", $InstallDir, "remote", "set-url", "callrec-update", $RepoUrl) | Out-Host
}
Invoke-Git @("-C", $InstallDir, "fetch", "--no-tags", "--filter=blob:none", "callrec-update", "+refs/heads/${Branch}:$TipRef") | Out-Host
$releaseText = (Invoke-Git @("-C", $InstallDir, "show", "${TipRef}:call-recorder/release.json")) -join "`n"
$release = $releaseText | ConvertFrom-Json
if (-not $release.commit) {
    throw "Es ist noch keine Version freigegeben (release.json nennt keinen Stand). Bitte später erneut versuchen."
}
$sha = "$($release.commit)".ToLower()
if ($sha -notmatch '^[0-9a-f]{40}$') { throw "release.json nennt keinen gültigen Commit." }
& git -C $InstallDir merge-base --is-ancestor $sha $TipRef
if ($LASTEXITCODE -ne 0) { throw "Der freigegebene Stand $sha gehört nicht zum Zweig $Branch - abgebrochen." }
$init = (Invoke-Git @("-C", $InstallDir, "show", "${sha}:call-recorder/src/callrec/__init__.py")) -join "`n"
if ($init -notmatch "__version__\s*=\s*[`"']([^`"']+)[`"']" -or $Matches[1] -ne $release.version) {
    throw "Der Stand $sha meldet nicht die freigegebene Version $($release.version) - abgebrochen."
}
Write-Host "Freigegeben: Version $($release.version) ($($sha.Substring(0, 10))) vom $($release.date)"

Write-Step "Checke genau diesen Stand aus"
Invoke-Git @("-C", $InstallDir, "-c", "advice.detachedHead=false", "checkout", "--detach", $sha) | Out-Host
$head = (Invoke-Git @("-C", $InstallDir, "rev-parse", "HEAD")) -join ""
if ($head.Trim() -ne $sha) { throw "Ausgecheckt ist $head statt $sha - abgebrochen." }

# --- 3. Installieren --------------------------------------------------------------
$appDir = Join-Path $InstallDir "call-recorder"
Write-Step "Führe setup.ps1 aus"
$setupArgs = @{ PythonVersion = $PythonVersion }
if ($NoCuda) { $setupArgs.NoCuda = $true }
if ($WithCuda) { $setupArgs.WithCuda = $true }
if ($Autostart) { $setupArgs.Autostart = $true }
& (Join-Path $appDir "setup.ps1") @setupArgs

Write-Step "Fertig"
Write-Host "CallRecorder $($release.version) ist installiert in: $appDir"
Write-Host "Updates künftig im Hauptfenster: Hilfe > Nach Updates suchen ..."
if ($oldAuto -and ($oldAuto -notlike "*$appDir*")) {
    Write-Host "Der bisherige Programmordner (aus dem alten Autostart-Eintrag: $oldAuto) wird nicht mehr benutzt"
    Write-Host "und kann gelöscht werden. Einstellungen, Modelle, Stimmprofile und Aufnahmen liegen nicht darin."
}
