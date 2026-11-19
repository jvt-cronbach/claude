"""Configuration (TOML in %APPDATA%\\CallRecorder\\config.toml).

There is intentionally *no* option to skip or pre-answer the consent dialog;
keys that look like an attempt to do so are reported and ignored.
"""

from __future__ import annotations

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
class SpeakerConfig:
    match_threshold: float = 0.55
    invitee_bonus: float = 0.05
    auto_update_on_match: bool = True
    auto_update_threshold: float = 0.70


@dataclass
class SummaryConfig:
    enabled: bool = True
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "gemma3:12b"
    num_ctx: int = 16384
    temperature: float = 0.2
    max_chunk_chars: int = 24000
    timeout_s: float = 900.0


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
    summary: SummaryConfig = field(default_factory=SummaryConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    warnings: list[str] = field(default_factory=list, compare=False, repr=False)

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


_SECTION_TYPES: dict[str, type] = {
    "general": GeneralConfig,
    "audio": AudioConfig,
    "calendar": CalendarConfig,
    "transcription": TranscriptionConfig,
    "diarization": DiarizationConfig,
    "speakers": SpeakerConfig,
    "summary": SummaryConfig,
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


def config_from_dict(data: dict[str, Any]) -> Config:
    cfg = Config()
    warnings: list[str] = []
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


def load_config(path: Path | None = None) -> Config:
    path = path or paths.config_file()
    if not path.exists():
        cfg = Config()
        write_default_config(path)
        return cfg
    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Konfigurationsdatei {path} ist ungültig: {exc}") from exc
    return config_from_dict(data)


def save_config(cfg: Config, path: Path | None = None) -> None:
    """Write the config (comments of the template are lost)."""
    path = path or paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        tomli_w.dump(config_to_dict(cfg), fh)
    tmp.replace(path)


DEFAULT_CONFIG_TEMPLATE = """\
# Konfiguration für CallRecorder
# Änderungen werden beim nächsten Start wirksam (Tray-Menü: Beenden, dann neu starten).
# Hinweis: Die Einwilligungsabfrage vor jeder Aufnahme lässt sich bewusst NICHT abschalten.

[general]
# Ihr Name im Transkript. Leer = Anzeigename aus dem Microsoft-Konto.
user_name = ""
user_email = ""
# Ablageordner für Aufnahmen. Leer = %USERPROFILE%\\Anrufaufnahmen
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

[summary]
enabled = true
# Nur lokale Adressen sind erlaubt (localhost / 127.0.0.1)
ollama_url = "http://127.0.0.1:11434"
model = "gemma3:12b"
num_ctx = 16384
temperature = 0.2
max_chunk_chars = 24000
timeout_s = 900.0

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
