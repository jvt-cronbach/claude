"""Configuration (TOML in %APPDATA%\\CallRecorder\\config.toml).

There is intentionally *no* option to skip or pre-answer the consent dialog;
keys that look like an attempt to do so are reported and ignored.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import re
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
    # "auto" = large-v3 on a usable NVIDIA GPU, large-v3-turbo (int8) on the CPU
    model: str = "auto"
    device: str = "auto"  # auto | cuda | cpu
    compute_type: str = "auto"  # auto -> float16 (cuda) / int8 (cpu)
    # "auto" = detect per recording, restricted to language_candidates (first entry = prior);
    # an ISO code ("de", "en", ...) forces that language. See README "Sprache".
    language: str = "auto"
    language_candidates: list[str] = field(default_factory=lambda: ["de", "en"])
    language_detection_windows: int = 6  # 30-s speech windows per track, spread over the recording
    language_switch_threshold: float = 0.75  # probability another candidate needs to beat the prior
    language_min_speech_s: float = 20.0  # less speech than this -> keep the prior
    # per-segment language detection of faster-whisper (all ~100 languages, not the candidates) -
    # experimental, for calls that switch language within one track
    multilingual: bool = False
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
    # Decoding guards against repetition loops (faster-whisper 1.2.1 defaults, now explicit):
    # a 30-s window is decoded again at the next temperature if its text compresses too well
    # (repetitive) or its average log-probability is too low.
    compression_ratio_threshold: float = 2.4  # 0 = off
    log_prob_threshold: float = -1.0  # 0 = off
    no_speech_threshold: float = 0.6  # 0 = off
    temperature_fallback: bool = True  # temperatures 0.0, 0.2 ... 1.0; False = only 0.0
    repetition_penalty: float = 1.0  # > 1 penalises repeated tokens (1 = off)
    no_repeat_ngram_size: int = 0  # forbid repeating n-grams of this size within a window (0 = off)
    # post-check: warn when a phrase repeats this often in a row (0 = off)
    repetition_warn_min_repeats: int = 4


@dataclass
class DiarizationConfig:
    enabled: bool = True
    embedding_model: str = "wespeaker_en_voxceleb_resnet34_LM"
    cluster_threshold: float = 0.4
    num_speakers: int = 0  # 0 = determine automatically
    min_duration_on: float = 0.3
    min_duration_off: float = 0.5
    num_threads: int = 0  # 0 = automatic from the physical cores (see callrec.speakers.threads)


@dataclass
class TranscriptRenderConfig:
    timestamp_format: str = "hh:mm:ss"  # hh:mm:ss | mm:ss | none
    merge_same_speaker: bool = True
    merge_max_gap_s: float = 1.5  # merge adjacent utterances of one speaker up to this pause
    split_pause_s: float = 2.0  # start a new paragraph after this pause
    max_paragraph_s: float = 60.0
    apply_replacements: bool = True  # replacements.txt
    # Name for your own (mic track) contributions in transcript and summary.
    # "" = automatic: first name from the Microsoft account (/me), else from general.user_name, else "Ich".
    self_display_name: str = ""


@dataclass
class SpeakerConfig:
    # "participants" (default): automatic assignment only to people of this recording (calendar
    # invitation or entered by you); "all_profiles": every known profile (behaviour of earlier
    # versions). Only config.toml / the settings dialog may change it, never a preset.
    match_scope: str = "participants"
    match_threshold: float = 0.55
    # the best profile must beat every other profile by this much (else: no automatic assignment)
    match_margin: float = 0.08
    # clean speech (s) a speaker needs before it is matched automatically / may change a profile
    min_match_speech_s: float = 8.0
    min_learn_speech_s: float = 20.0
    # the participant list limits the number of remote speakers (participants without you, plus
    # this many uninvited guests); per recording "Weitere Sprecher zulassen" lifts it
    limit_to_participants: bool = True
    extra_remote_speakers: int = 0
    invitee_bonus: float = 0.05  # only in match_scope = "all_profiles"
    auto_update_on_match: bool = True
    auto_update_threshold: float = 0.75


@dataclass
class SummaryConfig:
    enabled: bool = True
    provider: str = "ollama"  # ollama | openai (local OpenAI-compatible server) | anthropic (Claude API, cloud!)
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
    timeout_s: float = 900.0  # local providers (Ollama / OpenAI-compatible)
    # language the summary is written in, whatever language the call was in
    language: str = "de"
    # --- Claude via the Anthropic API (provider = "anthropic"): the transcript LEAVES this computer.
    # Only usable after the explicit confirmation in the settings dialog / `callrec anthropic enable`,
    # which sets the flag below; a preset can never set it or select this provider.
    anthropic_transcripts_leave_this_computer_confirmed: bool = False
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"  # low | medium | high | xhigh | max | "" (= not sent)
    anthropic_max_tokens: int = 32000
    anthropic_max_chunk_chars: int = 300000
    anthropic_max_retries: int = 4  # attempts per request (including the first)
    anthropic_connect_timeout_s: float = 15.0
    # maximum silence while waiting for the next part of the streamed answer
    anthropic_read_timeout_s: float = 180.0
    # no new attempt is started after this many seconds (waits are shortened to fit)
    anthropic_retry_budget_s: float = 300.0
    anthropic_refusal_fallback: bool = True


@dataclass
class HandoffConfig:
    mode: str = "folder"  # folder (OneDrive-synced SharePoint library) | graph (direct upload)
    folder: str = ""  # local folder synced by OneDrive, e.g. C:\Users\me\Firma\Team - Protokolle
    graph_drive_id: str = ""  # drive (document library) id, see README
    graph_folder: str = ""  # folder path inside the library, e.g. "Gesprächsprotokolle"
    graph_scope: str = "Sites.Selected"  # or Files.ReadWrite.All (broader)
    include_participant_emails: bool = False


@dataclass
class ConsentConfig:
    """How a recording start is authorised (never settable by a preset).

    ``ask`` (default): one question "aufnehmen?" with a reminder that all participants must have
    agreed; meta.json records that the user started the recording, not that consent was confirmed.
    ``confirm``: the explicit confirmation (checkbox / typed "ja"). In both modes a recording only
    ever starts through the user's own action for that recording."""

    mode: str = "ask"  # ask | confirm


@dataclass
class DetectionConfig:
    enabled: bool = True
    teams_processes: list[str] = field(default_factory=lambda: ["ms-teams.exe", "teams.exe"])
    poll_interval_s: float = 3.0
    calendar_reminder: bool = True
    cooldown_minutes: int = 10


@dataclass
class ProcessingConfig:
    """What happens after a recording ends (never settable by a preset).

    ``after_recording = "ask"``: one question "jetzt vollständig verarbeiten?" with the steps as
    checkboxes (pre-set from the ``default_*`` values below). ``"full"``: process right away
    without asking. The SharePoint hand-over of the summary only runs for a recording when the
    user ticked it in that question - or, in mode "full", when ``handoff_without_asking`` is
    explicitly true."""

    after_recording: str = "ask"  # ask | full
    default_speakers: bool = True  # speaker separation/recognition + assignment dialog
    default_summary: bool = True
    default_handoff: bool = True  # pre-ticks the hand-over box in the question (still the user's click)
    handoff_without_asking: bool = False  # only with after_recording = "full"


@dataclass
class AutoStopConfig:
    """Ending a recording when the call is over (starting always stays manual, behind the consent).

    Nothing is ever cut off silently: every automatic end first shows a countdown with
    "Weiter aufnehmen" / "Jetzt beenden"; without a reaction the recording is stopped through
    the normal stop path (same finalisation and processing as a manual stop)."""

    enabled: bool = True  # call-end detection (call app session + silence + calendar hint)
    # signal 1: the call app's (Teams) microphone session is no longer active
    # (process names: detection.teams_processes, matched incl. helper processes started by them)
    call_app_session: bool = True
    session_gone_s: float = 20.0  # gone this long AND the remote track quiet as long -> "call ended"
    session_gone_max_s: float = 120.0  # gone this long -> "call ended" even while something plays
    # signal 2: both tracks silent (also for calls without Teams: other apps, softphone)
    silence_minutes: float = 3.0
    silence_dbfs: float = -45.0  # a track is "silent" while its peak level stays below this
    # weak hint: once the calendar event of the recording has ended, a shorter silence suffices
    silence_after_event_end_s: float = 60.0
    countdown_s: int = 30  # notice "Aufnahme endet in N s" before stopping
    snooze_minutes: float = 15.0  # "Weiter aufnehmen": no further prompt for this long
    # safety nets - active even when enabled = false (0 = off)
    max_duration_minutes: float = 240.0
    max_duration_warning_minutes: float = 10.0
    max_duration_extend_minutes: float = 60.0  # "Weiter aufnehmen" at the limit extends by this
    silence_safety_minutes: float = 15.0  # only silence for this long -> prompt


@dataclass
class UpdateConfig:
    # Only config.toml may change the source (never a preset - [update] is not a preset section).
    repo_url: str = "https://github.com/jvt-cronbach/claude"
    branch: str = "claude/cool-maxwell-zmzy87"
    check_daily: bool = True  # look once a day and only NOTIFY; installing always needs your click


@dataclass
class HotkeyConfig:
    toggle: str = "ctrl+alt+shift+r"


UI_FRONTENDS = ("tk", "web")
UI_THEMES = ("system", "light", "dark")


@dataclass
class UiConfig:
    show_overlay: bool = True
    open_review_after_processing: bool = True
    start_minimized: bool = False  # True = start only in the tray, without opening the main window
    # "tk" = classic window; "web" = new interface (preview, pywebview/WebView2; falls back to "tk")
    frontend: str = "tk"
    theme: str = "system"  # new interface: system (Windows app mode) | light | dark


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
    consent: ConsentConfig = field(default_factory=ConsentConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    autostop: AutoStopConfig = field(default_factory=AutoStopConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    warnings: list[str] = field(default_factory=list, compare=False, repr=False)
    active_preset: str = field(default="", compare=False, repr=False)

    def recordings_path(self) -> Path:
        if self.general.recordings_dir.strip():
            return Path(self.general.recordings_dir).expanduser()
        return paths.default_recordings_dir()

    def validate(self) -> None:
        if self.ui.frontend not in UI_FRONTENDS:
            raise ConfigError("ui.frontend muss 'tk' oder 'web' sein")
        if self.ui.theme not in UI_THEMES:
            raise ConfigError("ui.theme muss 'system', 'light' oder 'dark' sein")
        if self.consent.mode not in ("ask", "confirm"):
            raise ConfigError("consent.mode muss 'ask' oder 'confirm' sein")
        if self.audio.format not in ("flac", "opus"):
            raise ConfigError(f"audio.format muss 'flac' oder 'opus' sein, nicht {self.audio.format!r}")
        if self.transcription.device not in ("auto", "cuda", "cpu"):
            raise ConfigError("transcription.device muss 'auto', 'cuda' oder 'cpu' sein")
        sp = self.speakers
        if not 0.0 < sp.match_threshold < 1.0:
            raise ConfigError("speakers.match_threshold muss zwischen 0 und 1 liegen")
        if sp.match_scope not in MATCH_SCOPES:
            raise ConfigError("speakers.match_scope muss 'participants' oder 'all_profiles' sein")
        if not 0.0 <= sp.match_margin < 0.5 or min(sp.min_match_speech_s, sp.min_learn_speech_s) < 0:
            raise ConfigError("speakers.match_margin muss zwischen 0 und 0,5 liegen, min_*_speech_s >= 0")
        if not sp.auto_update_threshold < 1.0:
            raise ConfigError("speakers.auto_update_threshold muss kleiner als 1 sein")
        if sp.auto_update_threshold < sp.match_threshold:
            # earlier versions accepted this (e.g. match_threshold raised to 0.72, auto_update left at 0.70):
            # a config.toml that worked before must not stop the app from starting after an update
            self.warnings.append(f"speakers.auto_update_threshold ({sp.auto_update_threshold}) lag unter "
                                 f"match_threshold ({sp.match_threshold}) und wird wie match_threshold behandelt.")
            sp.auto_update_threshold = sp.match_threshold
        from callrec.updater import UpdateError, validate_source

        try:
            validate_source(self.update.repo_url, self.update.branch)
        except UpdateError as exc:
            raise ConfigError(f"[update]: {exc}") from None
        if not 0 <= sp.extra_remote_speakers <= 20:
            raise ConfigError("speakers.extra_remote_speakers muss zwischen 0 und 20 liegen")
        if not 0 <= self.diarization.num_threads <= 64:
            raise ConfigError("diarization.num_threads muss zwischen 0 (automatisch) und 64 liegen")
        if self.audio.gap_threshold_s <= 0:
            raise ConfigError("audio.gap_threshold_s muss > 0 sein")
        if self.transcript.timestamp_format not in TIMESTAMP_FORMATS:
            raise ConfigError(f"transcript.timestamp_format muss einer von {', '.join(TIMESTAMP_FORMATS)} sein")
        if self.summary.provider not in SUMMARY_PROVIDERS:
            raise ConfigError("summary.provider muss 'ollama', 'openai' oder 'anthropic' sein")
        if self.summary.anthropic_effort not in ANTHROPIC_EFFORTS:
            raise ConfigError(f"summary.anthropic_effort muss einer von {', '.join(e or '(leer)' for e in ANTHROPIC_EFFORTS)} sein")
        if self.summary.anthropic_max_tokens < 1024:
            raise ConfigError("summary.anthropic_max_tokens muss >= 1024 sein")
        if self.summary.anthropic_max_chunk_chars < 10000:
            raise ConfigError("summary.anthropic_max_chunk_chars muss >= 10000 sein")
        if self.handoff.mode not in ("folder", "graph"):
            raise ConfigError("handoff.mode muss 'folder' oder 'graph' sein")
        if self.handoff.graph_scope not in GRAPH_FILE_SCOPES:
            raise ConfigError(f"handoff.graph_scope muss einer von {', '.join(GRAPH_FILE_SCOPES)} sein")
        if self.transcription.beam_size < 1:
            raise ConfigError("transcription.beam_size muss >= 1 sein")
        t = self.transcription
        if t.language != "auto" and not _LANG_RE.fullmatch(t.language):
            raise ConfigError(f"transcription.language muss 'auto' oder ein Sprachcode wie 'de' sein, nicht {t.language!r}")
        if not t.language_candidates or not all(_LANG_RE.fullmatch(c) for c in t.language_candidates):
            raise ConfigError("transcription.language_candidates muss eine Liste von Sprachcodes sein, z. B. [\"de\", \"en\"]")
        if not 0.5 <= t.language_switch_threshold < 1.0:
            raise ConfigError("transcription.language_switch_threshold muss zwischen 0,5 und 1 liegen")
        if not 1 <= t.language_detection_windows <= 30:
            raise ConfigError("transcription.language_detection_windows muss zwischen 1 und 30 liegen")
        if t.repetition_penalty <= 0 or t.no_repeat_ngram_size < 0 or t.repetition_warn_min_repeats < 0:
            raise ConfigError("transcription.repetition_penalty muss > 0, no_repeat_ngram_size/repetition_warn_min_repeats >= 0 sein")
        if not _LANG_RE.fullmatch(self.summary.language):
            raise ConfigError("summary.language muss ein Sprachcode wie 'de' sein")
        s = self.summary
        if min(s.anthropic_connect_timeout_s, s.anthropic_read_timeout_s, s.anthropic_retry_budget_s) <= 0:
            raise ConfigError("summary.anthropic_*_timeout_s / anthropic_retry_budget_s müssen > 0 sein")
        if self.processing.after_recording not in AFTER_RECORDING_MODES:
            raise ConfigError("processing.after_recording muss 'ask' oder 'full' sein")
        a = self.autostop
        if not 10 <= a.countdown_s <= 600:
            raise ConfigError("autostop.countdown_s muss zwischen 10 und 600 Sekunden liegen")
        if not -90.0 <= a.silence_dbfs <= -10.0:
            raise ConfigError("autostop.silence_dbfs muss zwischen -90 und -10 liegen")
        if a.session_gone_s < 3 or a.session_gone_max_s < a.session_gone_s:
            raise ConfigError("autostop.session_gone_s muss >= 3 und session_gone_max_s >= session_gone_s sein")
        if a.silence_minutes < 0.5 or a.silence_after_event_end_s < 10:
            raise ConfigError("autostop.silence_minutes muss >= 0,5 und silence_after_event_end_s >= 10 sein")
        if a.snooze_minutes < 1 or a.max_duration_extend_minutes < 1:
            raise ConfigError("autostop.snooze_minutes / max_duration_extend_minutes müssen >= 1 sein")
        if min(a.max_duration_minutes, a.max_duration_warning_minutes, a.silence_safety_minutes) < 0:
            raise ConfigError("autostop.max_duration_minutes / max_duration_warning_minutes / "
                              "silence_safety_minutes dürfen nicht negativ sein (0 = aus)")
        if 0 < a.max_duration_minutes < 5 or 0 < a.silence_safety_minutes < 1:
            raise ConfigError("autostop.max_duration_minutes muss 0 (aus) oder >= 5, silence_safety_minutes "
                              "0 (aus) oder >= 1 sein")


_LANG_RE = re.compile(r"[a-z]{2,3}")
TIMESTAMP_FORMATS = ("hh:mm:ss", "mm:ss", "none")
MATCH_SCOPES = ("participants", "all_profiles")
AFTER_RECORDING_MODES = ("ask", "full")
SUMMARY_PROVIDERS = ("ollama", "openai", "anthropic")
CLOUD_PROVIDERS = ("anthropic",)  # providers that send the transcript to a third party by design
ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max", "")
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
    "consent": ConsentConfig,
    "detection": DetectionConfig,
    "autostop": AutoStopConfig,
    "processing": ProcessingConfig,
    "hotkey": HotkeyConfig,
    "update": UpdateConfig,
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
# Keys that decide where conversation content is sent: only config.toml may set them,
# never a preset file (presets are meant to be swapped and shared).
PRESET_FORBIDDEN_KEYS = {
    "summary": (
        "allow_remote_endpoint_data_leaves_this_computer", "ollama_url", "openai_url", "openai_api_key",
        "anthropic_transcripts_leave_this_computer_confirmed",
    ),
    # who may be assigned automatically is a decision about this PC's data, not a tuning value
    "speakers": ("match_scope",),
}
# Values a preset may not select: a preset must never switch the summary to a cloud provider.
PRESET_FORBIDDEN_VALUES = {("summary", "provider"): CLOUD_PROVIDERS}


def config_from_dict(data: dict[str, Any], base: Config | None = None) -> Config:
    cfg = copy.deepcopy(base) if base is not None else Config()
    warnings: list[str] = list(cfg.warnings)
    for section, values in data.items():
        if "consent" in section.lower() and section != "consent":
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
            # [consent] knows exactly one setting (mode = ask | confirm); nothing can switch the
            # per-recording user action off.
            if "consent" in key.lower() or "einwillig" in key.lower() or (section == "consent" and key != "mode"):
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
    if is_legacy_language(allowed.get("transcription")):
        # presets of earlier versions stored the old default "de" - do not let it switch detection off
        allowed["transcription"] = {k: v for k, v in allowed["transcription"].items() if k != "language"}
        log.info("Preset %r: alter Standardwert language = \"de\" ignoriert (automatische Erkennung bleibt)", name)
    ignored = sorted(set(preset) - set(allowed))
    dropped: list[str] = []
    for section, keys in PRESET_FORBIDDEN_KEYS.items():
        values = allowed.get(section)
        if isinstance(values, dict):
            values = dict(values)
            for k in keys:
                if k in values:
                    del values[k]
                    dropped.append(f"{section}.{k}")
            allowed[section] = values
    for (section, k), bad in PRESET_FORBIDDEN_VALUES.items():
        values = allowed.get(section)
        if isinstance(values, dict) and values.get(k) in bad:
            values = dict(values)
            dropped.append(f"{section}.{k} = {values.pop(k)!r}")
            allowed[section] = values
    out = config_from_dict(allowed, base=cfg)
    for k in ignored:
        out.warnings.append(f"Preset {name!r}: Abschnitt [{k}] ist in Presets nicht erlaubt und wurde ignoriert")
    for k in dropped:
        out.warnings.append(f"Preset {name!r}: {k} kann nur in config.toml gesetzt werden und wurde ignoriert")
    out.active_preset = name
    return out


LEGACY_LANGUAGE_NOTE = (
    "Spracherkennung: Die Konfiguration enthielt language = \"de\" (Standardwert früherer Versionen). "
    "Die Sprache wird jetzt je Aufnahme automatisch erkannt (Deutsch oder Englisch, Deutsch bei Zweifel). "
    "Immer Deutsch erzwingen: Einstellungen > Transkription > Sprache \"de\"."
)


def is_legacy_language(section: Any) -> bool:
    """``language = "de"`` without ``language_candidates`` = written as a default by a version before 0.2.

    Those versions wrote the whole template (and the settings dialog wrote every value), so "de"
    there is not a deliberate choice. Once ``language_candidates`` is present the file was written
    by this version and ``language`` is respected as set.
    """
    return isinstance(section, dict) and section.get("language") == "de" and "language_candidates" not in section


_LEGACY_LANGUAGE_LINE = re.compile(r'^language\s*=\s*"de"\s*(#.*)?$')


def migrate_config_file(path: Path) -> bool:
    """Rewrite only the ``language`` line of [transcription] (comments and layout are kept)."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    section = ""
    for i, line in enumerate(lines):
        st = line.strip()
        if st.startswith("["):
            section = st.strip("[] ")
            continue
        if section == "transcription" and _LEGACY_LANGUAGE_LINE.match(st):
            lines[i] = (
                "# automatisch umgestellt: Sprache je Aufnahme erkennen (\"de\" = immer Deutsch)\n"
                'language = "auto"\nlanguage_candidates = ["de", "en"]\n'
            )
            new = "".join(lines)
            data = tomllib.loads(new)  # never write a file that does not parse
            if data.get("transcription", {}).get("language") != "auto":
                return False
            tmp = path.with_suffix(".tmp")
            tmp.write_text(new, encoding="utf-8")
            tmp.replace(path)
            return True
    return False


HANDOFF_DEFAULT_NOTE = (
    "Übergabe: Das Häkchen „an SharePoint übergeben“ in der Rückfrage nach einer Aufnahme ist jetzt "
    "vorbelegt (frühere Versionen: nicht vorbelegt). Übergeben wird weiterhin erst nach dem Klick auf "
    "„Vollständig verarbeiten“. Nicht vorbelegen: Einstellungen › Aufnahme › Vorauswahl."
)
_HANDOFF_FALSE_LINE = re.compile(r"^default_handoff\s*=\s*false\s*(#.*)?$")


def migrate_handoff_default_file(path: Path) -> bool:
    """One-time: ``default_handoff = false`` (template default before 0.3) -> true, and mark the file
    as written for 0.3 (``[ui] frontend``) so that a false set later is kept. Comments are kept."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    section, flipped, ui_at = "", False, -1
    for i, line in enumerate(lines):
        st = line.strip()
        if st.startswith("["):
            section = st.strip("[] ")
            if section == "ui":
                ui_at = i
            continue
        if section == "processing" and _HANDOFF_FALSE_LINE.match(st):
            lines[i] = ("# 0.3: Vorbelegung umgestellt (übergeben wird weiterhin nur nach dem Klick in der Rückfrage)\n"
                        "default_handoff = true\n")
            flipped = True
    if not flipped:
        return False
    marker = 'frontend = "tk"\n'
    if ui_at >= 0:
        lines.insert(ui_at + 1, marker)
    else:
        lines.append("\n[ui]\n" + marker)
    new = "".join(lines)
    data = tomllib.loads(new)  # never write a file that does not parse
    if data.get("processing", {}).get("default_handoff") is not True or "frontend" not in data.get("ui", {}):
        return False
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(path)
    return True


def load_config(path: Path | None = None, preset: str | None = None) -> Config:
    """Load config.toml and overlay the preset (argument wins over general.preset)."""
    path = path or paths.config_file()
    if not path.exists():
        write_default_config(path)
        cfg = Config()
    else:
        data = _read_toml(path)
        migrated = False
        tr = data.get("transcription")
        if isinstance(tr, dict) and is_legacy_language(tr):
            tr["language"] = "auto"
            tr["language_candidates"] = ["de", "en"]
            try:
                migrated = migrate_config_file(path)
            except (OSError, ValueError):
                log.warning("config.toml konnte nicht auf automatische Spracherkennung umgestellt werden", exc_info=True)
            log.info("transcription.language = \"de\" (alter Standardwert) -> \"auto\" mit Kandidaten de, en")
        di, sp = data.get("diarization"), data.get("speakers")
        if isinstance(di, dict) and di.get("num_threads") == 2 and not (isinstance(sp, dict) and "match_scope" in sp):
            # 2 was the template default of earlier versions (written into every config.toml), not a choice
            di["num_threads"] = 0
            log.info("diarization.num_threads = 2 (alter Standardwert) -> automatisch")
        ui, pr = data.get("ui"), data.get("processing")
        if isinstance(pr, dict) and pr.get("default_handoff") is False and not (isinstance(ui, dict) and "frontend" in ui):
            # false was the template default before 0.3 (written into every config.toml), not a choice;
            # since 0.3 the hand-over box in the question after a recording is ticked by default
            pr["default_handoff"] = True
            try:
                # persisted once (with the [ui] frontend marker), so that a false chosen later stays
                handoff_migrated = migrate_handoff_default_file(path)
            except (OSError, ValueError):
                handoff_migrated = False
                log.warning("config.toml: Vorbelegung der Übergabe nicht umgestellt", exc_info=True)
            log.info("processing.default_handoff = false (alter Standardwert) -> true")
        else:
            handoff_migrated = False
        cfg = config_from_dict(data)
        if migrated:
            cfg.warnings.append(LEGACY_LANGUAGE_NOTE)
        if handoff_migrated:
            cfg.warnings.append(HANDOFF_DEFAULT_NOTE)
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
    for section, keys in PRESET_FORBIDDEN_KEYS.items():
        for k in keys:
            data.get(section, {}).pop(k, None)
    for (section, k), bad in PRESET_FORBIDDEN_VALUES.items():
        if data.get(section, {}).get(k) in bad:
            data[section].pop(k)
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
# "auto" = large-v3 auf einer nutzbaren NVIDIA-GPU, sonst large-v3-turbo (int8) auf der CPU.
# Feste Wahl z.B. "large-v3", "large-v3-turbo", "medium".
model = "auto"
# "auto" = NVIDIA-GPU (CUDA), falls vorhanden und nutzbar (einmal geprüft), sonst direkt CPU
device = "auto"
compute_type = "auto"
# "auto" = Sprache je Aufnahme erkennen, nur unter language_candidates; der erste Eintrag gilt
# bei Zweifel (kurzer, leiser oder uneindeutiger Anfang). Ein Sprachcode ("de", "en" ...) erzwingt
# diese Sprache.
language = "auto"
language_candidates = ["de", "en"]
# so viele 30-s-Abschnitte Sprache je Spur, über die Aufnahme verteilt
language_detection_windows = 6
# Wahrscheinlichkeit, ab der eine andere Sprache als die erste gewählt wird
language_switch_threshold = 0.75
# weniger Sprache (Sekunden) -> erste Sprache der Liste
language_min_speech_s = 20.0
# Sprachwechsel innerhalb einer Spur (faster-whisper "multilingual", experimentell: erkennt je
# 30-s-Fenster unter allen Sprachen, nicht nur den Kandidaten)
multilingual = false
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
# Schutz vor Wiederholungsschleifen (Standardwerte von faster-whisper; 0 = aus): ein Abschnitt wird
# mit höherer "Temperatur" neu dekodiert, wenn sein Text zu gleichförmig (Kompressionsrate) oder zu
# unsicher (log-Wahrscheinlichkeit) ist.
compression_ratio_threshold = 2.4
log_prob_threshold = -1.0
no_speech_threshold = 0.6
temperature_fallback = true
# > 1 bestraft wiederholte Wörter; n-Gramm-Sperre (0 = aus). Vorsicht: kann echte Wiederholungen unterdrücken.
repetition_penalty = 1.0
no_repeat_ngram_size = 0
# Hinweis im Transkript, wenn sich eine Wortfolge so oft hintereinander wiederholt (0 = aus)
repetition_warn_min_repeats = 4

[diarization]
enabled = true
embedding_model = "wespeaker_en_voxceleb_resnet34_LM"
# kleiner = mehr Sprecher werden unterschieden (zu viele werden bei der Zuordnung zusammengeführt)
cluster_threshold = 0.4
# 0 = automatisch
num_speakers = 0
min_duration_on = 0.3
min_duration_off = 0.5
# 0 = automatisch nach Anzahl der physischen Prozessorkerne (ein Kern bleibt frei, weniger während einer Aufnahme)
num_threads = 0

[speakers]
# "participants": automatisch nur Personen dieses Termins (Einladung oder von Ihnen eingetragen);
# "all_profiles": alle bekannten Stimmprofile (Verhalten früherer Versionen). Nicht per Preset änderbar.
match_scope = "participants"
# Ab welcher Ähnlichkeit (Kosinus, 0..1) eine Stimme einem Profil zugeordnet wird
match_threshold = 0.55
# Abstand zum zweitbesten Profil, sonst keine automatische Zuordnung
match_margin = 0.08
# Mindest-Sprechzeit (s) für die automatische Zuordnung bzw. für das Lernen eines Profils
min_match_speech_s = 8.0
min_learn_speech_s = 20.0
# Anzahl der Sprecher der Gegenseite höchstens = Teilnehmende ohne Sie (+ so viele nicht eingeladene Gäste);
# je Aufnahme aufhebbar ("Weitere Sprecher zulassen")
limit_to_participants = true
extra_remote_speakers = 0
# Bonus für Eingeladene (nur bei match_scope = "all_profiles")
invitee_bonus = 0.05
auto_update_on_match = true
auto_update_threshold = 0.75

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
# Ihr Name für die eigenen Beiträge (Mikrofonspur); leer = Vorname aus dem Microsoft-Konto,
# sonst aus "Ihr Name", sonst "Ich"
self_display_name = ""

[summary]
enabled = true
# "ollama" oder "openai" (OpenAI-kompatibler lokaler Server, z.B. LM Studio, llama.cpp)
# oder "anthropic" (Claude über die Anthropic-API - das TRANSKRIPT VERLÄSST DANN DIESEN RECHNER;
# nur über Einstellungen > Zusammenfassung bzw. "callrec anthropic enable" mit Bestätigung)
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
# Sprache der Zusammenfassung - unabhängig von der Sprache des Gesprächs
language = "de"

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
# Prozessnamen der Anruf-App (Erinnerung beim Start und Erkennung des Gesprächsendes). Auch
# Hilfsprozesse, die von einem dieser Prozesse gestartet wurden (z.B. msedgewebview2.exe), zählen.
teams_processes = ["ms-teams.exe", "teams.exe"]
poll_interval_s = 3.0
calendar_reminder = true
cooldown_minutes = 10

[consent]
# ask = eine Frage "aufnehmen?" mit Hinweis auf die nötige Zustimmung aller Teilnehmenden (meta.json
# vermerkt dann: vom Nutzer gestartet, Hinweis angezeigt - nicht: Einwilligung bestätigt).
# confirm = ausdrückliche Bestätigung per Häkchen bzw. "ja". Eine Aufnahme startet nie von selbst.
mode = "ask"

[autostop]
# Aufnahme beenden, wenn das Gespräch vorbei ist - nie still: vorher erscheint ein Countdown
# mit "Weiter aufnehmen" / "Jetzt beenden". Der Start bleibt immer manuell (Einwilligung).
enabled = true
# Signal 1: Mikrofon-Sitzung der Anruf-App (Teams) nicht mehr aktiv
call_app_session = true
# so lange weg UND Gegenseite so lange still -> Gespräch beendet (überbrückt Bluetooth-Wechsel)
session_gone_s = 20.0
# so lange weg -> Gespräch beendet, auch wenn etwas abgespielt wird
session_gone_max_s = 120.0
# Signal 2: beide Spuren still (auch ohne Teams, z.B. andere App oder Softphone)
silence_minutes = 3.0
# "still" = Spitzenpegel unter diesem Wert (dBFS)
silence_dbfs = -45.0
# schwacher Hinweis: nach dem Ende des Kalendertermins genügt eine kürzere Stille (Sekunden)
silence_after_event_end_s = 60.0
# so viele Sekunden Countdown vor dem Beenden
countdown_s = 30
# "Weiter aufnehmen": so lange keine weitere Nachfrage (Minuten)
snooze_minutes = 15.0
# Sicherheitsnetz (gilt auch bei enabled = false; 0 = aus): Höchstdauer mit Vorwarnung ...
max_duration_minutes = 240.0
max_duration_warning_minutes = 10.0
max_duration_extend_minutes = 60.0
# ... und Nachfrage, wenn so lange nur Stille aufgenommen wurde
silence_safety_minutes = 15.0

[processing]
# Nach dem Ende einer Aufnahme: "ask" = einmal fragen ("jetzt vollständig verarbeiten?"),
# "full" = ohne Rückfrage vollständig verarbeiten
after_recording = "ask"
# Vorauswahl der Schritte in der Rückfrage
default_speakers = true
default_summary = true
# Häkchen "an SharePoint übergeben" vorbelegen (übergeben wird trotzdem nur nach dem Klick auf
# "Vollständig verarbeiten" in der Rückfrage)
default_handoff = true
# nur bei after_recording = "full": Zusammenfassung ohne Rückfrage an SharePoint übergeben
handoff_without_asking = false

[hotkey]
toggle = "ctrl+alt+shift+r"

[update]
# Quelle der Updates (nur HTTPS auf github.com; installiert wird nur der in release.json freigegebene Stand)
repo_url = "https://github.com/jvt-cronbach/claude"
branch = "claude/cool-maxwell-zmzy87"
# einmal täglich nachsehen und nur benachrichtigen (installiert wird nie ohne Klick)
check_daily = true

[ui]
show_overlay = true
open_review_after_processing = true
# true = beim Start nur das Tray-Symbol, ohne Hauptfenster
start_minimized = false
# Oberfläche: "tk" = klassisches Fenster, "web" = neue Oberfläche (Vorschau; ohne WebView2 automatisch "tk")
frontend = "tk"
# neue Oberfläche: "system" = wie der Windows-App-Modus, "light" = hell, "dark" = dunkel
theme = "system"
"""


def write_default_config(path: Path | None = None) -> Path:
    path = path or paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return path
