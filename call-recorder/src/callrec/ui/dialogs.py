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
from callrec.consent import (
    ConsentError,
    confirm_consent,
    consent_statement,
    later_confirmation,
    record_later_confirmation,
)
from callrec.controller import StartDecision
from callrec.detect import Prompt
from callrec.models import Person
from callrec.people import format_participants, parse_participants_text
from callrec.recording import Recording
from callrec.review import (
    SELF_CHOICE,
    SOURCE_TEXT,
    Assignment,
    apply_assignments,
    row_choices,
    self_overview,
    speaker_overview,
)
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
def ask_start(root: tk.Tk, suggestion: Suggestion, me: Person, calendar: CalendarService,
              summary_provider: str = "") -> StartDecision | None:
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
    ttk.Label(frame, text=consent_statement(summary_provider), wraplength=520).pack(anchor="w", padx=8, pady=4)
    cloud = summary_provider == "anthropic"
    if cloud:
        ttk.Label(frame, text="Achtung: Die Zusammenfassung erstellt Claude (Anthropic). Das Transkript verlässt "
                  "dafür diesen Rechner.", wraplength=520, foreground="#b00020").pack(anchor="w", padx=8, pady=2)
    consent_var = tk.BooleanVar(value=False)  # never pre-checked
    start_btn: ttk.Button

    def on_check() -> None:
        start_btn.state(["!disabled"] if consent_var.get() else ["disabled"])

    ttk.Checkbutton(
        frame, text="Ja, alle Teilnehmenden haben der Aufnahme und der Übertragung an Anthropic zugestimmt." if cloud
        else "Ja, alle Teilnehmenden haben der Aufnahme zugestimmt.", variable=consent_var, command=on_check
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
                summary_provider=summary_provider,
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
    handoff: Callable[[], object] | None = None,
    in_chain: bool = False,
    speaker_cfg: Any = None,
    allow_more: Callable[[], object] | None = None,
) -> bool:
    """``in_chain``: part of the guided processing - "Überspringen" instead of "Abbrechen", and
    no re-summary checkbox (the chain creates the summary next, with the names).
    ``speaker_cfg``: the [speakers] settings (match scope, minimum speech for learning)."""
    from callrec.audio.finalize import find_audio, write_clip
    from callrec.config import SpeakerConfig

    sc = speaker_cfg or SpeakerConfig()
    overview = speaker_overview(rec)
    me_row = self_overview(rec)
    win = tk.Toplevel(root)
    win.title(f"Sprecher zuordnen – {rec.title()}")
    audio = find_audio(rec.folder)
    tmpdir = Path(tempfile.mkdtemp(prefix="callrec_"))

    def player(track: str, ov: Any) -> Callable[[], None]:
        def play() -> None:
            if audio is None or ov.sample_span is None:
                return
            clip = tmpdir / f"{ov.id}.wav"
            try:
                write_clip(getattr(audio, track), ov.sample_span[0], ov.sample_span[1], clip)
                _play(clip)
            except Exception as exc:
                messagebox.showerror("Wiedergabe", str(exc), parent=win)
        return play

    ttk.Label(
        win,
        text="Ordnen Sie jede Stimme der Gegenseite einer Person zu. Angeboten werden die Teilnehmenden dieses "
        "Termins; Sie können auch einen Namen eintippen (die Person wird dann als Teilnehmende ergänzt). Ihre eigene "
        "Stimme steht nicht zur Auswahl: Die Mikrofonspur ist immer Ihnen zugeordnet (erste Zeile). Gespeicherte "
        "Stimmprofile sind biometrische Daten und bleiben ausschließlich auf diesem Rechner.",
        wraplength=720,
    ).grid(row=0, column=0, columnspan=5, sticky="w", **PAD)
    warnings = list((((rec.load_meta().get("processing") or {}).get("identification") or {}).get("trackcheck")
                     or {}).get("warnings") or [])
    row = 1
    for w in warnings:
        ttk.Label(win, text="Mikrofon prüfen: " + w, wraplength=720, foreground="#b00020").grid(
            row=row, column=0, columnspan=5, sticky="w", **PAD)
        row += 1
    for col, head in enumerate(["Sprecher", "Beispiel", "", "Name", "Stimme merken"]):
        ttk.Label(win, text=head, font=("Segoe UI", 9, "bold")).grid(row=row, column=col, sticky="w", **PAD)
    row += 1
    # fixed row: your own track
    ttk.Label(win, text=f"Ich – {me_row.name} (Mikrofonspur)\n{me_row.utterances} Beiträge, "
              f"{fmt_ts(me_row.talk_time_s, 'mm:ss')} Sprechzeit").grid(row=row, column=0, sticky="nw", **PAD)
    ttk.Label(win, text=f"„{me_row.sample_text}“" if me_row.sample_text else "-", wraplength=300,
              foreground="#333").grid(row=row, column=1, sticky="nw", **PAD)
    me_btn = ttk.Button(win, text="▶ Anhören", command=player("mic", me_row))
    if audio is None or me_row.sample_span is None:
        me_btn.state(["disabled"])
    me_btn.grid(row=row, column=2, sticky="nw", **PAD)
    ttk.Label(win, text="immer Sie – Name unter Einstellungen → Darstellung", foreground="#555").grid(
        row=row, column=3, columnspan=2, sticky="nw", **PAD)
    row += 1
    if not overview:
        ttk.Label(win, text="In dieser Aufnahme wurden keine Sprecher der Gegenseite erkannt.").grid(
            row=row, column=0, columnspan=5, sticky="w", **PAD)
        ttk.Button(win, text="Schließen", command=win.destroy).grid(row=row + 1, column=4, sticky="e", **PAD)
        _modal(win)
        root.wait_window(win)
        _stop_playback()
        return False

    all_profiles = getattr(sc, "match_scope", "participants") == "all_profiles"
    rows: list[tuple[str, tk.StringVar, tk.BooleanVar]] = []
    for i, ov in enumerate(overview, start=row):
        info = f"{ov.label}\n{fmt_ts(ov.talk_time_s, 'mm:ss')} Sprechzeit, {ov.utterances} Beiträge"
        if ov.source == "auto" and ov.score is not None:
            info += f"\nautomatisch erkannt ({ov.score:.2f})"
        elif ov.source == "self_echo":
            info += "\nals Ihre Stimme markiert"
        ttk.Label(win, text=info).grid(row=i, column=0, sticky="nw", **PAD)
        sample = f"„{ov.sample_text}“" if ov.sample_text else "-"
        if ov.note and not ov.name:
            sample += f"\n({ov.note})"
        ttk.Label(win, text=sample, wraplength=300, foreground="#333").grid(row=i, column=1, sticky="nw", **PAD)
        btn = ttk.Button(win, text="▶ Anhören", command=player("remote", ov))
        if audio is None or ov.sample_span is None:
            btn.state(["disabled"])
        btn.grid(row=i, column=2, sticky="nw", **PAD)
        initial = SELF_CHOICE if ov.source == "self_echo" else (ov.name or ov.preselect_name)
        name_var = tk.StringVar(value=initial)
        ttk.Combobox(win, textvariable=name_var, values=row_choices(rec, profiles, ov, all_profiles),
                     width=44).grid(row=i, column=3, sticky="nw", **PAD)
        # an automatic match is only learned when you confirm it here
        keep_var = tk.BooleanVar(value=ov.source != "auto")
        ttk.Checkbutton(win, variable=keep_var).grid(row=i, column=4, sticky="n", **PAD)
        rows.append((ov.id, name_var, keep_var))
    last = row + len(overview)

    resum_var = tk.BooleanVar(value=not in_chain)
    bottom = ttk.Frame(win)
    bottom.grid(row=last, column=0, columnspan=5, sticky="we", **PAD)
    if in_chain:
        ttk.Label(bottom, text="Danach wird die Zusammenfassung mit diesen Namen erstellt.", foreground="#555").pack(
            side="left")
    else:
        ttk.Checkbutton(bottom, text="Zusammenfassung danach neu erstellen", variable=resum_var).pack(side="left")
    applied = {"value": False}

    def apply() -> None:
        _stop_playback()
        assignments = [Assignment(sid, nv.get(), bool(kv.get())) for sid, nv, kv in rows]
        try:
            msgs = apply_assignments(rec, assignments, profiles, rerender,
                                     min_learn_speech_s=getattr(sc, "min_learn_speech_s", None))
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
    bound = ((rec.load_meta().get("processing") or {}).get("diarization") or {}).get("bound") or {}
    if allow_more is not None and not in_chain and bound.get("merged"):
        def more() -> None:
            _stop_playback()
            allow_more()
            win.destroy()

        ttk.Label(win, text=f"Die Sprecher der Gegenseite wurden auf die {bound.get('participants')} eingeladene(n) "
                  f"Person(en) begrenzt (zuvor {bound.get('raw_speakers')} getrennte Stimmen). Waren nicht eingeladene "
                  "Personen dabei, „Weitere Sprecher zulassen“.", wraplength=720, foreground="#555").grid(
            row=last + 1, column=0, columnspan=5, sticky="w", **PAD)
        ttk.Button(bottom, text="Weitere Sprecher zulassen", command=more).pack(side="left", padx=8)
    if handoff is not None and not in_chain:
        ttk.Button(bottom, text="Zusammenfassung an SharePoint übergeben \u2026", command=handoff).pack(side="left", padx=8)
    ttk.Button(bottom, text="Überspringen" if in_chain else "Abbrechen", command=win.destroy).pack(side="right", padx=4)
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    _modal(win)
    root.wait_window(win)
    _stop_playback()
    return applied["value"]


# ---------------------------------------------------------------------------
# Participants of a recording / re-check of the assignment
# ---------------------------------------------------------------------------
def edit_participants(root: tk.Misc, rec: Recording) -> list[Person] | None:
    """The participants of this recording (for the speaker identification); None = cancelled."""
    win = tk.Toplevel(root)
    win.title(f"Teilnehmende – {rec.title()}")
    result: dict[str, list[Person] | None] = {"v": None}
    ttk.Label(win, text="Eine Person pro Zeile, optional mit E-Mail: Name <E-Mail>. Stimmen werden automatisch nur "
              "Personen aus dieser Liste zugeordnet; manuelle Zuordnungen bleiben erhalten. Die bei der Aufnahme "
              "bestätigte Einwilligung bleibt unverändert.", wraplength=480).pack(anchor="w", **PAD)
    txt = tk.Text(win, width=60, height=10, wrap="none")
    txt.insert("1.0", format_participants(rec.participants()))
    txt.pack(fill="both", expand=True, **PAD)
    bar = ttk.Frame(win)
    bar.pack(fill="x", **PAD)

    def ok() -> None:
        result["v"] = parse_participants_text(txt.get("1.0", "end"))
        win.destroy()

    ttk.Button(bar, text="Speichern und Sprecher neu erkennen", command=ok).pack(side="right", padx=4)
    ttk.Button(bar, text="Abbrechen", command=win.destroy).pack(side="right", padx=4)
    win.bind("<Escape>", lambda e: win.destroy())
    _modal(win)
    txt.focus_set()
    root.wait_window(win)
    return result["v"]


def confirm_reset_foreign(root: tk.Misc, rec: Recording, foreign: list[tuple[str, str, str]]) -> bool:
    lines = "\n".join(f"• {label}: {name}" for _sid, label, name in foreign)
    return bool(messagebox.askyesno(
        "Frühere Zuordnungen prüfen",
        f"In „{rec.title()}“ sind Stimmen Personen zugeordnet, die keine Teilnehmenden dieses Termins sind:\n\n{lines}\n\n"
        "Frühere Versionen haben automatisch erkannte Namen beim Klick auf „Übernehmen“ als bestätigt gespeichert – "
        "diese Zuordnungen sind daher womöglich nie geprüft worden. Zurücksetzen und neu erkennen? Die Stimmprofile "
        "dieser Personen werden dann aus Ihren übrigen Zuordnungen neu aufgebaut.", icon="warning", parent=root))


def confirm_recheck(root: tk.Misc, rec: Recording, changes: list[tuple[str, str, str]]) -> bool:
    """Preview of "Sprecherzuordnung neu prüfen" (automatic assignments only)."""
    if not changes:
        messagebox.showinfo("Sprecherzuordnung neu prüfen", "Nach der neuen Regel (nur Teilnehmende dieses Termins) "
                            "ändert sich nichts. Manuelle Zuordnungen werden nie geändert.", parent=root)
        return False
    lines = "\n".join(f"• {label}: {before} → {after}" for label, before, after in changes)
    return bool(messagebox.askyesno(
        "Sprecherzuordnung neu prüfen",
        f"Automatische Zuordnungen in „{rec.title()}“ nach der neuen Regel (nur Teilnehmende dieses Termins):\n\n"
        f"{lines}\n\nManuelle Zuordnungen bleiben unverändert. Übernehmen? Das Transkript wird danach neu erstellt "
        "(die bisherige Zuordnung wird unter data\\archive gesichert).", parent=root))


# ---------------------------------------------------------------------------
# Voice profiles
# ---------------------------------------------------------------------------
def manage_profiles(root: tk.Tk, profiles: ProfileStore, rebuild: Callable[[str], str] | None = None) -> None:
    """``rebuild(profile_id) -> message``: rebuild a profile from your assignments in the recordings."""
    win = tk.Toplevel(root)
    win.title("Stimmprofile (biometrische Daten)")
    ttk.Label(
        win,
        text="Stimmprofile werden nur lokal und verschlüsselt (Windows-DPAPI) gespeichert. "
        "Löschen Sie Profile, wenn eine Person das verlangt oder sie nicht mehr benötigt werden. Unten steht, "
        "was ein Profil aus welcher Aufnahme gelernt hat; falsche Einträge lassen sich einzeln entfernen.",
        wraplength=640,
    ).pack(anchor="w", **PAD)
    tree = ttk.Treeview(win, columns=("name", "email", "samples", "model", "updated"), show="headings", height=8)
    for col, head, w in (
        ("name", "Name", 160), ("email", "E-Mail", 180), ("samples", "Proben", 60),
        ("model", "Modell", 140), ("updated", "Aktualisiert", 150),
    ):
        tree.heading(col, text=head)
        tree.column(col, width=w, anchor="w")
    tree.pack(fill="both", expand=True, **PAD)
    ttk.Label(win, text="Gelernt aus:").pack(anchor="w", padx=8)
    origins = ttk.Treeview(win, columns=("rec", "spk", "source", "secs", "at"), show="headings", height=6)
    for col, head, w in (("rec", "Aufnahme", 260), ("spk", "Sprecher", 70), ("source", "Herkunft", 200),
                         ("secs", "Sprechzeit", 80), ("at", "Datum", 150)):
        origins.heading(col, text=head)
        origins.column(col, width=w, anchor="w")
    origins.pack(fill="both", expand=True, **PAD)

    def refresh() -> None:
        tree.delete(*tree.get_children())
        for p in profiles.all_profiles():
            tree.insert("", "end", iid=p.id, values=(p.name, p.email, p.samples, p.model_id, p.updated_at))
        show_origins()

    def show_origins(*_a: Any) -> None:
        origins.delete(*origins.get_children())
        sel = tree.selection()
        p = profiles.get(sel[0]) if sel else None
        if p is None:
            return
        for i, c in enumerate(p.contributions):
            rec_text = c.title or c.folder or ("–" if not c.recording else c.recording[:8])
            if c.folder and c.title:
                rec_text = f"{c.title} ({c.folder})"
            origins.insert("", "end", iid=str(i), values=(rec_text, c.speaker if c.recording else "–",
                                                          SOURCE_TEXT.get(c.source, c.source),
                                                          f"{c.seconds:.0f} s" if c.seconds else "–", c.at))

    tree.bind("<<TreeviewSelect>>", show_origins)

    def selected_profile() -> str:
        sel = tree.selection()
        return sel[0] if sel else ""

    def remove_origin() -> None:
        pid, sel = selected_profile(), origins.selection()
        if not pid or not sel:
            return
        if messagebox.askyesno("Eintrag entfernen", "Diesen gelernten Eintrag aus dem Profil entfernen?", parent=win):
            for idx in sorted((int(s) for s in sel), reverse=True):
                profiles.remove_contribution(pid, idx)
            refresh()
            if profiles.get(pid) is not None:
                tree.selection_set(pid)

    def reset_confirmed() -> None:
        pid = selected_profile()
        if pid and messagebox.askyesno("Zurücksetzen", "Nur behalten, was Sie selbst zugeordnet oder bestätigt haben? "
                                       "Automatisch Gelerntes und Einträge früherer Versionen werden entfernt.",
                                       parent=win):
            if profiles.reset_to_confirmed(pid) is None:
                messagebox.showinfo("Zurücksetzen", "Das Profil enthielt nichts Bestätigtes und wurde gelöscht.",
                                    parent=win)
            refresh()

    def do_rebuild() -> None:
        pid = selected_profile()
        if pid and rebuild is not None:
            messagebox.showinfo("Neu aufbauen", rebuild(pid), parent=win)
            refresh()

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
    ttk.Button(bar, text="Eintrag entfernen", command=remove_origin).pack(side="left", padx=4)
    ttk.Button(bar, text="Nur Bestätigtes behalten", command=reset_confirmed).pack(side="left", padx=4)
    if rebuild is not None:
        ttk.Button(bar, text="Aus Ihren Zuordnungen neu aufbauen", command=do_rebuild).pack(side="left", padx=4)
    bar2 = ttk.Frame(win)
    bar2.pack(fill="x", **PAD)
    ttk.Button(bar2, text="Ausgewählte löschen", command=delete_selected).pack(side="left", padx=4)
    ttk.Button(bar2, text="Alle löschen", command=delete_all).pack(side="left", padx=4)
    ttk.Button(bar2, text="Schließen", command=win.destroy).pack(side="right", padx=4)
    refresh()
    _modal(win)
    root.wait_window(win)


# ---------------------------------------------------------------------------
# SharePoint hand-over (summary only)
# ---------------------------------------------------------------------------


def ask_handoff(root: tk.Misc, rec: Recording, cfg: Any, perform: Callable[[str], dict[str, Any]]) -> bool:
    """Preview + explicit confirmation; ``perform(sha256_of_preview)`` does the actual hand-over
    (may open a browser) and refuses if the document no longer matches the preview."""
    import threading

    from callrec.handoff import HANDOFF_WARNING, HandoffError, build_summary_document, last_handoff

    try:
        doc = build_summary_document(rec, cfg.handoff.include_participant_emails)
    except HandoffError as exc:
        messagebox.showinfo("Keine Übergabe möglich", str(exc), parent=root)
        return False
    win = tk.Toplevel(root)
    win.title(f"Zusammenfassung an SharePoint übergeben \u2013 {rec.title()}")
    ttk.Label(win, text=HANDOFF_WARNING, wraplength=640, foreground="#b00020").pack(anchor="w", **PAD)
    target = cfg.handoff.folder if cfg.handoff.mode == "folder" else f"Graph-Upload: {cfg.handoff.graph_folder or '/'}"
    ttk.Label(win, text=f"Ziel: {target}\nDatei: {doc.filename}", wraplength=640).pack(anchor="w", **PAD)
    prev = last_handoff(rec)
    if prev:
        same = prev.get("sha256") == doc.sha256
        ttk.Label(win, text=f"Zuletzt übergeben am {prev.get('at', '?')}"
                  + (" (identischer Inhalt)." if same else " - die Zusammenfassung hat sich seitdem geändert."),
                  foreground="#555").pack(anchor="w", **PAD)
    preview = tk.Text(win, width=90, height=22, wrap="word")
    preview.insert("1.0", doc.markdown)
    preview.configure(state="disabled")
    preview.pack(fill="both", expand=True, **PAD)
    status = tk.StringVar(value="")
    ttk.Label(win, textvariable=status).pack(anchor="w", **PAD)
    done = {"v": False}
    bar = ttk.Frame(win)
    bar.pack(fill="x", **PAD)

    def go() -> None:
        btn.state(["disabled"])
        status.set("Übergabe läuft …")
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["entry"] = perform(doc.sha256)
            except Exception as exc:  # shown to the user
                box["error"] = exc

        th = threading.Thread(target=work, name="handoff", daemon=True)
        th.start()

        def poll() -> None:
            if th.is_alive():
                win.after(200, poll)
                return
            if "error" in box:
                btn.state(["!disabled"])
                status.set("")
                messagebox.showerror("Übergabe fehlgeschlagen", str(box["error"]), parent=win)
                return
            done["v"] = True
            messagebox.showinfo("Übergeben", f"Gespeichert unter:\n{box['entry']['location']}", parent=win)
            win.destroy()

        poll()

    btn = ttk.Button(bar, text="An SharePoint übergeben", command=go)
    btn.pack(side="right", padx=4)
    ttk.Button(bar, text="Abbrechen", command=win.destroy).pack(side="right", padx=4)
    _modal(win)
    root.wait_window(win)
    return done["v"]


def confirm_cloud_summary(root: tk.Misc, rec: Recording, me: Person) -> bool:
    """Ask before an OLDER recording (consent without the Anthropic notice) is sent to Anthropic."""
    from callrec.consent import LATER_STATEMENT_ANTHROPIC

    ok = messagebox.askyesno(
        "Übertragung an Anthropic bestätigen",
        f"Die Einwilligung für „{rec.title()}“ wurde ohne Hinweis auf Anthropic erteilt.\n\n"
        f"{LATER_STATEMENT_ANTHROPIC}\n\nNur mit „Ja“ wird das Transkript an Anthropic gesendet.",
        icon="warning", parent=root,
    )
    if not ok:
        return False
    record_later_confirmation(rec, later_confirmation(me.display or "Benutzer", "dialog", True))
    return True


# ---------------------------------------------------------------------------
# Re-transcription options (main window)
# ---------------------------------------------------------------------------
RETRANSCRIBE_LANGUAGES = (("auto", "Automatisch erkennen"), ("de", "Deutsch"), ("en", "Englisch"))
CURRENT_SETTINGS = "(aktuelle Einstellungen)"


def ask_retranscribe(root: tk.Misc, rec: Recording, presets: list[str]) -> tuple[str, str | None] | None:
    """Language ("auto"/"de"/"en") and optional preset for a new transcript version, or None."""
    win = tk.Toplevel(root)
    win.title(f"Neu transkribieren – {rec.title()}")
    win.resizable(False, False)
    result: dict[str, tuple[str, str | None] | None] = {"v": None}
    ttk.Label(win, text="Es entsteht eine neue Transkript-Version; die bisherige bleibt erhalten. Danach wird auch die "
              "Zusammenfassung neu erstellt. Manuelle Sprecherzuordnungen bleiben gültig.",
              wraplength=440).grid(row=0, column=0, columnspan=2, sticky="w", **PAD)
    lang = tk.StringVar(value="auto")
    box = ttk.LabelFrame(win, text="Sprache des Gesprächs")
    box.grid(row=1, column=0, columnspan=2, sticky="we", padx=8, pady=4)
    first: ttk.Radiobutton | None = None
    for code, label in RETRANSCRIBE_LANGUAGES:
        rb = ttk.Radiobutton(box, text=label, value=code, variable=lang)
        rb.pack(anchor="w", padx=8, pady=2)
        first = first or rb
    ttk.Label(win, text="Preset (optional):").grid(row=2, column=0, sticky="w", **PAD)
    preset = tk.StringVar(value=CURRENT_SETTINGS)
    ttk.Combobox(win, textvariable=preset, values=[CURRENT_SETTINGS, *presets], state="readonly",
                 width=30).grid(row=2, column=1, sticky="we", **PAD)
    bar = ttk.Frame(win)
    bar.grid(row=3, column=0, columnspan=2, sticky="e", **PAD)

    def ok() -> None:
        p = preset.get()
        result["v"] = (lang.get(), None if p == CURRENT_SETTINGS else p)
        win.destroy()

    ttk.Button(bar, text="Neu transkribieren", command=ok).pack(side="right", padx=4)
    ttk.Button(bar, text="Abbrechen", command=win.destroy).pack(side="right", padx=4)
    win.bind("<Return>", lambda e: ok())
    win.bind("<Escape>", lambda e: win.destroy())
    _modal(win)
    if first is not None:
        first.focus_set()
    root.wait_window(win)
    return result["v"]


# ---------------------------------------------------------------------------
# Assign a calendar event to an existing recording
# ---------------------------------------------------------------------------
def ask_event_for_recording(root: tk.Misc, rec: Recording, events: list[Any]) -> tuple[Any, bool] | None:
    """(chosen event, take its subject as title) or None. ``events`` best first."""
    win = tk.Toplevel(root)
    win.title(f"Termin zuordnen – {rec.title()}")
    result: dict[str, tuple[Any, bool] | None] = {"v": None}
    from callrec.calendar.selection import demotion

    start = rec.started_at()
    when = f" am {start:%d.%m.%Y} ({start:%H:%M} Uhr)" if start else ""
    ttk.Label(win, text=f"Alle Termine des Aufnahmetags{when}, der am besten zur Aufnahmezeit passende zuerst; "
              "ganztägige Einträge und Blocker stehen am Ende. Nur die Teilnehmenden des gewählten Termins werden "
              "bei der Sprechererkennung automatisch zugeordnet.",
              wraplength=560).grid(row=0, column=0, columnspan=2, sticky="w", **PAD)
    choice = tk.IntVar(value=0)
    box = ttk.Frame(win)
    box.grid(row=1, column=0, columnspan=2, sticky="we", **PAD)
    for i, ev in enumerate(events):
        parts = ev.participants()
        people = ", ".join(p.display for p in parts[:5]) + (f" und {len(parts) - 5} weitere" if len(parts) > 5 else "")
        org = ev.organizer.display if ev.organizer else "–"
        why = demotion(ev)
        times = "ganztägig" if ev.is_all_day else f"{ev.start.astimezone():%d.%m. %H:%M}–{ev.end.astimezone():%H:%M}"
        text = (f"{times}  {ev.subject}" + (" – Teams" if ev.is_online else "") + (f"  [{why}]" if why else "")
                + f"\n    Organisation: {org}; {len(parts)} Teilnehmende" + (f": {people}" if people else ""))
        ttk.Radiobutton(box, text=text, value=i, variable=choice).pack(anchor="w", pady=2)
    take = tk.BooleanVar(value=True)
    ttk.Checkbutton(win, text="Betreff als Titel übernehmen", variable=take).grid(row=2, column=0, sticky="w", **PAD)
    bar = ttk.Frame(win)
    bar.grid(row=3, column=0, columnspan=2, sticky="e", **PAD)

    def ok() -> None:
        result["v"] = (events[choice.get()], bool(take.get()))
        win.destroy()

    ttk.Button(bar, text="Zuordnen", command=ok).pack(side="right", padx=4)
    ttk.Button(bar, text="Abbrechen", command=win.destroy).pack(side="right", padx=4)
    win.bind("<Escape>", lambda e: win.destroy())
    _modal(win)
    root.wait_window(win)
    return result["v"]


# ---------------------------------------------------------------------------
# After a recording: process fully / later / discard
# ---------------------------------------------------------------------------
AFTER_FULL, AFTER_LATER, AFTER_DISCARD = "full", "later", "discard"


def ask_after_recording(root: tk.Misc, rec: Recording, plan: Any, handoff_problem: str = "",
                        summary_problem: str = "") -> tuple[str, Any]:
    """("full", Plan) | ("later", None) | ("discard", None). Closing the window = "Später".

    ``handoff_problem``: why a hand-over could not run now (no SharePoint folder ...) - the box is
    then shown but the chain will skip it with that message."""
    from callrec.postcall import Plan

    win = tk.Toplevel(root)
    win.title("Aufnahme beendet")
    win.resizable(False, False)
    result: dict[str, tuple[str, Any]] = {"v": (AFTER_LATER, None)}
    try:
        title = rec.title()
    except (OSError, ValueError):
        title = rec.folder.name
    ttk.Label(win, text="Aufnahme beendet – jetzt vollständig verarbeiten?", font=("Segoe UI", 12, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w", **PAD)
    ttk.Label(win, text=title, foreground="#444").grid(row=1, column=0, columnspan=2, sticky="w", padx=8)
    box = ttk.LabelFrame(win, text="Was passiert (nacheinander, im Hintergrund)")
    box.grid(row=2, column=0, columnspan=2, sticky="we", padx=8, pady=8)
    v_tr = tk.BooleanVar(value=plan.transcript)
    v_sp = tk.BooleanVar(value=plan.speakers)
    v_su = tk.BooleanVar(value=plan.summary)
    v_ho = tk.BooleanVar(value=plan.handoff)
    rows = [
        (v_tr, "Transkript (lokal mit Whisper)"),
        (v_sp, "Sprecherzuordnung – erkennt Stimmen; sind Stimmen offen, öffnet sich der Zuordnungsdialog"),
        (v_su, "Zusammenfassung" + (f" ({summary_problem})" if summary_problem else "")),
        (v_ho, "Übergabe der Zusammenfassung an SharePoint (für den Claude-Assistenten) – nur die "
               "Zusammenfassung, ohne weitere Vorschau"),
    ]
    checks = []
    for i, (var, text) in enumerate(rows):
        cb = ttk.Checkbutton(box, text=text, variable=var)
        cb.grid(row=i, column=0, sticky="w", padx=8, pady=2)
        checks.append(cb)
    if handoff_problem:
        ttk.Label(box, text=f"Hinweis zur Übergabe: {handoff_problem}", foreground="#b00020", wraplength=480).grid(
            row=len(rows), column=0, sticky="w", padx=24, pady=(0, 4))

    def sync(*_a: Any) -> None:
        for cb in checks[1:]:
            cb.state(["!disabled"] if v_tr.get() else ["disabled"])
        checks[3].state(["!disabled"] if v_tr.get() and v_su.get() else ["disabled"])

    v_tr.trace_add("write", sync)
    v_su.trace_add("write", sync)
    sync()
    bar = ttk.Frame(win)
    bar.grid(row=3, column=0, columnspan=2, sticky="e", **PAD)

    def full() -> None:
        result["v"] = (AFTER_FULL, Plan(bool(v_tr.get()), bool(v_sp.get()), bool(v_su.get()), bool(v_ho.get())))
        win.destroy()

    def later() -> None:
        result["v"] = (AFTER_LATER, None)
        win.destroy()

    def discard() -> None:
        if messagebox.askyesno("Aufnahme verwerfen", f"„{title}“ in den Papierkorb verschieben?\n\nAudio und alle "
                               "Daten dieser Aufnahme werden gelöscht (aus dem Papierkorb wiederherstellbar).",
                               icon="warning", parent=win):
            result["v"] = (AFTER_DISCARD, None)
            win.destroy()

    full_btn = ttk.Button(bar, text="Vollständig verarbeiten", command=full)
    full_btn.pack(side="right", padx=4)
    ttk.Button(bar, text="Später", command=later).pack(side="right", padx=4)
    ttk.Button(bar, text="Verwerfen …", command=discard).pack(side="right", padx=4)
    win.protocol("WM_DELETE_WINDOW", later)
    win.bind("<Escape>", lambda e: later())
    _modal(win)
    full_btn.focus_set()
    root.wait_window(win)
    return result["v"]


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------
def ask_update(root: tk.Misc, res: Any, busy: str, install_info: dict[str, Any]) -> str | None:
    """ "now" | "after" | "restore" | None. ``res``: UpdateCheck or the exception of the check."""
    win = tk.Toplevel(root)
    win.title("Nach Updates suchen")
    win.resizable(False, False)
    result: dict[str, str | None] = {"v": None}
    from callrec.updater import version_text

    ttk.Label(win, text=f"Installiert: {version_text()}", font=("Segoe UI", 10, "bold")).pack(anchor="w", **PAD)
    available = False
    if isinstance(res, Exception):
        ttk.Label(win, text=str(res), wraplength=520, foreground="#b00020").pack(anchor="w", **PAD)
    else:
        ttk.Label(win, text=res.message, wraplength=520).pack(anchor="w", **PAD)
        rel = res.release
        available = bool(res.available and rel is not None)
        if rel is not None:
            ttk.Label(win, text=f"Freigegeben: {rel.version} ({rel.commit[:10]})" + (f" vom {rel.date}" if rel.date else ""),
                      foreground="#444").pack(anchor="w", padx=8)
            if rel.notes:
                ttk.Label(win, text="\n".join(f"• {n}" for n in rel.notes), wraplength=520, justify="left").pack(
                    anchor="w", **PAD)
        if available and res.requirements_changed:
            ttk.Label(win, text="Dieses Update installiert auch geänderte Python-Pakete (dauert einige Minuten; die "
                      "bisherige Umgebung wird für eine Rückkehr gesichert).", wraplength=520, foreground="#555").pack(
                anchor="w", **PAD)
    if busy and available:
        ttk.Label(win, text=busy + " Aktualisieren ist erst danach möglich.", wraplength=520,
                  foreground="#b00020").pack(anchor="w", **PAD)
    ttk.Label(win, text="Für das Update wird CallRecorder beendet und danach automatisch neu gestartet. Schlägt etwas "
              "fehl, wird der bisherige Stand wiederhergestellt. Einstellungen, Stimmprofile und Aufnahmen bleiben "
              "unverändert.", wraplength=520, foreground="#555").pack(anchor="w", **PAD)
    bar = ttk.Frame(win)
    bar.pack(fill="x", **PAD)

    def choose(v: str) -> None:
        result["v"] = v
        win.destroy()

    ttk.Button(bar, text="Schließen", command=win.destroy).pack(side="right", padx=4)
    if available:
        if busy:
            ttk.Button(bar, text="Nach Abschluss aktualisieren", command=lambda: choose("after")).pack(side="right", padx=4)
        else:
            ttk.Button(bar, text="Jetzt aktualisieren", command=lambda: choose("now")).pack(side="right", padx=4)
    if install_info.get("previous_commit"):
        ttk.Button(bar, text="Vorherige Version wiederherstellen", command=lambda: choose("restore")).pack(
            side="left", padx=4)
    win.bind("<Escape>", lambda e: win.destroy())
    _modal(win)
    root.wait_window(win)
    return result["v"]
