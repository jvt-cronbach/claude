"""Settings without a toolkit: what the web settings page shows, and how a change is checked and saved.

The schema lists every value the classic settings dialog offers (plus the new ``[ui]`` and
``[consent]`` choices) with a label and one plain sentence. Saving builds a new ``Config`` from
``config.toml`` (never the preset overlay), validates it, asks for the confirmations that the Tk
dialog asks for (cloud summary, hand-over without asking) and writes it. The API key never
travels through here: it is stored with DPAPI by its own bridge call and never sent to the page.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from callrec.asr.profiles import DESCRIPTIONS as PROFILE_DESCRIPTIONS
from callrec.asr.profiles import LABELS as PROFILE_LABELS
from callrec.asr.profiles import PROFILE_CHOICES
from callrec.config import (
    GRAPH_FILE_SCOPES,
    TIMESTAMP_FORMATS,
    Config,
    ConfigError,
    config_from_dict,
    config_to_dict,
)

WHISPER_MODELS = ["auto", "large-v3", "large-v3-turbo", "medium", "small", "base"]
# What each model costs on a CPU, relative to large-v3-turbo (int8): the encoder dominates there
# (32 layers in large-v3 and turbo alike; distil-large-v3/-v3.5 keep that encoder too and are
# trained for English only, so they are not offered). See README "Geschwindigkeit".
WHISPER_MODEL_LABELS = {
    "auto": "auto – large-v3 mit NVIDIA-GPU, sonst large-v3-turbo (empfohlen)",
    "large-v3": "large-v3 – genaueste Wahl für eine NVIDIA-GPU; auf der CPU etwa 2–3× langsamer als turbo",
    "large-v3-turbo": "large-v3-turbo – auf der CPU empfohlen, kaum ungenauer als large-v3",
    "medium": "medium – auf der CPU nicht schneller als turbo und ungenauer",
    "small": "small – auf der CPU nur etwa 1,5–2× schneller als turbo, aber deutlich mehr Fehler bei Deutsch",
    "base": "base – sehr schnell, für Gespräche zu ungenau",
}


@dataclass(frozen=True)
class Field:
    section: str
    key: str
    label: str
    help: str = ""
    kind: str = ""  # "" = from the type; select | radio | folder | list | text | number | bool
    choices: tuple[tuple[str, str], ...] = ()
    adv: bool = False  # under "Feineinstellung"

    @property
    def id(self) -> str:
        return f"{self.section}.{self.key}"


@dataclass(frozen=True)
class Page:
    key: str
    title: str
    intro: str = ""
    fields: tuple[Field, ...] = ()
    extras: tuple[str, ...] = field(default_factory=tuple)  # special panels the page renders


def _c(*pairs: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(pairs)


YES_NO = ()
PAGES: tuple[Page, ...] = (
    Page("general", "Allgemein", "", (
        Field("ui", "frontend", "Oberfläche", "Die neue Oberfläche ist eine Vorschau. Startet sie nicht (WebView2 fehlt), "
              "öffnet sich automatisch das klassische Fenster. Gilt ab dem nächsten Start.", "radio",
              _c(("web", "Neue Oberfläche (Vorschau)"), ("tk", "Klassisches Fenster"))),
        Field("ui", "theme", "Darstellung", "Teams' eigene Einstellung ist von außen nicht zuverlässig lesbar. „System“ "
              "folgt deshalb dem Windows-App-Modus (Einstellungen › Personalisierung › Farben) – meist dieselbe Wahl wie "
              "in Teams.", "select", _c(("system", "System (wie Windows)"), ("light", "Hell"), ("dark", "Dunkel"))),
        Field("general", "user_name", "Eigener Name", "So erscheinen die eigenen Beiträge. Leer = Name aus dem "
              "Microsoft-Konto."),
        Field("general", "recordings_dir", "Aufnahmeordner", "Leer = Standardordner. Kein OneDrive-Ordner: Aufnahmen "
              "würden sonst in die Cloud synchronisiert.", "folder"),
        Field("hotkey", "toggle", "Tastenkürzel Start/Stopp", "Gilt nach einem Neustart des Programms, z. B. "
              "ctrl+alt+shift+r."),
        Field("ui", "start_minimized", "Beim Start nur im Infobereich", "Das Fenster öffnet sich erst mit einem Klick "
              "auf das Symbol."),
        Field("ui", "show_overlay", "Aufnahme-Anzeige", "Kleines Fenster mit Laufzeit und „Beenden“, immer im "
              "Vordergrund – auch über Teams im Vollbild."),
        Field("ui", "open_review_after_processing", "Sprecher-Zuordnung nach der Verarbeitung öffnen",
              "Wenn Stimmen noch keinen Namen haben."),
        Field("update", "check_daily", "Einmal täglich nach Updates sehen", "Fragt nur den freigegebenen Stand bei "
              "github.com ab (keine Aufnahmen, Namen oder Einstellungen). Installiert wird nur nach einem Klick."),
        Field("general", "preset", "Aktives Preset", "Presets enthalten nur Transkriptions-, Sprecher-, Darstellungs- "
              "und Zusammenfassungs-Einstellungen.", "preset"),
    ), ("autostart",)),
    Page("recording", "Aufnahme", "Eine Aufnahme startet nie von selbst – immer erst nach einem Klick für genau diese "
         "Aufnahme.", (
        Field("consent", "mode", "Vor der Aufnahme", "Die rechtliche Verantwortung für die Zustimmung aller "
              "Teilnehmenden bleibt bei der Person, die aufnimmt. Nicht per Preset einstellbar.", "radio",
              _c(("ask", "Eine Frage vor der Aufnahme – „Aufnehmen“ mit Hinweis auf die nötige Zustimmung; vermerkt "
                         "wird: vom Nutzer gestartet, Hinweis angezeigt"),
                 ("confirm", "Ausdrückliche Bestätigung der Einwilligung – Häkchen vor dem Start; vermerkt wird: "
                             "Einwilligung bestätigt"))),
        Field("audio", "mic_device", "Mikrofon", "Automatisch = Windows-Standardgerät für Kommunikation.", "device_mic"),
        Field("audio", "loopback_device", "Gegenseite (Wiedergabegerät)", "Hier ist die Gegenseite zu hören – meist "
              "das Headset.", "device_out"),
        Field("processing", "after_recording", "Nach der Aufnahme", "", "radio",
              _c(("ask", "Fragen, ob vollständig verarbeitet wird (empfohlen)"),
                 ("full", "Sofort vollständig verarbeiten – ohne Rückfrage"))),
        Field("processing", "default_speakers", "Vorauswahl: Sprecher zuordnen"),
        Field("processing", "default_summary", "Vorauswahl: Zusammenfassung"),
        Field("processing", "default_handoff", "Vorauswahl: an SharePoint übergeben", "Häkchen in der Rückfrage "
              "vorbelegen. Übergeben wird trotzdem erst nach dem Klick auf „Vollständig verarbeiten“."),
        Field("processing", "handoff_without_asking", "Ohne Rückfrage übergeben", "Nur bei „Sofort vollständig "
              "verarbeiten“: nach jeder Aufnahme die Zusammenfassung (nur sie) ohne Vorschau übergeben. Beim "
              "Einschalten kommt noch eine Rückfrage."),
    )),
    Page("autostop", "Aufnahme-Ende", "Ist das Gespräch vorbei, erscheint „Call scheint beendet – Aufnahme endet in "
         "… s“ mit „Weiter aufnehmen“ und „Jetzt beenden“. Ohne Reaktion wird normal beendet; vom Ton wird nichts "
         "abgeschnitten.", (
        Field("autostop", "enabled", "Gesprächsende automatisch erkennen"),
        Field("autostop", "call_app_session", "Über die Mikrofon-Sitzung der Anruf-App (Teams)"),
        Field("autostop", "countdown_s", "Countdown vor dem Beenden (s)"),
        Field("autostop", "snooze_minutes", "„Weiter aufnehmen“: keine Nachfrage für (min)"),
        Field("autostop", "max_duration_minutes", "Sicherheitsnetz – Höchstdauer (min, 0 = aus)"),
        Field("detection", "teams_processes", "Programmnamen der Anruf-App", "Mehrere durch Komma getrennt.", "list"),
        Field("detection", "enabled", "Laufenden Call erkennen und nachfragen", "Fragt nur – startet nie selbst."),
        Field("detection", "calendar_reminder", "Bei Terminbeginn nachfragen"),
        Field("autostop", "session_gone_s", "Anruf-App weg und Gegenseite still seit (s)", adv=True),
        Field("autostop", "session_gone_max_s", "Anruf-App weg seit (s), auch wenn etwas abgespielt wird", adv=True),
        Field("autostop", "silence_minutes", "Beide Spuren still seit (min)", adv=True),
        Field("autostop", "silence_after_event_end_s", "Nach Terminende genügt Stille von (s)", adv=True),
        Field("autostop", "silence_dbfs", "Stille = Pegel unter (dBFS)", "Rauscht das Mikrofon, etwas höher setzen "
              "(z. B. −40).", adv=True),
        Field("autostop", "max_duration_warning_minutes", "Höchstdauer: Vorwarnung (min vorher)", adv=True),
        Field("autostop", "max_duration_extend_minutes", "Höchstdauer: „Weiter aufnehmen“ verlängert um (min)", adv=True),
        Field("autostop", "silence_safety_minutes", "Sicherheitsnetz – Nachfrage nach nur Stille (min, 0 = aus)", adv=True),
    )),
    Page("transcription", "Transkription", "Geänderte Werte gelten für neue Aufnahmen; ältere lassen sich über „Neu "
         "transkribieren“ verbessern – die alte Fassung bleibt als Version erhalten.", (
        Field("transcription", "profile", "Tempo und Genauigkeit", "Gilt für neue Aufnahmen; „Neu transkribieren“ "
              "fragt je Aufnahme. Den größten Gewinn bringt ohnehin die Nutzung aller Kerne – sie gilt für jedes Profil.",
              "radio", tuple((k, f"{PROFILE_LABELS[k]} – {PROFILE_DESCRIPTIONS[k]}") for k in PROFILE_CHOICES)),
        Field("transcription", "model", "Whisper-Modell", "Auf einem PC ohne NVIDIA-GPU ist large-v3-turbo die beste "
              "Wahl; kleinere Modelle sind schneller, machen aber spürbar mehr Fehler.", "select",
              tuple((m, WHISPER_MODEL_LABELS[m]) for m in WHISPER_MODELS)),
        Field("transcription", "language", "Sprache", "auto = je Aufnahme erkennen; de/en = erzwingen.", "select",
              _c(("auto", "Automatisch erkennen"), ("de", "Deutsch"), ("en", "Englisch"))),
        Field("transcription", "device", "Rechenwerk", "", "select", _c(("auto", "Automatisch"), ("cuda", "NVIDIA-GPU"),
                                                                         ("cpu", "Prozessor (CPU)"))),
        Field("transcription", "use_participant_names_as_prompt", "Namen der Teilnehmenden als Hinweis nutzen"),
        Field("transcription", "use_glossary", "Glossar als Hinweis nutzen"),
        Field("transcription", "initial_prompt", "Zusätzlicher Kontext", "Ein Satz zum Thema hilft bei Fachbegriffen."),
        Field("processing", "during_call", "Während eines Anrufs oder einer Aufnahme", "Die Verarbeitung läuft "
              "immer mit niedriger Priorität.", "radio",
              _c(("reduce", "Langsamer weiterrechnen – etwa die Hälfte der Kerne bleibt für das Gespräch frei"),
                 ("pause", "Anhalten – weiter, sobald das Gespräch vorbei ist"))),
        Field("processing", "parallel_diarization", "Sprechertrennung gleichzeitig mit der Transkription", "„Automatisch“: "
              "nach der Messung („Geschwindigkeit messen“), ohne Messung ab 6 Prozessorkernen.", "select",
              _c(("auto", "Automatisch"), ("on", "Immer gleichzeitig"), ("off", "Immer danach"))),
        Field("transcription", "cpu_threads", "Rechen-Threads für Whisper insgesamt (0 = automatisch)", "Automatisch: alle "
              "physischen Kerne, während eines Gesprächs etwa die Hälfte.", adv=True),
        Field("transcription", "parallel_lanes", "Parallele Spuren (0 = automatisch)", "Beide Spuren werden in Teilen "
              "gleichzeitig transkribiert; automatisch 2 ab 4 Kernen bzw. der gemessene beste Wert.", adv=True),
        Field("transcription", "beam_size", "Beam-Größe (nur bei „Eigene Einstellungen“)", adv=True),
        Field("transcription", "vad_filter", "Sprachaktivitätserkennung (VAD)", adv=True),
        Field("transcription", "vad_threshold", "VAD-Schwelle (0..1, nur bei „Eigene Einstellungen“)", adv=True),
        Field("transcription", "vad_min_silence_ms", "VAD-Mindestpause (ms, nur bei „Eigene Einstellungen“)", adv=True),
        Field("transcription", "vad_speech_pad_ms", "VAD-Polster (ms, nur bei „Eigene Einstellungen“)", adv=True),
        Field("transcription", "hallucination_silence_threshold", "Halluzinationsschutz in Pausen (s, 0 = aus)", adv=True),
        Field("transcription", "condition_on_previous_text", "Vorherigen Text als Kontext", adv=True),
    ), ("speed", "glossary", "models")),
    Page("language", "Sprache & Qualität", "Bei Sprache „auto“ wird je Aufnahme an mehreren Stellen beider Spuren "
         "gemessen; die erste Sprache der Liste gilt, solange keine andere eindeutig erkannt wird.", (
        Field("transcription", "language_candidates", "Mögliche Sprachen", "Kommas; die erste gilt bei Zweifel.", "list"),
        Field("summary", "language", "Sprache der Zusammenfassung", "", "select", _c(("de", "Deutsch"), ("en", "Englisch"))),
        Field("transcription", "language_switch_threshold", "Andere Sprache erst ab Wahrscheinlichkeit (0,5–1)", adv=True),
        Field("transcription", "language_min_speech_s", "Mindestens so viel Sprache für einen Wechsel (s)", adv=True),
        Field("transcription", "language_detection_windows", "Messabschnitte je Spur (je 30 s, nur bei „Eigene "
              "Einstellungen“)", adv=True),
        Field("transcription", "multilingual", "Sprachwechsel innerhalb einer Spur (experimentell)", adv=True),
        Field("transcription", "compression_ratio_threshold", "Schleifenschutz: max. Kompressionsrate (0 = aus)", adv=True),
        Field("transcription", "log_prob_threshold", "Schleifenschutz: min. log-Wahrscheinlichkeit (0 = aus)", adv=True),
        Field("transcription", "no_speech_threshold", "Stille-Schwelle no_speech (0 = aus)", adv=True),
        Field("transcription", "temperature_fallback", "Neu dekodieren mit höherer Temperatur (nur bei „Eigene "
              "Einstellungen“)", adv=True),
        Field("transcription", "repetition_penalty", "Wiederholungs-Strafe (1 = aus)", adv=True),
        Field("transcription", "no_repeat_ngram_size", "Wiederholte n-Gramme sperren (0 = aus)", adv=True),
        Field("transcription", "repetition_warn_min_repeats", "Warnung ab Wiederholungen in Folge (0 = aus)", adv=True),
    )),
    Page("speakers", "Sprecher", "", (
        Field("speakers", "match_scope", "Automatisch zuordnen", "Nicht per Preset einstellbar.", "radio",
              _c(("participants", "Nur Teilnehmende des Termins (empfohlen)"),
                 ("all_profiles", "Alle gespeicherten Stimmprofile – auch nicht Eingeladene"))),
        Field("speakers", "auto_update_on_match", "Profile bei sicherer Erkennung nachschärfen"),
        Field("speakers", "min_named_speech_s", "Kurze Fragmente ausblenden unter (Sekunden Sprache)",
              "Stimmen mit weniger Sprache (Husten, „mhm“, Störgeräusche) werden nicht zur Zuordnung angeboten; "
              "ihr Text geht an die passende Person oder erscheint als „Unklar“. Im Zuordnungsdialog über "
              "„Kurze Fragmente anzeigen“ trotzdem benennbar."),
        Field("diarization", "cluster_threshold", "Clusterschwelle (kleiner = mehr Sprecher)", adv=True),
        Field("diarization", "num_speakers", "Feste Sprecheranzahl Gegenseite (0 = auto)", adv=True),
        Field("diarization", "min_duration_on", "Mindestdauer Sprechabschnitt (s)", adv=True),
        Field("diarization", "min_duration_off", "Mindestpause zwischen Abschnitten (s)", adv=True),
        Field("diarization", "num_threads", "Rechen-Threads Sprechertrennung (0 = automatisch)", adv=True),
        Field("speakers", "match_threshold", "Schwelle Wiedererkennung (0..1)", adv=True),
        Field("speakers", "match_margin", "Mindestabstand zum zweitbesten Profil", adv=True),
        Field("speakers", "min_match_speech_s", "Mindest-Sprechzeit für automatische Erkennung (s)", adv=True),
        Field("speakers", "min_learn_speech_s", "Mindest-Sprechzeit, damit ein Profil lernt (s)", adv=True),
        Field("speakers", "invitee_bonus", "Bonus für Eingeladene (nur bei „alle Profile“)", adv=True),
        Field("speakers", "auto_update_threshold", "Nachschärfen ab Ähnlichkeit", adv=True),
    ), ("profiles_link",)),
    Page("rendering", "Transkript", "Geänderte Einstellungen erzeugen beim erneuten Verarbeiten eine neue Version; "
         "frühere Versionen bleiben zum Vergleich erhalten.", (
        Field("transcript", "timestamp_format", "Zeitstempel", "", "select", tuple((f, f) for f in TIMESTAMP_FORMATS)),
        Field("transcript", "self_display_name", "Eigener Name im Transkript", "Leer = Vorname aus dem Microsoft-Konto, "
              "sonst „Eigener Name“, sonst „Ich“. Bestehende Aufnahmen: „Transkript neu darstellen“."),
        Field("transcript", "merge_same_speaker", "Beiträge derselben Person zusammenfassen"),
        Field("transcript", "apply_replacements", "Korrekturliste anwenden"),
        Field("transcript", "merge_max_gap_s", "Zusammenfassen bis zu einer Pause von (s)", adv=True),
        Field("transcript", "split_pause_s", "Neuer Absatz nach Pause (s)", adv=True),
        Field("transcript", "max_paragraph_s", "Maximale Absatzlänge (s)", adv=True),
    )),
    Page("summary", "Zusammenfassung", "", (
        Field("summary", "enabled", "Zusammenfassung erstellen"),
        Field("summary", "provider", "Anbieter", "Claude überträgt das Transkript an Anthropic – das muss beim "
              "Speichern ausdrücklich bestätigt werden. Nie per Preset.", "radio",
              _c(("ollama", "Lokal mit Ollama – bleibt auf diesem Rechner"),
                 ("openai", "Lokaler OpenAI-kompatibler Server (z. B. LM Studio)"),
                 ("anthropic", "Claude (Anthropic-API) – Transkript verlässt den Rechner"))),
        Field("summary", "model", "Modell (lokal)", "", "model"),
        Field("summary", "anthropic_model", "Claude-Modell", "", "anthropic_model"),
        Field("summary", "anthropic_effort", "Denktiefe (effort)", "", "select",
              _c(("low", "niedrig"), ("medium", "mittel"), ("high", "hoch"), ("xhigh", "sehr hoch"), ("max", "maximal"),
                 ("", "Standard des Modells"))),
        Field("summary", "ollama_url", "Ollama-Adresse", adv=True),
        Field("summary", "openai_url", "OpenAI-kompatible Adresse (…/v1)", adv=True),
        Field("summary", "temperature", "Temperatur", adv=True),
        Field("summary", "num_ctx", "Kontextlänge (num_ctx, nur Ollama)", adv=True),
    ), ("llm_models", "apikey", "prompts")),
    Page("microsoft", "Microsoft-Konto", "Optional: für den Outlook-Kalender (Titel und Teilnehmende automatisch) und den "
         "direkten SharePoint-Upload. Dafür ist eine Entra-App-Registrierung nötig (README Abschnitt 5). Ohne sie "
         "funktioniert alles andere.", (
        Field("calendar", "client_id", "Anwendungs-ID (Client)"),
        Field("calendar", "tenant_id", "Verzeichnis-ID (Mandant)"),
    ), ("login",)),
    Page("sharepoint", "SharePoint", "Übergeben wird nur die Zusammenfassung – nie Transkript, Audio oder Stimmprofile – "
         "und nur nach einem Klick (oder dem Häkchen in der Rückfrage nach der Aufnahme).", (
        Field("handoff", "mode", "Verfahren", "", "radio",
              _c(("folder", "OneDrive-synchronisierter Ordner"), ("graph", "Direkter Upload über Microsoft Graph"))),
        Field("handoff", "folder", "Übergabeordner", "Mit OneDrive synchronisierte SharePoint-Bibliothek.", "folder"),
        Field("handoff", "include_participant_emails", "E-Mail-Adressen der Teilnehmenden mit übergeben"),
        Field("handoff", "graph_drive_id", "Graph: Laufwerks-ID der Bibliothek", adv=True),
        Field("handoff", "graph_folder", "Graph: Ordner in der Bibliothek", adv=True),
        Field("handoff", "graph_scope", "Graph: Berechtigung", "", "select", tuple((s, s) for s in GRAPH_FILE_SCOPES),
              adv=True),
    )),
    Page("retention", "Löschen", "Alte Aufnahmen werden automatisch gelöscht – das Alter zählt ab Beginn der Aufnahme. "
         "Aufnahmen mit „Behalten“ bleiben immer. Bereits an SharePoint übergebene Zusammenfassungen bleiben "
         "dort erhalten (CallRecorder löscht in SharePoint nichts). Nicht per Preset einstellbar.", (
        Field("retention", "enabled", "Alte Aufnahmen automatisch löschen"),
        Field("retention", "max_age_days", "Löschen nach (Tagen)", "Vorher erscheinen sie in der Übersicht als "
              "„wird in … Tagen gelöscht“, mit „Behalten“."),
        Field("retention", "scope", "Was gelöscht wird", "", "radio",
              _c(("audio_transcript", "Audio und Transkript – die Zusammenfassung bleibt (empfohlen)"),
                 ("recording", "Die ganze Aufnahme, auch die Zusammenfassung"),
                 ("audio", "Nur das Audio – Transkript und Zusammenfassung bleiben"))),
        Field("retention", "delete_mode", "Wie gelöscht wird", "Endgültig heißt: Die Dateien werden gelöscht und "
              "lassen sich nicht wiederherstellen. Das ist gewöhnliches Löschen, kein sicheres Überschreiben (auf SSDs, "
              "in Sicherungen oder synchronisierten Ordnern können Reste bleiben; der Aufnahmeordner liegt "
              "standardmäßig nicht in OneDrive).", "radio",
              _c(("permanent", "Endgültig (nicht rückgängig zu machen)"), ("recycle_bin", "In den Windows-Papierkorb"))),
        Field("retention", "max_share_per_run", "Sicherheit: nachfragen, wenn mehr als dieser Anteil auf einmal "
              "gelöscht würde (0..1)", "Schützt bei falsch gestellter Systemuhr.", adv=True),
    ), ("retention_log",)),
)

PAGE_BY_TAB = {  # tab names used by setupcheck / hints ("settings:<Tab>") -> page key
    "Allgemein": "general", "Aufnahme-Ende": "autostop", "Transkription": "transcription",
    "Zusammenfassung": "summary", "Microsoft-Konto": "microsoft", "SharePoint": "sharepoint",
    "Sprecher": "speakers", "Darstellung": "rendering", "Sprache & Qualität": "language", "Aufnahme": "recording",
    "Löschen": "retention",
}


def all_fields() -> list[Field]:
    return [f for p in PAGES for f in p.fields]


def _value(cfg: Config, f: Field) -> Any:
    v = getattr(getattr(cfg, f.section), f.key)
    return list(v) if isinstance(v, list) else v


def _kind(f: Field, default: Any) -> str:
    if f.kind:
        return f.kind
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, (int, float)):
        return "number"
    if isinstance(default, list):
        return "list"
    return "text"


def schema(cfg: Config) -> list[dict[str, Any]]:
    """Pages with fields and current values (JSON for the page)."""
    defaults = Config()
    out = []
    for p in PAGES:
        fields = []
        for f in p.fields:
            default = getattr(getattr(defaults, f.section), f.key)
            fields.append({"id": f.id, "label": f.label, "help": f.help, "kind": _kind(f, default),
                           "choices": [list(c) for c in f.choices], "adv": f.adv, "value": _value(cfg, f),
                           "default": list(default) if isinstance(default, list) else default})
        out.append({"key": p.key, "title": p.title, "intro": p.intro, "fields": fields, "extras": list(p.extras)})
    return out


def apply_patch(base: Config, patch: dict[str, Any]) -> Config:
    """New, validated Config: ``base`` with the changed values (``"section.key": value``) of known fields."""
    known = {f.id: f for f in all_fields()}
    data = config_to_dict(base)
    defaults = Config()
    for fid, raw in patch.items():
        f = known.get(fid)
        if f is None:
            raise ConfigError(f"Unbekannte Einstellung {fid}")
        default = getattr(getattr(defaults, f.section), f.key)
        val: Any
        if isinstance(default, bool):
            if not isinstance(raw, bool):
                raise ConfigError(f"{f.label}: ja oder nein erwartet")
            val = raw
        elif isinstance(default, list):
            items = raw if isinstance(raw, list) else str(raw).replace(";", ",").split(",")
            val = [str(x).strip() for x in items if str(x).strip()]
        elif isinstance(default, int):
            try:
                val = int(str(raw).strip())
            except ValueError:
                raise ConfigError(f"{f.label}: ganze Zahl erwartet") from None
        elif isinstance(default, float):
            try:
                val = float(str(raw).replace(",", ".").replace("−", "-").strip())
            except ValueError:
                raise ConfigError(f"{f.label}: Zahl erwartet") from None
        else:
            val = str(raw).strip()
            if f.choices and val not in {c[0] for c in f.choices}:
                raise ConfigError(f"{f.label}: ungültige Auswahl")
        data[f.section][f.key] = val
    cfg = config_from_dict(data)
    from callrec.hotkey import parse_hotkey

    parse_hotkey(cfg.hotkey.toggle)  # HotkeyError for an invalid shortcut
    return cfg


# -- confirmations (the same questions the classic settings dialog asks) ----------------------
CONFIRM_CLOUD, CONFIRM_AUTO_HANDOFF = "cloud", "auto_handoff"


def needs_cloud_confirmation(old: Config, new: Config) -> bool:
    """Selecting Claude needs an explicit confirmation (once); switching away revokes it."""
    return new.summary.provider == "anthropic" and not new.summary.anthropic_transcripts_leave_this_computer_confirmed


def needs_auto_handoff_confirmation(old: Config, new: Config) -> bool:
    p, o = new.processing, old.processing
    on = p.after_recording == "full" and p.handoff_without_asking
    was = o.after_recording == "full" and o.handoff_without_asking
    return on and not was


def confirmation_texts() -> dict[str, dict[str, str]]:
    from callrec.llm_anthropic import ANTHROPIC_SETTINGS_WARNING

    return {
        CONFIRM_CLOUD: {"title": "Transkripte an Anthropic senden?", "message": ANTHROPIC_SETTINGS_WARNING,
                        "confirm": "Claude verwenden", "cancel": "Abbrechen"},
        CONFIRM_AUTO_HANDOFF: {"title": "Ohne Rückfrage übergeben?",
                               "message": "Nach jeder Aufnahme wird dann ohne Rückfrage und ohne Vorschau die "
                                          "Zusammenfassung (nur sie) an den SharePoint-Ordner übergeben. Das gilt für "
                                          "alle künftigen Aufnahmen.",
                               "confirm": "Einschalten", "cancel": "Abbrechen"},
    }


def finalize(old: Config, new: Config, confirmed: list[str]) -> tuple[Config | None, str]:
    """Apply the confirmation rules. Returns (config to save, "") or (None, kind still to confirm)."""
    cfg = copy.deepcopy(new)
    if cfg.summary.provider != "anthropic":
        cfg.summary.anthropic_transcripts_leave_this_computer_confirmed = False
    elif needs_cloud_confirmation(old, cfg):
        if CONFIRM_CLOUD not in confirmed:
            return None, CONFIRM_CLOUD
        cfg.summary.anthropic_transcripts_leave_this_computer_confirmed = True
    if needs_auto_handoff_confirmation(old, cfg) and CONFIRM_AUTO_HANDOFF not in confirmed:
        return None, CONFIRM_AUTO_HANDOFF
    return cfg, ""


def save(old: Config, patch: dict[str, Any], confirmed: list[str], write: Callable[[Config], None]) -> dict[str, Any]:
    """Validate, confirm, write. Result for the page: ``{"ok": True}``, ``{"confirm": {...}}`` or ``{"error": ...}``."""
    from callrec.hotkey import HotkeyError

    try:
        new = apply_patch(old, patch)
    except (ConfigError, HotkeyError) as exc:
        return {"error": str(exc)}
    cfg, need = finalize(old, new, confirmed)
    if cfg is None:
        return {"confirm": {"kind": need, **confirmation_texts()[need]}}
    write(cfg)
    return {"ok": True}
