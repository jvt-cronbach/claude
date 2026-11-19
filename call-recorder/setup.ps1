<#
.SYNOPSIS
    Installiert CallRecorder in eine virtuelle Python-Umgebung (.venv) neben diesem Skript.

.DESCRIPTION
    - prüft Python (Standard 3.12, über den py-Launcher)
    - legt .venv an und installiert alle gepinnten Abhängigkeiten
    - installiert die NVIDIA-Bibliotheken (cuBLAS/cuDNN) für die GPU-Transkription - nur wenn eine
      NVIDIA-Grafikkarte erkannt wird (-NoCuda: nie, -WithCuda: immer)
    - legt die Konfigurationsdatei an und lädt die Sprechertrennungs-Modelle
    - erstellt eine Startmenü-Verknüpfung (optional Autostart)

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -NoCuda -Autostart
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WithWhisperModel
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WithCuda
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12",
    [switch]$NoCuda,
    [switch]$WithCuda,
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

if ($NoCuda -and $WithCuda) {
    throw "-NoCuda und -WithCuda schließen sich aus."
}

# --- 0. NVIDIA-Grafikkarte? --------------------------------------------------------
# Die CUDA-Pakete (ca. 1,5 GB) nützen nur mit einer NVIDIA-GPU. Erkennung über die Grafikadapter
# (WMI/CIM) bzw. nvidia-smi; -WithCuda erzwingt die Installation, -NoCuda verhindert sie.
function Test-NvidiaGpu {
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { return $true }
    try {
        $adapters = @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop)
        foreach ($a in $adapters) {
            if ("$($a.Name) $($a.AdapterCompatibility)" -match 'NVIDIA') { return $true }
        }
    } catch {
        Write-Warning "Grafikadapter konnten nicht abgefragt werden: $($_.Exception.Message)"
    }
    return $false
}
$HasNvidia = Test-NvidiaGpu
if ($WithCuda) {
    $InstallCuda = $true
} elseif ($NoCuda) {
    $InstallCuda = $false
} else {
    $InstallCuda = $HasNvidia
}
if ($HasNvidia) {
    Write-Host "NVIDIA-Grafikkarte erkannt." -ForegroundColor Green
} else {
    Write-Host "Keine NVIDIA-Grafikkarte erkannt - Transkription läuft auf der CPU (Modell large-v3-turbo)." -ForegroundColor Yellow
}
if (-not $InstallCuda) {
    Write-Host "NVIDIA-Bibliotheken werden nicht installiert (erzwingen mit -WithCuda)."
}

# --- 1. Python ----------------------------------------------------------------
Write-Step "Prüfe Python $PythonVersion"
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) {
    throw "Der Python-Launcher 'py' wurde nicht gefunden. Bitte Python $PythonVersion von https://www.python.org/downloads/windows/ installieren (Option 'tcl/tk and IDLE' aktiviert lassen) oder: winget install Python.Python.$PythonVersion"
}
if ($PythonVersion -notmatch '^3\.(12|13)$') {
    throw "Python $PythonVersion wird nicht unterstützt: die gepinnten Pakete (u. a. numpy 2.5) gibt es nur für Python 3.12 und 3.13 (64 Bit)."
}
& py "-$PythonVersion" -c "import sys, struct, tkinter; print(sys.version); sys.exit(0 if struct.calcsize('P') == 8 else 3)"
if ($LASTEXITCODE -eq 3) {
    throw "Python $PythonVersion ist eine 32-Bit-Installation. Bitte die 64-Bit-Version installieren: winget install Python.Python.$PythonVersion"
}
if ($LASTEXITCODE -ne 0) {
    throw "Python $PythonVersion (mit tkinter) ist nicht installiert. Installation: winget install Python.Python.$PythonVersion"
}
# ctranslate2 (faster-whisper) needs the Visual C++ runtime (msvcp140.dll), which Python itself does not ship.
if (-not (Test-Path (Join-Path $env:SystemRoot "System32\msvcp140.dll"))) {
    Write-Warning "Visual C++ Runtime (msvcp140.dll) fehlt - ohne sie startet die Transkription nicht. Installation: winget install Microsoft.VCRedist.2015+.x64"
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

if ($InstallCuda) {
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
Invoke-Checked $VenvPython @("-c", "import callrec, pyaudiowpatch, pycaw.pycaw, faster_whisper, sherpa_onnx, msal, msal_extensions, pystray, tkinter, anthropic; print('Importe OK')")
if ($InstallCuda) {
    & $VenvPython -c "from callrec.asr.cuda import setup_cuda_dll_paths; d = setup_cuda_dll_paths(); import ctranslate2; print('NVIDIA-DLL-Ordner:', len(d), '| CUDA-Geräte:', ctranslate2.get_cuda_device_count())"
}
# GPU-Prüfung einmal ausführen und merken (CallRecorder nutzt das Ergebnis; erneut prüfen: callrec gpu --recheck)
& $VenvPython -m callrec gpu --recheck

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
