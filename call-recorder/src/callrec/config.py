"""Configuration (TOML in %APPDATA%\\CallRecorder\\config.toml).

There is intentionally *no* option to skip or pre-answer the consent dialog;
keys that look like an attempt to do so are reported and ignored.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from callrec import paths

log = logging.getLogger(__name__)


class ConfigError(ValueError):
    pass


@dataclass
class GeneralConfig:
    user_name: str = ""  # empty -> taken from Microsoft Graph /me
    user_email: str = ""
    recordings_dir: str = ""  # empty -> %USERPROFILE%\Anrufaufnahmen
    preset: str = ""  # name of a preset in %APPDATA%\CallRecorder\presets (empty = none)


@dataclass
class AudioConfig:
    mic_device: str = "auto"  # "auto" = Windows default communications capture device
    loopback_device: str = "auto"  # "auto" = default communications render device
    format: str = "flac"  # flac | opus
    write_mix: bool = True
    keep_wav: bool = False
    gap_threshold_s: float = 0.5
    frames_per_buffer: int = 2048


@dataclass
class CalendarConfig:
    client_id: str = ""
    tenant_id: str = "organizations"
    tolerance_before_min: int = 10  # meeting starting within N minutes counts as "now"
    tolerance_after_min: int = 10  # meeting that ended up to N minutes ago still counts
    request_timeout_s: float = 8.0


@dataclass
class TranscriptionConfig:
    model: str = "large-v3"
    device: str = "auto"  # auto | cuda | cpu
    compute_type: str = "auto"  # auto -> float16 (cuda) / int8 (cpu)
    language: str = "de"  # ISO code or "auto"
    beam_size: int = 5
    vad_filter: bool = True
    condition_on_previous_text: bool = False
    use_participant_names_as_prompt: bool = True
    initial_prompt: str = ""  # extra context text, e.g. "Besprechung der Firma X zu Projekt Y."
    use_glossary: bool = True  # glossary.txt -> faster-whisper "hotwords"
    vad_threshold: float = 0.5
    vad_min_silence_ms: int = 2000
    vad_speech_pad_ms: int = 400
    hallucination_silence_threshold: float = 0.0  # 0 = off (seconds)


@dataclass
class DiarizationConfig:
    enabled: bool = True
    embedding_model: str = "wespeaker_en_voxceleb_resnet34_LM"
    cluster_threshold: float = 0.4
    num_speakers: int = 0  # 0 = determine automatically
    min_duration_on: float = 0.3
    min_duration_off: float = 0.5
    num_threads: int = 2


@dataclass
class TranscriptRenderConfig:
    timestamp_format: str = "hh:mm:ss"  # hh:mm:ss | mm:ss | none
    merge_same_speaker: bool = True
    merge_max_gap_s: float = 1.5  # merge adjacent utterances of one speaker up to this pause
    split_pause_s: float = 2.0  # start a new paragraph after this pause
    max_paragraph_s: float = 60.0
    apply_replacements: bool = True  # replacements.txt


@dataclass
class SpeakerConfig:
    match_threshold: float = 0.55
    invitee_bonus: float = 0.05
    auto_update_on_match: bool = True
    auto_update_threshold: float = 0.70


@dataclass
class SummaryConfig:
    enabled: bool = True
    provider: str = "ollama"  # ollama | openai (OpenAI-compatible local server: LM Studio, llama.cpp ...)
    ollama_url: str = "http://127.0.0.1:11434"
    openai_url: str = "http://127.0.0.1:1234/v1"
    openai_api_key: str = ""  # most local servers ignore it
    # Leaving this False restricts endpoints to localhost/loopback. Setting it True sends
    # transcript text to another machine - only for a server you fully control.
    allow_remote_endpoint_data_leaves_this_computer: bool = False
    model: str = "gemma3:12b"
    num_ctx: int = 16384
    temperature: float = 0.2
    max_chunk_chars: int = 24000
    timeout_s: float = 900.0


@dataclass
class HandoffConfig:
    mode: str = "folder"  # folder (OneDrive-synced SharePoint library) | graph (direct upload)
    folder: str = ""  # local folder synced by OneDrive, e.g. C:\Users\me\Firma\Team - Protokolle
    graph_drive_id: str = ""  # drive (document library) id, see README
    graph_folder: str = ""  # folder path inside the library, e.g. "Gesprächsprotokolle"
    graph_scope: str = "Sites.Selected"  # or Files.ReadWrite.All (broader)
    include_participant_emails: bool = False


@dataclass
class DetectionConfig:
    enabled: bool = True
    teams_processes: list[str] = field(default_factory=lambda: ["ms-teams.exe", "teams.exe"])
    poll_interval_s: float = 3.0
    calendar_reminder: bool = True
    cooldown_minutes: int = 10


@dataclass
class HotkeyConfig:
    toggle: str = "ctrl+alt+shift+r"


@dataclass
class UiConfig:
    show_overlay: bool = True
    open_review_after_processing: bool = True


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    diarization: DiarizationConfig = field(default_factory=DiarizationConfig)
    speakers: SpeakerConfig = field(default_factory=SpeakerConfig)
    transcript: TranscriptRenderConfig = field(default_factory=TranscriptRenderConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)
    handoff: HandoffConfig = field(default_factory=HandoffConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    warnings: list[str] = field(default_factory=list, compare=False, repr=False)
    active_preset: str = field(default="", compare=False, repr=False)

    def recordings_path(self) -> Path:
        if self.general.recordings_dir.strip():
            return Path(self.general.recordings_dir).expanduser()
        return paths.default_recordings_dir()

    def validate(self) -> None:
        if self.audio.format not in ("flac", "opus"):
            raise ConfigError(f"audio.format muss 'flac' oder 'opus' sein, nicht {self.audio.format!r}")
        if self.transcription.device not in ("auto", "cuda", "cpu"):
            raise ConfigError("transcription.device muss 'auto', 'cuda' oder 'cpu' sein")
        if not 0.0 < self.speakers.match_threshold < 1.0:
            raise ConfigError("speakers.match_threshold muss zwischen 0 und 1 liegen")
        if self.audio.gap_threshold_s <= 0:
            raise ConfigError("audio.gap_threshold_s muss > 0 sein")
        if self.transcript.timestamp_format not in TIMESTAMP_FORMATS:
            raise ConfigError(f"transcript.timestamp_format muss einer von {', '.join(TIMESTAMP_FORMATS)} sein")
        if self.summary.provider not in ("ollama", "openai"):
            raise ConfigError("summary.provider muss 'ollama' oder 'openai' sein")
        if self.handoff.mode not in ("folder", "graph"):
            raise ConfigError("handoff.mode muss 'folder' oder 'graph' sein")
        if self.handoff.graph_scope not in GRAPH_FILE_SCOPES:
            raise ConfigError(f"handoff.graph_scope muss einer von {', '.join(GRAPH_FILE_SCOPES)} sein")
        if self.transcription.beam_size < 1:
            raise ConfigError("transcription.beam_size muss >= 1 sein")


TIMESTAMP_FORMATS = ("hh:mm:ss", "mm:ss", "none")
GRAPH_FILE_SCOPES = ("Sites.Selected", "Files.ReadWrite.All")


_SECTION_TYPES: dict[str, type] = {
    "general": GeneralConfig,
    "audio": AudioConfig,
    "calendar": CalendarConfig,
    "transcription": TranscriptionConfig,
    "diarization": DiarizationConfig,
    "speakers": SpeakerConfig,
    "transcript": TranscriptRenderConfig,
    "summary": SummaryConfig,
    "handoff": HandoffConfig,
    "detection": DetectionConfig,
    "hotkey": HotkeyConfig,
    "ui": UiConfig,
}


def _coerce(section: str, key: str, value: Any, default: Any) -> Any:
    where = f"{section}.{key}"
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: true/false erwartet")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: ganze Zahl erwartet")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: Zahl erwartet")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{where}: Text erwartet")
        return value
    if isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where}: Liste von Texten erwartet")
        return list(value)
    return value


# Sections a preset may change (tuning only - never devices, calendar or hand-off).
PRESET_SECTIONS = ("transcription", "diarization", "speakers", "transcript", "summary")


def config_from_dict(data: dict[str, Any], base: Config | None = None) -> Config:
    cfg = copy.deepcopy(base) if base is not None else Config()
    warnings: list[str] = list(cfg.warnings)
    for section, values in data.items():
        if "consent" in section.lower():
            warnings.append(
                f"Abschnitt [{section}] ignoriert: Die Einwilligungsbestätigung kann nicht abgeschaltet werden."
            )
            continue
        section_type = _SECTION_TYPES.get(section)
        if section_type is None or not isinstance(values, dict):
            warnings.append(f"Unbekannter Abschnitt [{section}] ignoriert")
            continue
        target = getattr(cfg, section)
        defaults = {f.name: getattr(target, f.name) for f in dataclasses.fields(target)}
        for key, value in values.items():
            if "consent" in key.lower() or "einwillig" in key.lower():
                warnings.append(
                    f"{section}.{key} ignoriert: Die Einwilligungsbestätigung kann nicht abgeschaltet werden."
                )
                continue
            if key not in defaults:
                warnings.append(f"Unbekannte Einstellung {section}.{key} ignoriert")
                continue
            setattr(target, key, _coerce(section, key, value, defaults[key]))
    cfg.warnings = warnings
    cfg.validate()
    for w in warnings:
        log.warning(w)
    return cfg


def config_to_dict(cfg: Config) -> dict[str, Any]:
    return {name: dataclasses.asdict(getattr(cfg, name)) for name in _SECTION_TYPES}


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        try:
            return tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Datei {path} ist ungültig: {exc}") from exc


def apply_preset(cfg: Config, preset: dict[str, Any], name: str) -> Config:
    """Overlay a preset (only the tuning sections) onto ``cfg``."""
    allowed = {k: v for k, v in preset.items() if k in PRESET_SECTIONS}
    ignored = sorted(set(preset) - set(allowed))
    out = config_from_dict(allowed, base=cfg)
    for k in ignored:
        out.warnings.append(f"Preset {name!r}: Abschnitt [{k}] ist in Presets nicht erlaubt und wurde ignoriert")
    out.active_preset = name
    return out


def load_config(path: Path | None = None, preset: str | None = None) -> Config:
    """Load config.toml and overlay the preset (argument wins over general.preset)."""
    path = path or paths.config_file()
    if not path.exists():
        write_default_config(path)
        cfg = Config()
    else:
        cfg = config_from_dict(_read_toml(path))
    name = preset if preset is not None else cfg.general.preset.strip()
    if name:
        ppath = paths.presets_dir() / f"{name}.toml"
        if not ppath.exists():
            raise ConfigError(f"Preset {name!r} nicht gefunden ({ppath})")
        cfg = apply_preset(cfg, _read_toml(ppath), name)
    return cfg


def list_presets() -> list[str]:
    d = paths.presets_dir()
    return sorted(p.stem for p in d.glob("*.toml")) if d.exists() else []


def save_preset(cfg: Config, name: str) -> Path:
    """Store the tuning sections of ``cfg`` as a named preset."""
    if not name.strip() or any(c in name for c in '<>:"/\\|?*'):
        raise ConfigError("Ungültiger Preset-Name")
    d = paths.presets_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name.strip()}.toml"
    data = {k: v for k, v in config_to_dict(cfg).items() if k in PRESET_SECTIONS}
    with path.open("wb") as fh:
        tomli_w.dump(data, fh)
    return path


def save_config(cfg: Config, path: Path | None = None) -> None:
    """Write the config (comments of the template are lost)."""
    path = path or paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(config_to_dict(cfg), fh)
    tmp.replace(path)


DEFAULT_CONFIG_TEMPLATE = r"""# Konfiguration für CallRecorder
# Änderungen werden beim nächsten Start wirksam (Tray-Menü: Beenden, dann neu starten).
# Hinweis: Die Einwilligungsabfrage vor jeder Aufnahme lässt sich bewusst NICHT abschalten.

[general]
# Ihr Name im Transkript. Leer = Anzeigename aus dem Microsoft-Konto.
user_name = ""
user_email = ""
# Aktives Preset (Datei %APPDATA%\CallRecorder\presets\<Name>.toml), leer = keins
preset = ""
# Ablageordner für Aufnahmen. Leer = %USERPROFILE%\Anrufaufnahmen
# (bitte keinen OneDrive-Ordner verwenden, sonst landen Aufnahmen in der Cloud).
recordings_dir = ""

[audio]
# "auto" = Windows-Standardgerät für Kommunikation, sonst (Teil des) Gerätenamens.
mic_device = "auto"
loopback_device = "auto"
# Zielformat nach der Aufnahme: "flac" (verlustfrei) oder "opus" (sehr klein)
format = "flac"
write_mix = true
keep_wav = false
gap_threshold_s = 0.5
frames_per_buffer = 2048

[calendar]
# Aus der Entra-App-Registrierung (siehe README). Leer = Kalender deaktiviert.
client_id = ""
# Verzeichnis-ID (Mandanten-ID) oder "organizations"
tenant_id = "organizations"
tolerance_before_min = 10
tolerance_after_min = 10
request_timeout_s = 8.0

[transcription]
model = "large-v3"
# "auto" = GPU (CUDA), bei Problemen automatisch CPU
device = "auto"
compute_type = "auto"
# "de" oder "auto" für automatische Spracherkennung
language = "de"
beam_size = 5
vad_filter = true
condition_on_previous_text = false
use_participant_names_as_prompt = true
# Zusätzlicher Kontext für Whisper (Thema, Firmenname ...). Eigennamen besser ins Glossar
# (%APPDATA%\CallRecorder\glossary.txt, ein Begriff pro Zeile).
initial_prompt = ""
use_glossary = true
# Sprachaktivitätserkennung (VAD): Schwelle 0..1, Mindestpause und Polster in ms
vad_threshold = 0.5
vad_min_silence_ms = 2000
vad_speech_pad_ms = 400
# Sekunden; 0 = aus. Unterdrückt erfundenen Text in langen Pausen.
hallucination_silence_threshold = 0.0

[diarization]
enabled = true
embedding_model = "wespeaker_en_voxceleb_resnet34_LM"
# kleiner = mehr Sprecher werden unterschieden (zu viele werden bei der Zuordnung zusammengeführt)
cluster_threshold = 0.4
# 0 = automatisch
num_speakers = 0
min_duration_on = 0.3
min_duration_off = 0.5
num_threads = 2

[speakers]
# Ab welcher Ähnlichkeit (Kosinus, 0..1) eine Stimme einem Profil zugeordnet wird
match_threshold = 0.55
# Bonus für Personen, die in der Einladung stehen
invitee_bonus = 0.05
auto_update_on_match = true
auto_update_threshold = 0.70

[transcript]
# Zeitstempel: "hh:mm:ss", "mm:ss" oder "none"
timestamp_format = "hh:mm:ss"
# aufeinanderfolgende Beiträge derselben Person zusammenfassen (bis zu dieser Pause in s)
merge_same_speaker = true
merge_max_gap_s = 1.5
# neuer Absatz nach dieser Pause bzw. spätestens nach dieser Dauer (s)
split_pause_s = 2.0
max_paragraph_s = 60.0
# Korrekturliste %APPDATA%\CallRecorder\replacements.txt anwenden
apply_replacements = true

[summary]
enabled = true
# "ollama" oder "openai" (OpenAI-kompatibler lokaler Server, z.B. LM Studio, llama.cpp)
provider = "ollama"
ollama_url = "http://127.0.0.1:11434"
openai_url = "http://127.0.0.1:1234/v1"
openai_api_key = ""
# Nur lokale Adressen (localhost/127.0.0.1/::1) sind erlaubt. NUR auf true setzen, wenn der
# Server auf einem Rechner läuft, den Sie vollständig kontrollieren: Dann VERLASSEN
# GESPRÄCHSINHALTE DIESEN RECHNER.
allow_remote_endpoint_data_leaves_this_computer = false
# Vorlagen für die Zusammenfassung: %APPDATA%\CallRecorder\prompts\
model = "gemma3:12b"
num_ctx = 16384
temperature = 0.2
max_chunk_chars = 24000
timeout_s = 900.0

[handoff]
# Übergabe der Zusammenfassung (NUR summary, nie Transkript/Audio) an SharePoint - nur auf Klick.
# "folder" = in einen von OneDrive synchronisierten SharePoint-Ordner kopieren (empfohlen)
# "graph"  = direkt über Microsoft Graph hochladen (zusätzliche Berechtigung, siehe README)
mode = "folder"
folder = ""
graph_drive_id = ""
graph_folder = ""
# "Sites.Selected" (nur freigegebene Website, empfohlen) oder "Files.ReadWrite.All"
graph_scope = "Sites.Selected"
include_participant_emails = false

[detection]
enabled = true
teams_processes = ["ms-teams.exe", "teams.exe"]
poll_interval_s = 3.0
calendar_reminder = true
cooldown_minutes = 10

[hotkey]
toggle = "ctrl+alt+shift+r"

[ui]
show_overlay = true
open_review_after_processing = true
"""


def write_default_config(path: Path | None = None) -> Path:
    path = path or paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return path
