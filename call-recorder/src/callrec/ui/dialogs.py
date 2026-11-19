"""Tk dialogs. All functions must be called on the Tk thread."""

from __future__ import annotations

import logging
import tempfile
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from callrec.calendar.service import CalendarService, Suggestion
from callrec.consent import CONSENT_STATEMENT, ConsentError, confirm_consent
from callrec.controller import StartDecision
from callrec.detect import Prompt
from callrec.models import Person
from callrec.people import format_participants, parse_participants_text
from callrec.recording import Recording
from callrec.review import Assignment, apply_assignments, name_choices, speaker_overview
from callrec.speakers.profiles import ProfileStore
from callrec.transcript import fmt_ts

log = logging.getLogger(__name__)

PAD: dict[str, Any] = {"padx": 8, "pady": 4}


def _center(win: tk.Toplevel) -> None:
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    x = (win.winfo_screenwidth() - w) // 2
    y = (win.winfo_screenheight() - h) // 3
    win.geometry(f"+{max(0, x)}+{max(0, y)}")


def _modal(win: tk.Toplevel) -> None:
    win.attributes("-topmost", True)
    _center(win)
    win.lift()
    win.focus_force()
    win.grab_set()


# ---------------------------------------------------------------------------
# Start + consent
# ---------------------------------------------------------------------------
def ask_start(root: tk.Tk, suggestion: Suggestion, me: Person, calendar: CalendarService) -> StartDecision | None:
    win = tk.Toplevel(root)
    win.title("Aufnahme vorbereiten")
    win.resizable(True, False)
    result: dict[str, StartDecision | None] = {"value": None}
    state = {"suggestion": suggestion}

    info_var = tk.StringVar(value=_suggestion_text(suggestion))
    ttk.Label(win, textvariable=info_var, wraplength=520, foreground="#444").grid(row=0, column=0, columnspan=2, sticky="w", **PAD)

    ttk.Label(win, text="Titel:").grid(row=1, column=0, sticky="w", **PAD)
    title_var = tk.StringVar(value=suggestion.event.subject if suggestion.event else "")
    title_entry = ttk.Entry(win, textvariable=title_var, width=60)
    title_entry.grid(row=1, column=1, sticky="we", **PAD)

    ttk.Label(win, text="Teilnehmende\n(eine Person pro Zeile,\noptional: Name <E-Mail>):").grid(row=2, column=0, sticky="nw", **PAD)
    people_txt = tk.Text(win, width=60, height=7, wrap="none")
    people_txt.grid(row=2, column=1, sticky="we", **PAD)
    people_txt.insert("1.0", format_participants(suggestion.participants))

    def import_ics() -> None:
        path = filedialog.askopenfilename(
            parent=win, title="Einladung (.ics) öffnen", filetypes=[("Kalenderdatei", "*.ics"), ("Alle Dateien", "*.*")]
        )
        if not path:
            return
        try:
            s = calendar.suggest_from_ics(Path(path))
        except Exception as exc:
            messagebox.showerror("Import fehlgeschlagen", f"Die Datei konnte nicht gelesen werden:\n{exc}", parent=win)
            return
        state["suggestion"] = s
        info_var.set(_suggestion_text(s))
        if s.event:
            title_var.set(s.event.subject)
            people_txt.delete("1.0", "end")
            people_txt.insert("1.0", format_participants(s.participants))

    ttk.Button(win, text=".ics-Einladung importieren …", command=import_ics).grid(row=3, column=1, sticky="w", **PAD)

    frame = ttk.LabelFrame(win, text="Einwilligung aller Teilnehmenden (§ 201 StGB)")
    frame.grid(row=4, column=0, columnspan=2, sticky="we", padx=8, pady=8)
    ttk.Label(frame, text=CONSENT_STATEMENT, wraplength=520).pack(anchor="w", padx=8, pady=4)
    consent_var = tk.BooleanVar(value=False)  # never pre-checked
    start_btn: ttk.Button

    def on_check() -> None:
        start_btn.state(["!disabled"] if consent_var.get() else ["disabled"])

    ttk.Checkbutton(
        frame, text="Ja, alle Teilnehmenden haben der Aufnahme zugestimmt.", variable=consent_var, command=on_check
    ).pack(anchor="w", padx=8, pady=4)

    buttons = ttk.Frame(win)
    buttons.grid(row=5, column=0, columnspan=2, sticky="e", **PAD)

    def do_start() -> None:
        title = title_var.get().strip() or "Anruf"
        participants = parse_participants_text(people_txt.get("1.0", "end"))
        try:
            consent = confirm_consent(
                user_checked_box=bool(consent_var.get()),
                confirmed_by=me.display or "Benutzer",
                participants=participants,
                method="dialog",
            )
        except ConsentError as exc:
            messagebox.showwarning("Einwilligung fehlt", str(exc), parent=win)
            return
        sugg = state["suggestion"]
        result["value"] = StartDecision(title, participants, sugg.event, consent)
        win.destroy()

    start_btn = ttk.Button(buttons, text="Aufnahme starten", command=do_start)
    start_btn.state(["disabled"])
    start_btn.pack(side="right", padx=4)
    ttk.Button(buttons, text="Abbrechen", command=win.destroy).pack(side="right", padx=4)
    win.bind("<Escape>", lambda e: win.destroy())
    win.columnconfigure(1, weight=1)
    _modal(win)
    title_entry.focus_set()
    root.wait_window(win)
    return result["value"]


def _suggestion_text(s: Suggestion) -> str:
    if s.event:
        ev = s.event
        src = {"graph": "Outlook-Kalender", "cache": "Kalender (zwischengespeichert)", "ics": ".ics-Datei"}.get(s.source, s.source)
        return f"Termin aus {src}: {ev.subject} ({ev.start.astimezone():%H:%M}–{ev.end.astimezone():%H:%M})" + (
            f"\n{s.message}" if s.message and s.source != "ics" else ""
        )
    return s.message or "Kein Termin gefunden - bitte Titel und Teilnehmende eingeben."


# ---------------------------------------------------------------------------
# Reminder
# ---------------------------------------------------------------------------
def ask_record_prompt(root: tk.Tk, prompt: Prompt, timeout_s: int = 90) -> bool:
    win = tk.Toplevel(root)
    win.title("Gespräch aufnehmen?")
    answer = {"value": False}
    ttk.Label(win, text=prompt.message(), wraplength=360).pack(padx=12, pady=10)
    ttk.Label(win, text="Es wird nichts aufgenommen, bevor Sie die Einwilligung bestätigt haben.", foreground="#666", wraplength=360).pack(padx=12)
    bar = ttk.Frame(win)
    bar.pack(pady=10)

    def yes() -> None:
        answer["value"] = True
        win.destroy()

    ttk.Button(bar, text="Aufnahme vorbereiten …", command=yes).pack(side="left", padx=6)
    ttk.Button(bar, text="Nein, danke", command=win.destroy).pack(side="left", padx=6)
    win.attributes("-topmost", True)
    # bottom-right corner, like a notification
    win.update_idletasks()
    x = win.winfo_screenwidth() - win.winfo_width() - 24
    y = win.winfo_screenheight() - win.winfo_height() - 80
    win.geometry(f"+{x}+{y}")
    def expire() -> None:
        if win.winfo_exists():
            win.destroy()

    win.after(timeout_s * 1000, expire)
    root.wait_window(win)
    return answer["value"]


# ---------------------------------------------------------------------------
# Speaker review
# ---------------------------------------------------------------------------
def _play(path: Path) -> None:
    try:
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)  # type: ignore[attr-defined]
    except ImportError:
        log.info("Wiedergabe nur unter Windows verfügbar: %s", path)


def _stop_playback() -> None:
    try:
        import winsound

        winsound.PlaySound(None, 0)  # type: ignore[attr-defined]
    except ImportError:
        pass


def review_speakers(
    root: tk.Tk,
    rec: Recording,
    profiles: ProfileStore,
    rerender: Callable[[], object],
    resummarize: Callable[[], object],
) -> bool:
    from callrec.audio.finalize import find_audio, write_clip

    overview = speaker_overview(rec)
    win = tk.Toplevel(root)
    win.title(f"Sprecher zuordnen – {rec.title()}")
    if not overview:
        ttk.Label(win, text="In dieser Aufnahme wurden keine Sprecher der Gegenseite erkannt.").pack(padx=12, pady=12)
        ttk.Button(win, text="Schließen", command=win.destroy).pack(pady=8)
        _modal(win)
        root.wait_window(win)
        return False

    choices = name_choices(rec, profiles)
    audio = find_audio(rec.folder)
    tmpdir = Path(tempfile.mkdtemp(prefix="callrec_"))
    ttk.Label(
        win,
        text="Ordnen Sie jede Stimme einer Person zu. Gespeicherte Stimmprofile werden bei künftigen Gesprächen "
        "automatisch erkannt. Stimmprofile sind biometrische Daten und bleiben ausschließlich auf diesem Rechner.",
        wraplength=640,
    ).grid(row=0, column=0, columnspan=5, sticky="w", **PAD)
    for col, head in enumerate(["Sprecher", "Beispiel", "", "Name", "Stimme merken"]):
        ttk.Label(win, text=head, font=("Segoe UI", 9, "bold")).grid(row=1, column=col, sticky="w", **PAD)

    rows: list[tuple[str, tk.StringVar, tk.BooleanVar]] = []
    for i, ov in enumerate(overview, start=2):
        info = f"{ov.label}\n{fmt_ts(ov.talk_time_s)} Redezeit, {ov.utterances} Beiträge"
        if ov.source == "auto" and ov.score is not None:
            info += f"\nerkannt ({ov.score:.2f})"
        ttk.Label(win, text=info).grid(row=i, column=0, sticky="nw", **PAD)
        ttk.Label(win, text=f"„{ov.sample_text}“" if ov.sample_text else "-", wraplength=300, foreground="#333").grid(
            row=i, column=1, sticky="nw", **PAD
        )

        def play(ov: Any = ov) -> None:
            if audio is None or ov.sample_span is None:
                return
            clip = tmpdir / f"{ov.id}.wav"
            try:
                write_clip(audio.remote, ov.sample_span[0], ov.sample_span[1], clip)
                _play(clip)
            except Exception as exc:
                messagebox.showerror("Wiedergabe", str(exc), parent=win)

        btn = ttk.Button(win, text="▶ Anhören", command=play)
        if audio is None or ov.sample_span is None:
            btn.state(["disabled"])
        btn.grid(row=i, column=2, sticky="nw", **PAD)
        name_var = tk.StringVar(value=ov.name)
        ttk.Combobox(win, textvariable=name_var, values=choices, width=28).grid(row=i, column=3, sticky="nw", **PAD)
        keep_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(win, variable=keep_var).grid(row=i, column=4, sticky="n", **PAD)
        rows.append((ov.id, name_var, keep_var))

    resum_var = tk.BooleanVar(value=True)
    bottom = ttk.Frame(win)
    bottom.grid(row=len(overview) + 2, column=0, columnspan=5, sticky="we", **PAD)
    ttk.Checkbutton(bottom, text="Zusammenfassung danach neu erstellen", variable=resum_var).pack(side="left")
    applied = {"value": False}

    def apply() -> None:
        _stop_playback()
        assignments = [Assignment(sid, nv.get(), bool(kv.get())) for sid, nv, kv in rows]
        try:
            msgs = apply_assignments(rec, assignments, profiles, rerender)
        except Exception as exc:
            log.exception("Zuordnung fehlgeschlagen")
            messagebox.showerror("Fehler", str(exc), parent=win)
            return
        applied["value"] = True
        if resum_var.get():
            resummarize()
        if msgs:
            messagebox.showinfo("Gespeichert", "\n".join(msgs), parent=win)
        win.destroy()

    ttk.Button(bottom, text="Übernehmen", command=apply).pack(side="right", padx=4)
    ttk.Button(bottom, text="Abbrechen", command=win.destroy).pack(side="right", padx=4)
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    _modal(win)
    root.wait_window(win)
    _stop_playback()
    return applied["value"]


# ---------------------------------------------------------------------------
# Voice profiles
# ---------------------------------------------------------------------------
def manage_profiles(root: tk.Tk, profiles: ProfileStore) -> None:
    win = tk.Toplevel(root)
    win.title("Stimmprofile (biometrische Daten)")
    ttk.Label(
        win,
        text="Stimmprofile werden nur lokal und verschlüsselt (Windows-DPAPI) gespeichert. "
        "Löschen Sie Profile, wenn eine Person das verlangt oder sie nicht mehr benötigt werden.",
        wraplength=560,
    ).pack(anchor="w", **PAD)
    tree = ttk.Treeview(win, columns=("name", "email", "samples", "model", "updated"), show="headings", height=10)
    for col, head, w in (
        ("name", "Name", 160), ("email", "E-Mail", 180), ("samples", "Proben", 60),
        ("model", "Modell", 140), ("updated", "Aktualisiert", 150),
    ):
        tree.heading(col, text=head)
        tree.column(col, width=w, anchor="w")
    tree.pack(fill="both", expand=True, **PAD)

    def refresh() -> None:
        tree.delete(*tree.get_children())
        for p in profiles.all_profiles():
            tree.insert("", "end", iid=p.id, values=(p.name, p.email, p.samples, p.model_id, p.updated_at))

    def delete_selected() -> None:
        sel = tree.selection()
        if not sel:
            return
        names = ", ".join(str(tree.item(i, "values")[0]) for i in sel)
        if messagebox.askyesno("Löschen", f"Stimmprofil(e) von {names} endgültig löschen?", parent=win):
            for i in sel:
                profiles.delete(i)
            refresh()

    def delete_all() -> None:
        if messagebox.askyesno("Alle löschen", "ALLE Stimmprofile endgültig löschen?", parent=win):
            profiles.delete_all()
            refresh()

    bar = ttk.Frame(win)
    bar.pack(fill="x", **PAD)
    ttk.Button(bar, text="Ausgewählte löschen", command=delete_selected).pack(side="left", padx=4)
    ttk.Button(bar, text="Alle löschen", command=delete_all).pack(side="left", padx=4)
    ttk.Button(bar, text="Schließen", command=win.destroy).pack(side="right", padx=4)
    refresh()
    _modal(win)
    root.wait_window(win)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
WHISPER_MODELS = ["large-v3", "large-v3-turbo", "medium", "small", "base"]


def edit_settings(root: tk.Tk, cfg: Any, mic_names: list[str], render_names: list[str], save: Callable[[Any], None]) -> bool:
    from callrec.hotkey import HotkeyError, parse_hotkey

    win = tk.Toplevel(root)
    win.title("Einstellungen")
    saved = {"value": False}
    auto_mic = "auto (Windows-Standard für Kommunikation)"
    auto_out = "auto (Windows-Standard für Kommunikation)"
    fields: list[tuple[str, tk.StringVar]] = []

    def row(label: str, var: tk.StringVar, values: list[str] | None = None, r: list[int] = [0]) -> None:  # noqa: B006
        ttk.Label(win, text=label).grid(row=r[0], column=0, sticky="w", **PAD)
        if values is not None:
            ttk.Combobox(win, textvariable=var, values=values, width=55).grid(row=r[0], column=1, sticky="we", **PAD)
        else:
            ttk.Entry(win, textvariable=var, width=58).grid(row=r[0], column=1, sticky="we", **PAD)
        fields.append((label, var))
        r[0] += 1

    mic_var = tk.StringVar(value=auto_mic if cfg.audio.mic_device == "auto" else cfg.audio.mic_device)
    out_var = tk.StringVar(value=auto_out if cfg.audio.loopback_device == "auto" else cfg.audio.loopback_device)
    name_var = tk.StringVar(value=cfg.general.user_name)
    dir_var = tk.StringVar(value=cfg.general.recordings_dir)
    lang_var = tk.StringVar(value=cfg.transcription.language)
    model_var = tk.StringVar(value=cfg.transcription.model)
    llm_var = tk.StringVar(value=cfg.summary.model)
    hk_var = tk.StringVar(value=cfg.hotkey.toggle)
    row("Mikrofon:", mic_var, [auto_mic, *mic_names])
    row("Ausgabegerät (Gegenseite):", out_var, [auto_out, *render_names])
    row("Ihr Name (leer = aus Microsoft-Konto):", name_var)
    row("Aufnahmeordner (leer = Standard):", dir_var)
    row("Sprache (de / auto / en …):", lang_var, ["de", "auto", "en"])
    row("Whisper-Modell:", model_var, WHISPER_MODELS)
    row("Ollama-Modell:", llm_var)
    row("Tastenkürzel Start/Stopp:", hk_var)

    def browse() -> None:
        d = filedialog.askdirectory(parent=win, title="Aufnahmeordner wählen")
        if d:
            dir_var.set(d)

    ttk.Button(win, text="Ordner wählen …", command=browse).grid(row=3, column=2, **PAD)

    def do_save() -> None:
        try:
            parse_hotkey(hk_var.get())
        except HotkeyError as exc:
            messagebox.showerror("Tastenkürzel", str(exc), parent=win)
            return
        cfg.audio.mic_device = "auto" if mic_var.get() == auto_mic else mic_var.get().strip()
        cfg.audio.loopback_device = "auto" if out_var.get() == auto_out else out_var.get().strip()
        cfg.general.user_name = name_var.get().strip()
        cfg.general.recordings_dir = dir_var.get().strip()
        cfg.transcription.language = lang_var.get().strip() or "de"
        cfg.transcription.model = model_var.get().strip() or "large-v3"
        cfg.summary.model = llm_var.get().strip() or cfg.summary.model
        cfg.hotkey.toggle = hk_var.get().strip()
        save(cfg)
        saved["value"] = True
        messagebox.showinfo(
            "Gespeichert", "Einstellungen gespeichert. Tastenkürzel und Geräte gelten ab der nächsten Aufnahme "
            "bzw. nach einem Neustart des Programms.", parent=win,
        )
        win.destroy()

    bar = ttk.Frame(win)
    bar.grid(row=20, column=0, columnspan=3, sticky="e", **PAD)
    ttk.Button(bar, text="Speichern", command=do_save).pack(side="right", padx=4)
    ttk.Button(bar, text="Abbrechen", command=win.destroy).pack(side="right", padx=4)
    win.columnconfigure(1, weight=1)
    _modal(win)
    root.wait_window(win)
    return saved["value"]
