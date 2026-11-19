"""Command line interface (``callrec ...``). Without arguments the tray app starts."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

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
    from callrec.jobs import STEP_LABELS

    sys.stdout.write(f"\r{STEP_LABELS.get(step, step):<22} {int(frac * 100):3d} % {msg:<20}")
    sys.stdout.flush()
    if frac >= 1.0:
        sys.stdout.write("\n")


def cmd_process(args: argparse.Namespace) -> int:
    from callrec.factory import build_pipeline

    cfg = load_config()
    if args.model:
        cfg.transcription.model = args.model
    if args.device:
        cfg.transcription.device = args.device
    if args.language:
        cfg.transcription.language = args.language
    rec = _recording(args.folder)
    result = build_pipeline(cfg).run(rec, args.steps, _print_progress)
    for m in result.messages:
        print(m)
    if result.unassigned_speakers:
        print(f"{result.unassigned_speakers} Sprecher noch nicht zugeordnet: callrec speakers \"{rec.folder}\"")
    print(f"Fertig: {rec.folder}")
    return 0


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
                consent = cli_confirm(input, print, me.display, people)
            except ConsentError as exc:
                print(exc)
                return None
            return StartDecision(title, people, suggestion.event, consent)

        def ask_record_prompt(self, prompt):  # type: ignore[no-untyped-def]
            return False

        def notify(self, title: str, message: str) -> None:
            print(f"[{title}] {message}")

        def set_recording_state(self, recording: bool, title: str) -> None:
            pass

        def set_status(self, text: str) -> None:
            if text:
                print(text)

        def open_review(self, rec) -> None:  # type: ignore[no-untyped-def]
            print(f'Sprecher zuordnen: callrec speakers "{rec.folder}"')

    def status(text: str) -> None:
        if text:
            print(text)

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


def cmd_models(args: argparse.Namespace) -> int:
    from callrec.speakers.modelstore import SEGMENTATION, embedding_model, ensure_model

    cfg = load_config()
    print("Lade Diarisierungsmodelle ...")
    print(" ", ensure_model(SEGMENTATION, paths.models_dir()))
    print(" ", ensure_model(embedding_model(cfg.diarization.embedding_model), paths.models_dir()))
    if args.whisper:
        from faster_whisper import WhisperModel

        print(f"Lade Whisper-Modell {cfg.transcription.model} (einige GB) ...")
        WhisperModel(cfg.transcription.model, device="cpu", compute_type="int8",
                     download_root=str(paths.models_dir() / "whisper"))
    print("Fertig.")
    return 0


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
    path = write_default_config()
    print(path)
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
    pr.add_argument("--language")
    pr.set_defaults(func=cmd_process)

    for name, preset, helptext in (
        ("summarize", "resummarize", "Zusammenfassung neu erstellen"),
        ("retranscribe", "retranscribe", "Neu transkribieren"),
        ("repair", "finalize", "Unterbrochene Aufnahme reparieren/abschließen"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("folder")
        sp.set_defaults(func=cmd_process, steps=_steps(preset), model=None, device=None, language=None)

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

    m = sub.add_parser("models", help="Modelle vorab herunterladen")
    m.add_argument("action", choices=["download"])
    m.add_argument("--whisper", action="store_true", help="auch das Whisper-Modell laden")
    m.set_defaults(func=cmd_models)

    sub.add_parser("list", help="Aufnahmen auflisten").set_defaults(func=cmd_list)

    c = sub.add_parser("config", help="Pfad der Konfigurationsdatei / Prüfung")
    c.add_argument("action", choices=["path", "check"], nargs="?", default="path")
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
