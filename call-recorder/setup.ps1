<#
.SYNOPSIS
    Installiert CallRecorder in eine virtuelle Python-Umgebung (.venv) neben diesem Skript.

.DESCRIPTION
    - prüft Python (Standard 3.12, über den py-Launcher)
    - legt .venv an und installiert alle gepinnten Abhängigkeiten
    - installiert die NVIDIA-Bibliotheken (cuBLAS/cuDNN) für die GPU-Transkription
    - legt die Konfigurationsdatei an und lädt die Sprechertrennungs-Modelle
    - erstellt eine Startmenü-Verknüpfung (optional Autostart)

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -NoCuda -Autostart
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WithWhisperModel
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12",
    [switch]$NoCuda,
    [switch]$WithWhisperModel,
    [switch]$Autostart,
    [switch]$SkipModels
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Befehl fehlgeschlagen (Exit-Code $LASTEXITCODE): $Exe $($Arguments -join ' ')"
    }
}

# --- 1. Python ----------------------------------------------------------------
Write-Step "Prüfe Python $PythonVersion"
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) {
    throw "Der Python-Launcher 'py' wurde nicht gefunden. Bitte Python $PythonVersion von https://www.python.org/downloads/windows/ installieren (Option 'tcl/tk and IDLE' aktiviert lassen) oder: winget install Python.Python.$PythonVersion"
}
& py "-$PythonVersion" -c "import sys, tkinter; print(sys.version)"
if ($LASTEXITCODE -ne 0) {
    throw "Python $PythonVersion (mit tkinter) ist nicht installiert. Installation: winget install Python.Python.$PythonVersion"
}

# --- 2. venv --------------------------------------------------------------------
$Venv = Join-Path $Root ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$VenvPythonW = Join-Path $Venv "Scripts\pythonw.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Step "Lege virtuelle Umgebung an: $Venv"
    Invoke-Checked "py" @("-$PythonVersion", "-m", "venv", $Venv)
}

Write-Step "Aktualisiere pip"
Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip")

Write-Step "Installiere Abhängigkeiten (requirements.txt)"
Invoke-Checked $VenvPython @("-m", "pip", "install", "-r", "requirements.txt", "-c", "constraints-win.txt")

if (-not $NoCuda) {
    Write-Step "Installiere NVIDIA cuBLAS/cuDNN für die GPU (ca. 1,5 GB)"
    Invoke-Checked $VenvPython @("-m", "pip", "install", "-r", "requirements-cuda.txt", "-c", "constraints-win.txt")
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($smi) {
        & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
    } else {
        Write-Warning "nvidia-smi nicht gefunden - ist der NVIDIA-Treiber installiert? Ohne Treiber wird automatisch die CPU verwendet."
    }
}

Write-Step "Installiere CallRecorder selbst"
Invoke-Checked $VenvPython @("-m", "pip", "install", "--no-deps", "-e", ".")

# --- 3. Prüfung -------------------------------------------------------------------
Write-Step "Prüfe Importe"
Invoke-Checked $VenvPython @("-c", "import callrec, pyaudiowpatch, pycaw.pycaw, faster_whisper, sherpa_onnx, msal, msal_extensions, pystray, tkinter; print('Importe OK')")
if (-not $NoCuda) {
    & $VenvPython -c "from callrec.asr.cuda import setup_cuda_dll_paths; d = setup_cuda_dll_paths(); import ctranslate2; print('NVIDIA-DLL-Ordner:', len(d), '| CUDA-Geräte:', ctranslate2.get_cuda_device_count())"
}

# --- 4. Konfiguration und Modelle ---------------------------------------------------
Write-Step "Lege Konfiguration an"
Invoke-Checked $VenvPython @("-m", "callrec", "config", "path")

if (-not $SkipModels) {
    Write-Step "Lade Modelle für die Sprechertrennung (ca. 35 MB)"
    $modelArgs = @("-m", "callrec", "models", "download")
    if ($WithWhisperModel) { $modelArgs += "--whisper" }
    Invoke-Checked $VenvPython $modelArgs
}

# --- 5. Verknüpfungen ---------------------------------------------------------------
Write-Step "Erstelle Startmenü-Verknüpfung"
$shell = New-Object -ComObject WScript.Shell
$programs = [Environment]::GetFolderPath("Programs")
$lnkPath = Join-Path $programs "CallRecorder.lnk"
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $VenvPythonW
$lnk.Arguments = "-m callrec tray"
$lnk.WorkingDirectory = $Root
$lnk.Description = "CallRecorder - lokale Gesprächsaufnahme"
$lnk.Save()
Write-Host "  $lnkPath"

if ($Autostart) {
    $startup = [Environment]::GetFolderPath("Startup")
    $auto = Join-Path $startup "CallRecorder.lnk"
    Copy-Item $lnkPath $auto -Force
    Write-Host "  Autostart: $auto"
}

Write-Step "Fertig"
Write-Host "Nächste Schritte (siehe README.md):"
Write-Host "  1. Konfiguration bearbeiten:   notepad `"$env:APPDATA\CallRecorder\config.toml`""
Write-Host "  2. Audiogeräte prüfen:         .venv\Scripts\python -m callrec devices"
Write-Host "  3. Bei Microsoft anmelden:     .venv\Scripts\python -m callrec login"
Write-Host "  4. Starten:                    Startmenü > CallRecorder"
