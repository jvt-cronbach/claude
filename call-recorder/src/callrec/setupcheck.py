"""First-run / setup checklist of the main window: what is configured, what is missing.

Every check is a plain function over the configuration with injectable probes, so the
logic is testable without Windows, audio devices, Graph or a network. Checks that need
the network (summary provider test) only run on an explicit click.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from callrec import paths
from callrec.config import Config

log = logging.getLogger(__name__)

OK, WARN, ERROR, OPTIONAL, UNKNOWN = "ok", "warn", "error", "optional", "unknown"
STATE_LABEL = {OK: "✓ in Ordnung", WARN: "! prüfen", ERROR: "✗ fehlt", OPTIONAL: "○ optional, nicht eingerichtet",
               UNKNOWN: "? unbekannt"}

# action keys understood by the main window
A_SETTINGS_GENERAL = "settings:Allgemein"
A_SETTINGS_TRANSCRIPTION = "settings:Transkription"
A_SETTINGS_SUMMARY = "settings:Zusammenfassung"
A_SETTINGS_MICROSOFT = "settings:Microsoft-Konto"
A_SETTINGS_SHAREPOINT = "settings:SharePoint"
A_TEST_SUMMARY = "test_summary"
A_DOWNLOAD_WHISPER = "download_whisper"
A_LOGIN = "login"
A_README = "readme"
A_RECHECK = "recheck"


@dataclass
class CheckItem:
    key: str
    title: str
    state: str
    detail: str
    required: bool = True
    actions: list[tuple[str, str]] = field(default_factory=list)  # (button text, action key)


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------
def check_audio(cfg: Config, resolve: Callable[[str, str], tuple[Any, Any]] | None) -> CheckItem:
    """``resolve(mic_pref, loopback_pref)`` -> (mic, loopback) device specs, or None off Windows."""
    title = "Audiogeräte (Mikrofon und Gegenseite)"
    fix = [("Geräte einstellen …", A_SETTINGS_GENERAL)]
    if resolve is None:
        return CheckItem("audio", title, UNKNOWN, "Nur unter Windows prüfbar.", actions=fix)
    try:
        mic, loop = resolve(cfg.audio.mic_device, cfg.audio.loopback_device)
    except Exception as exc:
        return CheckItem("audio", title, ERROR, f"Keine passenden Audiogeräte gefunden: {exc}. Headset anschließen, "
                         "dann „Erneut prüfen“ – oder das Gerät in den Einstellungen fest wählen.", actions=fix)
    return CheckItem("audio", title, OK, f"Mikrofon: {mic.name}\nGegenseite (Ausgabegerät): {loop.name}", actions=fix)


def check_whisper(cfg: Config, selected: Callable[[], tuple[str, str]], find: Callable[[str], Any]) -> CheckItem:
    """``selected()`` -> (model name, note); ``find(model)`` -> installed model with ``.complete`` or None."""
    title = "Whisper-Modell für die Transkription"
    try:
        model, note = selected()
    except Exception as exc:
        return CheckItem("whisper", title, UNKNOWN, f"Modellwahl nicht ermittelbar: {exc}")
    found = find(model)
    extra = f"\n{note}" if note else ""
    if found is None:
        return CheckItem("whisper", title, WARN,
                         f"Modell {model} ist noch nicht heruntergeladen (ca. 1,5–3 GB). Es wird bei der ersten "
                         f"Transkription automatisch geladen – oder jetzt vorab.{extra}",
                         actions=[("Jetzt herunterladen", A_DOWNLOAD_WHISPER), ("Modell wählen …", A_SETTINGS_TRANSCRIPTION)])
    if not getattr(found, "complete", True):
        return CheckItem("whisper", title, ERROR,
                         f"Modell {model} ist unvollständig (abgebrochener Download). „Jetzt herunterladen“ setzt fort.{extra}",
                         actions=[("Jetzt herunterladen", A_DOWNLOAD_WHISPER)])
    return CheckItem("whisper", title, OK, f"Modell {model} ist vorhanden.{extra}",
                     actions=[("Modell wählen …", A_SETTINGS_TRANSCRIPTION)])


def check_summary(cfg: Config, key_present: Callable[[], bool], tested: str | None = None,
                  tested_ok: bool | None = None) -> CheckItem:
    """Static check of the summary provider; ``tested``/``tested_ok`` = result of the last connection test."""
    s = cfg.summary
    title = "Zusammenfassung (Sprachmodell)"
    actions = [("Verbindung testen", A_TEST_SUMMARY), ("Einstellungen …", A_SETTINGS_SUMMARY)]
    if not s.enabled:
        return CheckItem("summary", title, WARN, "Die Zusammenfassung ist ausgeschaltet – es entstehen nur Transkripte.",
                         actions=actions[1:])
    if s.provider == "anthropic":
        what = f"Claude über die Anthropic-API (Modell {s.anthropic_model}); das Transkript verlässt dafür den Rechner."
        if not s.anthropic_transcripts_leave_this_computer_confirmed:
            return CheckItem("summary", title, ERROR, what + "\nDie Übertragung an Anthropic ist noch nicht bestätigt "
                             "(Einstellungen → Zusammenfassung → Speichern).", actions=actions[1:])
        if not key_present():
            return CheckItem("summary", title, ERROR, what + "\nKein API-Schlüssel hinterlegt.", actions=actions[1:])
    elif s.provider == "ollama":
        what = f"Lokal mit Ollama (Modell {s.model})."
    else:
        what = f"Lokaler OpenAI-kompatibler Server {s.openai_url} (Modell {s.model})."
    if tested is not None:
        return CheckItem("summary", title, OK if tested_ok else ERROR, f"{what}\nVerbindungstest: {tested}", actions=actions)
    return CheckItem("summary", title, UNKNOWN, what + "\nNoch nicht getestet – „Verbindung testen“ klicken.", actions=actions)


def check_microsoft(cfg: Config, signed_in_as: Callable[[], str | None]) -> CheckItem:
    title = "Microsoft-Anmeldung (Outlook-Kalender)"
    if not cfg.calendar.client_id.strip():
        return CheckItem("microsoft", title, OPTIONAL,
                         "Noch nicht eingerichtet (Entra-App-Registrierung, README Abschnitt 5). Die App funktioniert "
                         "auch ohne: Titel und Teilnehmende tippen Sie dann beim Start selbst ein.",
                         required=False, actions=[("Client-ID eintragen …", A_SETTINGS_MICROSOFT), ("Anleitung", A_README)])
    try:
        who = signed_in_as()
    except Exception as exc:
        return CheckItem("microsoft", title, WARN, f"Anmeldestatus nicht lesbar: {exc}", required=False,
                         actions=[("Anmelden …", A_LOGIN), ("Einstellungen …", A_SETTINGS_MICROSOFT)])
    if not who:
        return CheckItem("microsoft", title, WARN, "Client-ID ist eingetragen, aber Sie sind nicht angemeldet.",
                         required=False, actions=[("Anmelden …", A_LOGIN), ("Einstellungen …", A_SETTINGS_MICROSOFT)])
    return CheckItem("microsoft", title, OK, f"Angemeldet als {who}.", required=False,
                     actions=[("Neu anmelden …", A_LOGIN)])


def check_sharepoint(cfg: Config, is_dir: Callable[[Path], bool] = Path.is_dir,
                     synced: Callable[[Path], Any] = paths.cloud_sync_root_for) -> CheckItem:
    title = "SharePoint-Übergabe der Zusammenfassungen"
    h = cfg.handoff
    fix = [("Einstellungen …", A_SETTINGS_SHAREPOINT), ("Anleitung", A_README)]
    if h.mode == "graph":
        if not h.graph_drive_id.strip():
            return CheckItem("sharepoint", title, ERROR, "Verfahren „graph“ gewählt, aber keine Laufwerks-ID eingetragen.",
                             required=False, actions=fix)
        if not cfg.calendar.client_id.strip():
            return CheckItem("sharepoint", title, ERROR, "Der Graph-Upload braucht die Microsoft-Anmeldung (Client-ID fehlt).",
                             required=False, actions=fix)
        return CheckItem("sharepoint", title, OK,
                         f"Direkter Upload in die Bibliothek {h.graph_drive_id[:12]}…/{h.graph_folder or '/'}. "
                         "Beim ersten Upload fragt Microsoft einmalig nach der Zustimmung.", required=False, actions=fix)
    if not h.folder.strip():
        return CheckItem("sharepoint", title, OPTIONAL,
                         "Noch kein Übergabeordner eingestellt. Ohne ihn funktioniert alles außer der Übergabe an "
                         "SharePoint (README Abschnitt 10: Bibliothek in SharePoint „Synchronisieren“, den Ordner hier eintragen).",
                         required=False, actions=fix)
    folder = Path(h.folder).expanduser()
    if not is_dir(folder):
        return CheckItem("sharepoint", title, ERROR,
                         f"Übergabeordner {folder} ist nicht erreichbar (OneDrive pausiert oder Ordner verschoben?).",
                         required=False, actions=fix)
    if not synced(folder):
        return CheckItem("sharepoint", title, WARN,
                         f"Ordner {folder} ist erreichbar, liegt aber nicht in einem OneDrive-Ordner – Dateien dort "
                         "werden vermutlich nicht nach SharePoint synchronisiert.", required=False, actions=fix)
    return CheckItem("sharepoint", title, OK, f"Übergabe in {folder} (wird von OneDrive synchronisiert).",
                     required=False, actions=fix)


def summary_line(items: list[CheckItem]) -> tuple[bool, str]:
    """(ready for daily use, one sentence)."""
    blocking = [i for i in items if i.required and i.state == ERROR]
    if blocking:
        return False, "Noch nicht startklar: " + ", ".join(i.title for i in blocking) + "."
    open_optional = [i for i in items if not i.required and i.state in (OPTIONAL, WARN, ERROR)]
    text = "Startklar für Aufnahme, Transkript und Zusammenfassung."
    if open_optional:
        text += " Optional noch offen: " + ", ".join(i.title for i in open_optional) + "."
    return True, text


# ---------------------------------------------------------------------------
# real probes (Windows / installed packages); used by the app
# ---------------------------------------------------------------------------
def real_audio_resolver(cfg: Config) -> Callable[[str, str], tuple[Any, Any]] | None:
    if sys.platform != "win32":
        return None

    def resolve(mic: str, loop: str) -> tuple[Any, Any]:
        from callrec.audio.wasapi import WasapiBackend, com_initialized

        with com_initialized():
            b = WasapiBackend(cfg.detection.teams_processes)
            try:
                return b.resolve_devices(mic, loop)
            finally:
                b.close()

    return resolve


def real_selected_whisper(cfg: Config) -> Callable[[], tuple[str, str]]:
    def selected() -> tuple[str, str]:
        from callrec.asr.gpu import gpu_status, plan_asr

        usable = gpu_status().usable if cfg.transcription.device == "auto" else None
        plan = plan_asr(cfg.transcription, usable)
        return plan.model_for(plan.devices[0]), plan.note

    return selected


def real_find_whisper(model: str) -> Any:
    from callrec.asr.modelcache import find_model

    return find_model(paths.models_dir() / "whisper", model)


def real_key_present() -> bool:
    from callrec import apikey

    return bool(apikey.load_key()[0])


def real_signed_in(cfg: Config) -> Callable[[], str | None]:
    def who() -> str | None:
        from callrec.factory import graph_auth

        return graph_auth(cfg).account_username()  # type: ignore[no-any-return]

    return who


def run_all(cfg: Config, summary_test: tuple[str, bool] | None = None, *, resolve: Any = "real",
            selected: Any = None, find: Any = None, key_present: Any = None, signed_in: Any = None) -> list[CheckItem]:
    """All checks with the real probes (each probe replaceable for tests). Runs on a worker thread."""
    items = []

    def safe(fn: Callable[[], CheckItem], key: str, title: str) -> CheckItem:
        try:
            return fn()
        except Exception as exc:  # a broken probe must not hide the other checks
            log.exception("Prüfung %s fehlgeschlagen", key)
            return CheckItem(key, title, UNKNOWN, f"Prüfung fehlgeschlagen: {exc}")

    res = real_audio_resolver(cfg) if resolve == "real" else resolve
    items.append(safe(lambda: check_audio(cfg, res), "audio", "Audiogeräte"))
    items.append(safe(lambda: check_whisper(cfg, selected or real_selected_whisper(cfg), find or real_find_whisper),
                      "whisper", "Whisper-Modell"))
    t, ok = summary_test if summary_test else (None, None)
    items.append(safe(lambda: check_summary(cfg, key_present or real_key_present, t, ok), "summary", "Zusammenfassung"))
    items.append(safe(lambda: check_microsoft(cfg, signed_in or real_signed_in(cfg)), "microsoft", "Microsoft"))
    items.append(safe(lambda: check_sharepoint(cfg), "sharepoint", "SharePoint"))
    return items


def probe_summary_provider(cfg: Config, provider_factory: Callable[[Any], Any] | None = None) -> tuple[str, bool]:
    """Connection test of the configured summary provider -> (German result text, ok).

    Anthropic: the same mini request as the settings dialog ("Antworte nur mit: OK", ~20 s timeout).
    Local servers: list the installed models and check that the configured one is there."""
    s = cfg.summary
    try:
        if s.provider == "anthropic":
            if provider_factory is not None:
                provider = provider_factory(s)
            else:
                from callrec import apikey
                from callrec.llm_anthropic import TEST_TIMEOUT_S, AnthropicProvider

                provider = AnthropicProvider(apikey.load_key()[0], s.anthropic_model, effort=s.anthropic_effort,
                                             max_attempts=1, timeout_s=TEST_TIMEOUT_S)
            return str(provider.test_connection()), True
        if provider_factory is not None:
            provider = provider_factory(s)
        else:
            from callrec.llm import make_provider

            provider = make_provider(s)
        models = list(provider.list_models())
        if s.model in models or any(m.split(":")[0] == s.model for m in models):
            return f"Server erreichbar, Modell {s.model} ist installiert.", True
        return (f"Server erreichbar, aber Modell {s.model} fehlt (installiert: {', '.join(models) or 'keine'}). "
                "Einstellungen → Zusammenfassung → „Modell laden“."), False
    except Exception as exc:
        return str(exc), False


def readme_path() -> Path | None:
    p = Path(__file__).resolve().parents[2] / "README.md"
    return p if p.exists() else None
