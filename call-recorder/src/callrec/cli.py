"""Command line interface (``callrec ...``). Without arguments the tray app starts."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from callrec import __version__, paths
from callrec.config import ConfigError, load_config, write_default_config
from callrec.pipeline import ALL_STEPS, STEPS

log = logging.getLogger(__name__)

PRESETS = {
    "all": set(STEPS),
    "retranscribe": {"transcribe", "identify", "render", "summarize"},
    "rediarize": {"diarize", "identify", "render", "summarize"},
    "resummarize": {"summarize"},
    "render": {"render"},
}


def _steps(value: str) -> set[str]:
    if value in PRESETS:
        return set(PRESETS[value])
    steps = {s.strip() for s in value.split(",") if s.strip()}
    unknown = steps - ALL_STEPS
    if unknown:
        raise argparse.ArgumentTypeError(f"Unbekannte Schritte: {', '.join(sorted(unknown))}")
    return steps


def _recording(path: str):  # type: ignore[no-untyped-def]
    from callrec.recording import Recording

    rec = Recording(Path(path))
    if not rec.exists():
        raise SystemExit(f"Keine Aufnahme gefunden (meta.json fehlt): {path}")
    return rec


def _print_progress(step: str, frac: float, msg: str) -> None:
    """Pipeline progress: one line updated in place on a terminal, rate-limited otherwise."""
    from callrec.console import default_status
    from callrec.jobs import status_text

    default_status().update(status_text(step, frac, msg))


def _say(text: str) -> None:
    from callrec.console import default_status

    default_status().message(text)


def cmd_process(args: argparse.Namespace) -> int:
    from callrec.factory import build_pipeline

    cfg = load_config(preset=getattr(args, "preset", None))
    if args.model:
        cfg.transcription.model = args.model
    if args.device:
        cfg.transcription.device = args.device
    language = (args.language or "").strip().lower() or None
    if language and language != "auto" and not re.fullmatch(r"[a-z]{2,3}", language):
        raise SystemExit(f"Ungültige Sprache {args.language!r}: Sprachcode wie de oder en, oder auto")
    rec = _recording(args.folder)
    if "summarize" in args.steps and not _cloud_consent_ok(cfg, rec, getattr(args, "confirm_anthropic", False)):
        return 1
    from callrec.asr.transcriber import TranscriptionCancelled

    try:
        pipeline = build_pipeline(cfg)
        result = pipeline.run(rec, args.steps, _print_progress, language=language) if language else \
            pipeline.run(rec, args.steps, _print_progress)
    except (KeyboardInterrupt, TranscriptionCancelled):
        _say("Abgebrochen. Ein erneuter Aufruf setzt fort (bereits geladene Modelldateien bleiben erhalten).")
        return 130
    from callrec.console import default_status

    default_status().finish()
    for m in result.messages:
        print(m)
    if result.unassigned_speakers:
        print(f"{result.unassigned_speakers} Sprecher noch nicht zugeordnet: callrec speakers \"{rec.folder}\"")
    print(f"Fertig: {rec.folder}")
    if cfg.active_preset:
        print(f"(Preset: {cfg.active_preset})")
    return 0


def _cloud_consent_ok(cfg, rec, confirm: bool) -> bool:  # type: ignore[no-untyped-def]
    """Recording made before Anthropic was selected: send its transcript only after a typed 'ja'."""
    from callrec.consent import (
        LATER_STATEMENT_ANTHROPIC,
        ConsentError,
        cloud_summary_covered,
        later_confirmation,
        record_later_confirmation,
    )

    provider = cfg.summary.provider if cfg.summary.enabled else ""
    if cloud_summary_covered(rec.load_meta(), provider):
        return True
    if not confirm:
        print("Die Einwilligung dieser Aufnahme erwähnt die Übertragung an Anthropic nicht. Mit "
              "--confirm-anthropic können Sie die Zustimmung aller Teilnehmenden nachträglich bestätigen.")
        return False
    if sys.stdin is None or not sys.stdin.isatty():
        print("Die Bestätigung muss in einem Konsolenfenster eingetippt werden.")
        return False
    print(LATER_STATEMENT_ANTHROPIC)
    answer = input("Bitte 'ja' eingeben, um zu bestätigen: ").strip().lower()
    me = (rec.load_meta().get("self") or {}).get("name") or "Benutzer"
    try:
        record_later_confirmation(rec, later_confirmation(str(me), "cli", answer == "ja"))
    except ConsentError as exc:
        print(exc)
        return False
    return True


def cmd_speakers(args: argparse.Namespace) -> int:
    from callrec.factory import profile_store
    from callrec.review import name_choices, speaker_overview
    from callrec.transcript import fmt_ts

    rec = _recording(args.folder)
    for ov in speaker_overview(rec):
        state = f"= {ov.name}" + (f" (automatisch, {ov.score:.2f})" if ov.source == "auto" and ov.score is not None else "") if ov.name else "(nicht zugeordnet)"
        print(f"{ov.id:<4} {ov.label:<12} {fmt_ts(ov.talk_time_s)}  {state}")
        if ov.sample_text:
            print(f"      „{ov.sample_text}“")
    choices = name_choices(rec, profile_store())
    if choices:
        print("\nMögliche Namen: " + ", ".join(choices))
    print('\nZuordnen mit: callrec assign "<Ordner>" S1="Vorname Nachname" ...')
    return 0


def cmd_assign(args: argparse.Namespace) -> int:
    from callrec.factory import build_pipeline, profile_store
    from callrec.review import apply_assignments, parse_cli_assignments

    cfg = load_config()
    rec = _recording(args.folder)
    profiles = profile_store()
    pipeline = build_pipeline(cfg, profiles)
    msgs = apply_assignments(rec, parse_cli_assignments(args.assignments, args.no_profile), profiles,
                             lambda: pipeline.run(rec, {"render"}))
    for m in msgs:
        print(m)
    if args.resummarize:
        pipeline.run(rec, {"summarize"}, _print_progress)
    print(f"Transkript aktualisiert: {rec.transcript_path}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    import tkinter as tk

    from callrec.factory import build_pipeline, profile_store
    from callrec.ui.dialogs import review_speakers

    cfg = load_config()
    rec = _recording(args.folder)
    profiles = profile_store()
    pipeline = build_pipeline(cfg, profiles)
    root = tk.Tk()
    root.withdraw()
    review_speakers(root, rec, profiles, lambda: pipeline.run(rec, {"render"}),
                    lambda: pipeline.run(rec, {"summarize"}, _print_progress))
    root.destroy()
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    from callrec.factory import profile_store

    store = profile_store()
    if args.action == "list":
        profs = store.all_profiles()
        if not profs:
            print("Keine Stimmprofile gespeichert.")
        for p in profs:
            print(f"{p.id[:8]}  {p.name:<28} {p.email:<30} Proben: {p.samples:<3} Modell: {p.model_id}  ({p.updated_at})")
        return 0
    if args.action == "delete":
        if not args.key:
            raise SystemExit("Bitte Name, E-Mail oder ID angeben")
        deleted = store.delete(args.key)
        if not deleted:
            print(f"Kein Profil für {args.key!r} gefunden.")
            return 1
        for p in deleted:
            print(f"Gelöscht: {p.name} ({p.id[:8]})")
        return 0
    if args.action == "delete-all":
        if not args.yes and input("ALLE Stimmprofile löschen? (ja/nein) ").strip().lower() != "ja":
            print("Abgebrochen.")
            return 1
        print(f"{store.delete_all()} Profil(e) gelöscht.")
        return 0
    return 2


def cmd_purge_voiceprints(args: argparse.Namespace) -> int:
    """Delete the per-recording speaker embeddings (biometric data) of one or all recordings."""
    from callrec.recording import list_recordings

    if args.all:
        recs = list_recordings(load_config().recordings_path())
    elif args.folder:
        recs = [_recording(args.folder)]
    else:
        raise SystemExit("Ordner oder --all angeben")
    n = sum(1 for r in recs if r.delete_embeddings())
    print(f"Stimm-Embeddings in {n} Aufnahme(n) gelöscht. (Neue Sprecherzuordnung danach nur nach 'callrec process <Ordner> --steps rediarize'.)")
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    """Hand over the SUMMARY (only) of one recording to SharePoint - explicit action."""
    from callrec.factory import perform_handoff
    from callrec.handoff import HANDOFF_WARNING, HandoffError, build_summary_document, last_handoff

    cfg = load_config()
    rec = _recording(args.folder)
    try:
        doc = build_summary_document(rec, cfg.handoff.include_participant_emails)
    except HandoffError as exc:
        print(exc)
        return 1
    target = cfg.handoff.folder if cfg.handoff.mode == "folder" else f"Graph-Upload ({cfg.handoff.graph_folder or '/'})"
    print(HANDOFF_WARNING)
    print(f"\nZiel: {target}\nDatei: {doc.filename}\n" + "-" * 60)
    print(doc.markdown)
    print("-" * 60)
    prev = last_handoff(rec)
    if prev:
        print(f"Zuletzt übergeben: {prev.get('at')} -> {prev.get('location')}"
              + (" (identischer Inhalt)" if prev.get("sha256") == doc.sha256 else ""))
    if not args.yes and input("Jetzt übergeben? (ja/nein) ").strip().lower() != "ja":
        print("Abgebrochen.")
        return 1
    try:
        entry = perform_handoff(cfg, rec, doc.sha256)  # exactly what was shown above
    except Exception as exc:
        print(f"Übergabe fehlgeschlagen: {exc}")
        return 1
    print(f"Übergeben: {entry['location']}")
    return 0


def cmd_llm(args: argparse.Namespace) -> int:
    from callrec.llm import OLLAMA_UPDATE_HINT, LlmUnavailable, make_provider

    cfg = load_config()
    try:
        p = make_provider(cfg.summary)
        if args.action == "models":
            for m in p.list_models():
                print(("* " if m == cfg.summary.model else "  ") + m)
        elif args.action == "version":
            v = p.version()
            print(f"{p.name} {v or '(Version nicht abfragbar)'} unter {p.base_url}")
            print(OLLAMA_UPDATE_HINT)
        elif args.action == "pull":
            model = args.model or cfg.summary.model

            def progress(status: str, frac: float | None) -> None:
                sys.stdout.write(f"\r{status:<50} {'' if frac is None else f'{frac * 100:5.1f} %'}")
                sys.stdout.flush()

            p.pull(model, progress)
            print(f"\nModell {model} ist aktuell.")
    except (LlmUnavailable, ValueError) as exc:
        print(exc)
        return 1
    return 0


def cmd_preset(args: argparse.Namespace) -> int:
    from callrec.config import list_presets, save_preset

    if args.action == "list":
        active = load_config(preset="").general.preset
        for n in list_presets():
            print(("* " if n == active else "  ") + n)
        return 0
    if not args.name:
        raise SystemExit("Bitte Preset-Namen angeben")
    if args.action == "save":
        print(save_preset(load_config(), args.name))
        return 0
    path = paths.presets_dir() / f"{args.name}.toml"
    print(path.read_text(encoding="utf-8") if path.exists() else f"Preset {args.name!r} nicht gefunden")
    return 0


def cmd_versions(args: argparse.Namespace) -> int:
    from callrec.versions import list_versions

    rec = _recording(args.folder)
    for kind in ("transcript", "summary"):
        print(f"{kind}:")
        for v in list_versions(rec, kind):
            st = v.get("settings") or {}
            t = st.get("transcription") or {}
            extra = f"Modell {t.get('model', '?')}, Preset {st.get('preset') or '-'}" if kind == "transcript" else \
                f"LLM {(st.get('llm') or {}).get('model', '?')}, aus Transkript v{st.get('transcript_version')}"
            print(f"  v{v['version']}  {v.get('created', '')}  {v.get('file', '')}  ({extra})")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    from callrec.calendar.graph import GraphClient
    from callrec.factory import graph_auth

    cfg = load_config()
    if not cfg.calendar.client_id.strip():
        raise SystemExit(f"calendar.client_id ist leer - bitte in {paths.config_file()} eintragen (siehe README).")
    auth = graph_auth(cfg)
    if args.action == "logout":
        auth.logout()
        print("Abgemeldet, Token-Cache geleert.")
        return 0
    if args.action == "login":
        auth.login_interactive()
    token = auth.token_silent()
    if not token:
        print("Nicht angemeldet. Bitte: callrec login")
        return 1
    me = GraphClient(lambda: token, timeout=cfg.calendar.request_timeout_s).get_me()
    print(f"Angemeldet als {me.name} <{me.email}>")
    return 0


def cmd_event(args: argparse.Namespace) -> int:
    from callrec.factory import build_calendar_service

    cfg = load_config()
    svc = build_calendar_service(cfg)
    s = svc.suggest_from_ics(Path(args.ics)) if args.ics else svc.suggest()
    if s.message:
        print(s.message)
    if s.event is None:
        print("Kein Termin ausgewählt.")
        return 1
    ev = s.event
    from callrec.naming import recording_basename

    print(f"Termin:   {ev.subject}  ({ev.start.astimezone():%d.%m.%Y %H:%M}–{ev.end.astimezone():%H:%M}, online={ev.is_online})")
    print("Personen: " + ", ".join(p.display for p in s.participants))
    print(f"Ordner:   {recording_basename(ev.start.astimezone(), ev.subject)}")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    from callrec.audio.devices import describe
    from callrec.audio.wasapi import WasapiBackend

    cfg = load_config()
    b = WasapiBackend(cfg.detection.teams_processes)
    try:
        print("Mikrofone (WASAPI):")
        for d in b.capture_devices():
            print(f"  [{d.id}] {describe(d)}")
        print("Ausgabegeräte (Loopback):")
        for d in b.loopback_devices():
            print(f"  [{d.id}] {describe(d)}")
        mic, loop = b.resolve_devices(cfg.audio.mic_device, cfg.audio.loopback_device)
        print(f"\nAktuelle Auswahl:\n  Mikrofon:   {describe(mic)}\n  Gegenseite: {describe(loop)}")
    finally:
        b.close()
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Console recording (useful for a first test on Windows)."""
    from callrec.audio.wasapi import WasapiBackend
    from callrec.calendar.service import Suggestion
    from callrec.consent import ConsentError, cli_confirm
    from callrec.controller import Controller, StartDecision
    from callrec.factory import build_calendar_service, build_pipeline
    from callrec.jobs import ProcessingQueue
    from callrec.people import parse_participants_text
    from callrec.ui.app import MeProvider

    cfg = load_config()
    calendar = build_calendar_service(cfg)

    class ConsoleUi:
        def ask_start(self, suggestion: Suggestion, me):  # type: ignore[no-untyped-def]
            title = args.title or (suggestion.event.subject if suggestion.event else "") or input("Titel: ").strip() or "Anruf"
            people = parse_participants_text(args.participants) if args.participants else suggestion.participants
            if suggestion.message:
                print(suggestion.message)
            try:
                from callrec.consent import active_summary_provider

                tty = sys.stdin is not None and sys.stdin.isatty()
                consent = cli_confirm(input, print, me.display, people, interactive=tty,
                                      summary_provider=active_summary_provider(cfg.summary))
            except ConsentError as exc:
                print(exc)
                return None
            return StartDecision(title, people, suggestion.event, consent)

        def ask_record_prompt(self, prompt):  # type: ignore[no-untyped-def]
            return False

        def notify(self, title: str, message: str) -> None:
            _say(f"[{title}] {message}")

        def set_recording_state(self, recording: bool, title: str) -> None:
            pass

        def set_status(self, text: str) -> None:
            status(text)

        def open_review(self, rec) -> None:  # type: ignore[no-untyped-def]
            print(f'Sprecher zuordnen: callrec speakers "{rec.folder}"')

    from callrec.console import default_status

    def status(text: str) -> None:
        if text:
            default_status().update(text)
        else:
            default_status().finish()

    queue = ProcessingQueue(lambda: build_pipeline(cfg), status=status)
    ctl = Controller(cfg, ConsoleUi(), lambda: WasapiBackend(cfg.detection.teams_processes), calendar, queue, MeProvider(cfg))
    suggestion = calendar.suggest_from_ics(Path(args.ics)) if args.ics else (Suggestion(None) if args.no_calendar else None)
    if not ctl.start(suggestion):
        return 1
    try:
        input("Aufnahme läuft. Enter drücken zum Beenden ...")
    except (KeyboardInterrupt, EOFError):
        pass
    if args.no_process:
        ctl.process = lambda rec, steps=ALL_STEPS: None  # type: ignore[method-assign]
    rec = ctl.stop()
    if rec is not None and not args.no_process:
        queue.stop()
        queue.join()
    return 0


def run_whisper_download(model: str, download_root: Path, out: Callable[[str], None] = print,
                         command_prefix: Sequence[str] | None = None, status: Any = None) -> int:
    """Download a Whisper model in a child process with progress; Ctrl+C stops it.

    Returns 0 only if the worker verified the complete model afterwards (``DOWNLOADED`` line).
    """
    import subprocess

    from callrec.asr.download import DownloadDisplay
    from callrec.asr.transcriber import console_python
    from callrec.console import ConsoleStatus

    status = status or ConsoleStatus()
    cmd = [*(command_prefix or [console_python(), "-m", "callrec.asr.worker"]),
           "--download-only", "--model", model, "--download-root", str(download_root)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env)  # noqa: S603
    assert proc.stdout is not None
    display = DownloadDisplay()
    failure = ""
    try:
        for line in proc.stdout:
            parts = line.split()
            if parts[:2] == ["STAGE", "download"]:
                status.message("Download läuft (Abbrechen mit Strg+C; ein erneuter Aufruf setzt fort) ...")
            elif parts[:1] == ["FAILED"]:
                failure = line.strip()[len("FAILED"):].strip()
            elif parts[:1] in (["DOWNLOAD"], ["DOWNLOADDISK"], ["DOWNLOADED"]):
                text = display.feed(parts)
                if text and parts[0] != "DOWNLOADED":
                    status.update(text)
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        status.message("Download abgebrochen. Erneut starten mit: callrec models --whisper")
        return 130
    status.finish()
    if rc == 0 and display.final:
        out(display.final)
        return 0
    if failure:
        out(f"FEHLER: {failure}")
    return rc or 3


def _whisper_root() -> Path:
    return paths.models_dir() / "whisper"


def _selected_whisper_model(cfg: Any) -> tuple[str, str]:
    from callrec.asr.gpu import gpu_status, plan_asr

    usable = gpu_status().usable if cfg.transcription.device == "auto" else None
    plan = plan_asr(cfg.transcription, usable)
    return plan.model_for(plan.devices[0]), plan.note


def cmd_models(args: argparse.Namespace) -> int:
    from callrec.asr.download import fmt_bytes
    from callrec.asr.modelcache import find_model, installed_models, remove_model

    cfg = load_config()
    root = _whisper_root()
    if args.action == "list":
        models = installed_models(root)
        current, _note = _selected_whisper_model(cfg)
        if not models:
            print(f"Keine Whisper-Modelle in {root}.")
        for m in models:
            print(("* " if current in (m.name, m.repo) else "  ") + m.describe())
        if models:
            print(f"\nGesamt: {fmt_bytes(sum(m.size for m in models))} in {root}")
            print(f"* = wird verwendet ({current}). Entfernen: callrec models remove <Name>")
        return 0
    if args.action == "remove":
        if not args.name:
            raise SystemExit("Bitte den Modellnamen angeben (siehe: callrec models list)")
        m = find_model(root, args.name)
        if m is None:
            print(f"Modell {args.name!r} ist nicht installiert (callrec models list).")
            return 1
        current, _note = _selected_whisper_model(cfg)
        if current in (m.name, m.repo):
            print(f"Achtung: {m.name} ist das Modell, das die Transkription verwendet - es wird bei der nächsten "
                  "Transkription erneut heruntergeladen.")
        if not args.yes and input(f"{m.name} ({fmt_bytes(m.size)}) löschen? (ja/nein) ").strip().lower() != "ja":
            print("Abgebrochen.")
            return 1
        try:
            freed = remove_model(m, root)
        except OSError as exc:
            print(f"Löschen fehlgeschlagen (wird das Modell gerade verwendet?): {exc}")
            return 1
        print(f"Gelöscht: {m.name}, {fmt_bytes(freed)} frei.")
        return 0

    from callrec.speakers.modelstore import SEGMENTATION, embedding_model, ensure_model

    print("Lade Diarisierungsmodelle ...")
    print(" ", ensure_model(SEGMENTATION, paths.models_dir()))
    print(" ", ensure_model(embedding_model(cfg.diarization.embedding_model), paths.models_dir()))
    if args.whisper:
        model = args.model or args.name
        if not model:
            model, note = _selected_whisper_model(cfg)
            if note:
                print(note)
        print(f"Lade Whisper-Modell {model} (je nach Modell 1,5-3 GB) ...")
        rc = run_whisper_download(model, root)
        if rc != 0:
            if rc != 130:
                print(f"Download fehlgeschlagen (Exit-Code {rc}) - das Modell ist NICHT vollständig. "
                      "Erneut versuchen: callrec models --whisper; Details: callrec -v models --whisper")
            return rc
        m = find_model(root, model)
        if m is not None and not m.complete:
            print(f"FEHLER: Modell {model} ist nicht vollständig im Modellordner.")
            return 3
    print("Fertig.")
    return 0


def cmd_gpu(args: argparse.Namespace) -> int:
    """Show (and with --recheck re-test) whether an NVIDIA GPU is usable, and what transcription will use."""
    from callrec.asr.gpu import describe_effective, gpu_status

    cfg = load_config()
    status = gpu_status(force=args.recheck)
    print(status.describe() + (f" – geprüft {status.checked_at}" if status.checked_at else ""))
    print(describe_effective(cfg.transcription, status))
    return 0


def cmd_anthropic(args: argparse.Namespace) -> int:
    """Claude (Anthropic API) as summary provider: key, opt-in, connection test."""
    import getpass

    from callrec import apikey
    from callrec.config import save_config
    from callrec.llm_anthropic import ANTHROPIC_SETTINGS_WARNING, AnthropicError

    if args.action == "set-key":
        if sys.stdin is not None and sys.stdin.isatty():
            key = getpass.getpass("Anthropic-API-Schlüssel (Eingabe unsichtbar): ")
        else:
            key = sys.stdin.readline() if sys.stdin is not None else ""
        try:
            apikey.store_key(key)
        except apikey.ApiKeyError as exc:
            print(exc)
            return 1
        print(f"Schlüssel gespeichert ({apikey.mask(key.strip())}) in {apikey.key_file()} (Windows-DPAPI-verschlüsselt).")
        return 0
    if args.action == "delete-key":
        print("Schlüssel gelöscht." if apikey.delete_key() else "Kein gespeicherter Schlüssel vorhanden.")
        return 0
    base = load_config(preset="")
    if args.action == "status":
        key, source = apikey.load_key()
        s = base.summary
        print(f"Anbieter: {s.provider}" + (" (Claude über die Anthropic-API)" if s.provider == "anthropic" else ""))
        print(f"Übertragung an Anthropic bestätigt: {'ja' if s.anthropic_transcripts_leave_this_computer_confirmed else 'nein'}")
        print(f"Modell: {s.anthropic_model}, Denktiefe (effort): {s.anthropic_effort or '-'}")
        print(f"API-Schlüssel: {apikey.mask(key) + ' – ' + source if key else 'nicht hinterlegt'}")
        return 0
    if args.action == "enable":
        print(ANTHROPIC_SETTINGS_WARNING)
        if sys.stdin is None or not sys.stdin.isatty():
            print("Die Bestätigung muss in einem Konsolenfenster eingetippt werden.")
            return 1
        if input("Bitte 'ja' eingeben, um Claude (Anthropic) zu aktivieren: ").strip().lower() != "ja":
            print("Abgebrochen - nichts geändert.")
            return 1
        base.summary.provider = "anthropic"
        base.summary.anthropic_transcripts_leave_this_computer_confirmed = True
        if args.model:
            base.summary.anthropic_model = args.model
        save_config(base)
        print(f"Aktiviert (Modell {base.summary.anthropic_model}). Gilt für neue Aufnahmen; die Einwilligungsabfrage "
              "nennt jetzt die Übertragung an Anthropic.")
        if not apikey.load_key()[0]:
            print("Noch kein API-Schlüssel: callrec anthropic set-key")
        return 0
    if args.action == "disable":
        base.summary.provider = "ollama" if base.summary.provider == "anthropic" else base.summary.provider
        base.summary.anthropic_transcripts_leave_this_computer_confirmed = False
        save_config(base)
        print(f"Claude (Anthropic) deaktiviert. Anbieter jetzt: {base.summary.provider}.")
        return 0
    if args.action == "test":
        from callrec.llm import make_provider

        cfg = base.summary
        cfg.provider, cfg.anthropic_transcripts_leave_this_computer_confirmed = "anthropic", True  # test only, not saved
        if args.model:
            cfg.anthropic_model = args.model
        try:
            print(make_provider(cfg).test_connection())  # type: ignore[attr-defined]
        except AnthropicError as exc:
            print(exc)
            return 1
        return 0
    return 2


def cmd_list(args: argparse.Namespace) -> int:
    from callrec.recording import list_recordings

    cfg = load_config()
    for rec in list_recordings(cfg.recordings_path()):
        meta = rec.load_meta()
        st = (meta.get("recording") or {}).get("status", "?")
        ps = (meta.get("processing") or {}).get("status", "-")
        print(f"{rec.folder.name:<60} Aufnahme: {st:<10} Verarbeitung: {ps}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    from callrec.userfiles import ensure_user_files

    path = write_default_config()
    print(path)
    if args.action == "files":
        for f in ensure_user_files():
            print(f)
        print(paths.presets_dir())
    if args.action == "check":
        cfg = load_config()
        for w in cfg.warnings:
            print("Warnung:", w)
        print("Konfiguration ist gültig.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="callrec", description="Lokaler Gesprächsrekorder mit Transkript und Zusammenfassung")
    p.add_argument("--version", action="version", version=f"callrec {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("tray", help="Tray-Anwendung starten (Standard)")

    r = sub.add_parser("record", help="Aufnahme in der Konsole (Test)")
    r.add_argument("--title")
    r.add_argument("--participants", help='z.B. "Anna Beispiel <anna@firma.de>; Bernd"')
    r.add_argument("--ics", help="Einladung als .ics")
    r.add_argument("--no-calendar", action="store_true")
    r.add_argument("--no-process", action="store_true")
    r.set_defaults(func=cmd_record)

    pr = sub.add_parser("process", help="Aufnahme (erneut) verarbeiten")
    pr.add_argument("folder")
    pr.add_argument("--steps", type=_steps, default=set(STEPS),
                    help=f"Kommagetrennt aus {','.join(STEPS)} oder Preset: {', '.join(PRESETS)}")
    pr.add_argument("--model")
    pr.add_argument("--device", choices=["auto", "cuda", "cpu"])
    pr.add_argument("--language", help="Sprache für diese Verarbeitung: de, en … oder auto (neu erkennen)")
    pr.add_argument("--preset", help="Preset für diese Verarbeitung (überschreibt general.preset)")
    pr.set_defaults(func=cmd_process)

    pr.add_argument("--confirm-anthropic", action="store_true", help=argparse.SUPPRESS)
    for name, preset, helptext in (
        ("summarize", "resummarize", "Zusammenfassung neu erstellen"),
        ("retranscribe", "retranscribe", "Neu transkribieren"),
        ("repair", "finalize", "Unterbrochene Aufnahme reparieren/abschließen"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("folder")
        sp.add_argument("--preset")
        sp.add_argument("--confirm-anthropic", action="store_true",
                        help="ältere Aufnahme: Zustimmung zur Übertragung an Anthropic nachträglich bestätigen")
        if name == "retranscribe":
            sp.add_argument("--language", help="Sprache für diese Neu-Transkription: de, en … oder auto (neu erkennen)")
        sp.set_defaults(func=cmd_process, steps=_steps(preset), model=None, device=None)
        if name != "retranscribe":
            sp.set_defaults(language=None)

    s = sub.add_parser("speakers", help="Sprecher einer Aufnahme anzeigen")
    s.add_argument("folder")
    s.set_defaults(func=cmd_speakers)

    a = sub.add_parser("assign", help='Sprecher zuordnen: S1="Anna Beispiel"')
    a.add_argument("folder")
    a.add_argument("assignments", nargs="+")
    a.add_argument("--no-profile", action="store_true", help="kein Stimmprofil speichern")
    a.add_argument("--resummarize", action="store_true")
    a.set_defaults(func=cmd_assign)

    rv = sub.add_parser("review", help="Sprecher-Zuordnungsdialog öffnen")
    rv.add_argument("folder")
    rv.set_defaults(func=cmd_review)

    pf = sub.add_parser("profiles", help="Stimmprofile anzeigen/löschen")
    pf.add_argument("action", choices=["list", "delete", "delete-all"])
    pf.add_argument("key", nargs="?")
    pf.add_argument("--yes", action="store_true")
    pf.set_defaults(func=cmd_profiles)

    ho = sub.add_parser("handoff", help="Zusammenfassung (nur diese!) an SharePoint übergeben")
    ho.add_argument("folder")
    ho.add_argument("--yes", action="store_true", help="ohne Rückfrage")
    ho.set_defaults(func=cmd_handoff)

    lm = sub.add_parser("llm", help="KI-Modelle für die Zusammenfassung: models | version | pull [Modell]")
    lm.add_argument("action", choices=["models", "version", "pull"])
    lm.add_argument("model", nargs="?")
    lm.set_defaults(func=cmd_llm)

    ps = sub.add_parser("preset", help="Presets: list | save <Name> | show <Name>")
    ps.add_argument("action", choices=["list", "save", "show"])
    ps.add_argument("name", nargs="?")
    ps.set_defaults(func=cmd_preset)

    vs = sub.add_parser("versions", help="Transkript-/Zusammenfassungs-Versionen einer Aufnahme")
    vs.add_argument("folder")
    vs.set_defaults(func=cmd_versions)

    pv = sub.add_parser("purge-voiceprints", help="Stimm-Embeddings in Aufnahmeordnern löschen")
    pv.add_argument("folder", nargs="?")
    pv.add_argument("--all", action="store_true")
    pv.set_defaults(func=cmd_purge_voiceprints)

    for name in ("login", "logout", "whoami"):
        lp = sub.add_parser(name, help={"login": "Bei Microsoft anmelden", "logout": "Abmelden", "whoami": "Anmeldestatus"}[name])
        lp.set_defaults(func=cmd_login, action=name)

    ev = sub.add_parser("event", help="Zeigt, welcher Termin jetzt gewählt würde")
    ev.add_argument("--ics")
    ev.set_defaults(func=cmd_event)

    sub.add_parser("devices", help="Audiogeräte anzeigen").set_defaults(func=cmd_devices)

    m = sub.add_parser("models", help="Modelle laden (download), installierte Whisper-Modelle anzeigen (list) "
                       "oder löschen (remove <Name>)")
    m.add_argument("action", choices=["download", "list", "remove"], nargs="?", default="download")
    m.add_argument("name", nargs="?", help="Whisper-Modell für remove (bzw. download --whisper)")
    m.add_argument("--whisper", action="store_true", help="auch das Whisper-Modell laden")
    m.add_argument("--model", help="Whisper-Modell (Standard: das für diesen Rechner gewählte)")
    m.add_argument("--yes", action="store_true", help="ohne Rückfrage löschen")
    m.set_defaults(func=cmd_models)

    gp = sub.add_parser("gpu", help="Ist eine NVIDIA-GPU nutzbar? Effektives Gerät/Modell anzeigen")
    gp.add_argument("--recheck", action="store_true", help="erneut prüfen (sonst gespeichertes Ergebnis)")
    gp.set_defaults(func=cmd_gpu)

    an = sub.add_parser("anthropic", help="Claude (Anthropic-API) für die Zusammenfassung: "
                        "set-key | delete-key | status | test | enable | disable")
    an.add_argument("action", choices=["set-key", "delete-key", "status", "test", "enable", "disable"])
    an.add_argument("--model", help=f"Claude-Modell (Standard {'claude-opus-5-5'})")
    an.set_defaults(func=cmd_anthropic)

    sub.add_parser("list", help="Aufnahmen auflisten").set_defaults(func=cmd_list)

    c = sub.add_parser("config", help="Pfad der Konfigurationsdatei / Prüfung")
    c.add_argument("action", choices=["path", "check", "files"], nargs="?", default="path")
    c.set_defaults(func=cmd_config)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd in (None, "tray"):
        return main_tray()
    from callrec.logging_setup import setup_logging

    setup_logging(logging.DEBUG if args.verbose else logging.INFO, console=args.verbose)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2


def main_tray() -> int:
    from callrec.ui.app import run_tray_app

    return run_tray_app()
