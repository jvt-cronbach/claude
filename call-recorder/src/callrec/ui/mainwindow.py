"""The main window: record control, list of recordings with details, processing status, setup checklist.

Threading: every method here runs on the Tk thread. Work that can take long (scanning the
recordings folder, reading transcripts, calendar, setup probes, connection tests) runs on
short-lived worker threads that hand their result back through :meth:`MainWindow.post`
(a queue drained by an ``after`` loop) - Tk is never touched from another thread. The
window only *reads* live state from the controller and the processing queue; every action
goes through :class:`callrec.ui.actions.AppActions`.
"""

from __future__ import annotations

import functools
import logging
import queue
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, simpledialog, ttk
from typing import Any, cast

from callrec import hints, setupcheck
from callrec.calendar.service import Suggestion
from callrec.jobs import CANCELLED, DONE, FAILED, Job
from callrec.library import (
    FILTERS,
    OPEN,
    LibraryError,
    RecordingIndex,
    RecordingInfo,
    filter_infos,
    fmt_duration,
    fmt_language,
    handoff_description,
    recycle_bin_available,
    stage_cells,
    status_description,
)
from callrec.recording import Recording
from callrec.transcript import WEEKDAYS
from callrec.ui.viewmodel import (
    MANUAL_EVENT,
    VersionChoice,
    WindowState,
    action_states,
    clamp_geometry,
    default_geometry,
    delete_question,
    event_label,
    fmt_clock,
    format_geometry,
    markdown_spans,
    parse_geometry,
    retention_due_text,
    version_choices,
)
from callrec.ui.viewmodel import next_action as compact_next_action

log = logging.getLogger(__name__)

# tray notifications that the window shows itself in a better form (or that are pure noise there)
_SKIP_NOTIFY = ("Fertig:", "Verarbeitung fehlgeschlagen", "Verarbeitung abgebrochen", "Aufnahme läuft", "Aufnahme beendet",
                "CallRecorder bereit", "Verarbeitung gestartet", "Neu-Transkription gestartet",
                "Zusammenfassung wird neu erstellt", "Verarbeitung abbrechen")
_ERROR_NOTIFY = ("nicht möglich", "fehlgeschlagen", "Problem", "ungültig", "Achtung", "Keine Übertragung")

COLUMNS = (  # logical pixels at 100 %, scaled with the DPI
    ("date", "Datum", 110, "w"),
    ("title", "Titel", 150, "w"),
    ("dur", "Dauer", 56, "e"),
    ("lang", "Sprache", 64, "w"),
    ("t", "Transkript", 72, "center"),
    ("s", "Sprecher", 68, "center"),
    ("z", "Zusammenf.", 88, "center"),
    ("h", "SharePoint", 76, "center"),
)


class FlowFrame(ttk.Frame):
    """Lays its children out left to right and wraps them into new rows when narrow."""

    def __init__(self, master: tk.Misc, gap: int = 4) -> None:
        super().__init__(master)
        self.gap = gap
        self.items: list[tk.Widget] = []
        self.bind("<Configure>", lambda e: self.relayout(e.width))

    def add(self, widget: tk.Widget) -> None:
        self.items.append(widget)

    def relayout(self, width: int | None = None) -> None:
        width = width or self.winfo_width()
        if width <= 1:
            width = sum(w.winfo_reqwidth() + self.gap for w in self.items)
        x = y = row_h = 0
        for w in self.items:
            rw, rh = w.winfo_reqwidth(), w.winfo_reqheight()
            if x and x + rw > width:
                x, y = 0, y + row_h + self.gap
                row_h = 0
            w.place(in_=self, x=x, y=y)
            x += rw + self.gap
            row_h = max(row_h, rh)
        h = y + row_h
        if int(self.cget("height") or 0) != h:
            self.configure(height=h)


class MainWindow:
    POLL_MS = 250
    LIST_REFRESH_S = 20.0
    CALENDAR_REFRESH_S = 300.0

    def __init__(self, root: tk.Misc, app: Any, index: RecordingIndex | None = None,
                 state_path: Path | None = None) -> None:
        self.root = root
        self.app = app
        self.cfg = app.cfg
        self.controller = app.controller
        self.queue = app.queue
        self.index = index or RecordingIndex(self.cfg.recordings_path)
        self.state_path = state_path
        self.state = WindowState.load(state_path)
        self._calls: queue.Queue[Callable[[], object]] = queue.Queue()
        self.infos: list[RecordingInfo] = []
        self.by_id: dict[str, RecordingInfo] = {}
        self.selected: RecordingInfo | None = None
        self._loading = False
        self._reload_pending = False
        self._last_list_refresh = 0.0
        self._last_calendar = 0.0
        self._detail_token = 0
        self._text_keys: dict[str, tuple[str, float]] = {}
        self._events: list[Any] = []
        self._event_source = "manual"
        self._seen_jobs: set[int] = set()
        self._msg_action: Callable[[], object] | None = None
        self._summary_test: tuple[str, bool] | None = None
        self._checks: list[setupcheck.CheckItem] = []
        self._audio_item: setupcheck.CheckItem | None = None
        self._download_text = ""
        self._whisper_label: tuple[ttk.Label, str] | None = None
        self._indeterminate = False
        self._started_at = time.time()
        self.visible = False
        self.closed = False

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        from callrec.updater import version_text

        self.win.title(version_text())
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        self.scale = max(1.0, float(self.win.winfo_fpixels("1i")) / 96.0)
        self._set_icon()
        self._styles()
        self._build()
        self._apply_geometry()
        self.queue.add_listener(lambda job: self.post(lambda: self._job_finished(job)))
        app.on_changed.append(lambda rec: self.post(lambda: self.refresh_list(rec)))
        app.on_config_changed.append(lambda: self.post(self.after_settings))
        self.win.after(100, self._pump)
        self.win.after(self.POLL_MS, self._tick)

    # ------------------------------------------------------------------
    # threading helpers
    # ------------------------------------------------------------------
    def post(self, fn: Callable[[], object]) -> None:
        """Run ``fn`` on the Tk thread (safe from any thread)."""
        self._calls.put(fn)

    def _pump(self) -> None:
        if self.closed:
            return
        self.win.after(100, self._pump)
        try:
            while True:
                fn = self._calls.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("Fehler im Hauptfenster")
        except queue.Empty:
            pass

    def bg(self, work: Callable[[], Any], done: Callable[[Any], None], name: str = "window-bg") -> None:
        """Run ``work`` on a worker thread, then ``done(result_or_exception)`` on the Tk thread."""
        def run() -> None:
            try:
                res: Any = work()
            except Exception as exc:  # handed to done()
                log.debug("Hintergrundarbeit %s fehlgeschlagen", name, exc_info=True)
                res = exc
            self.post(lambda: done(res))

        threading.Thread(target=run, name=name, daemon=True).start()

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------
    def _set_icon(self) -> None:
        try:
            from PIL import ImageTk

            from callrec.ui.tray import IDLE, make_icon

            self._icon = ImageTk.PhotoImage(make_icon(IDLE, size=64), master=self.win)
            self.win.iconphoto(False, self._icon)  # type: ignore[arg-type]
        except Exception:  # cosmetic only
            log.debug("Fenstersymbol nicht gesetzt", exc_info=True)

    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def _styles(self) -> None:
        base = tkfont.nametofont("TkDefaultFont")
        size = int(base.cget("size"))
        sign = -1 if size < 0 else 1
        self.f_title = base.copy()
        self.f_title.configure(size=sign * (abs(size) + 4), weight="bold")
        self.f_big = base.copy()
        self.f_big.configure(size=sign * (abs(size) + 3), weight="bold")
        self.f_bold = base.copy()
        self.f_bold.configure(weight="bold")
        self.f_h1 = base.copy()
        self.f_h1.configure(size=sign * (abs(size) + 5), weight="bold")
        self.f_h2 = base.copy()
        self.f_h2.configure(size=sign * (abs(size) + 3), weight="bold")
        self.f_h3 = base.copy()
        self.f_h3.configure(size=sign * (abs(size) + 1), weight="bold")
        self.f_italic = base.copy()
        self.f_italic.configure(slant="italic")
        self.f_fixed = tkfont.nametofont("TkFixedFont")
        style = ttk.Style(self.win)
        style.configure("Big.TButton", font=self.f_big, padding=(self.px(14), self.px(8)))
        style.configure("Title.TLabel", font=self.f_title)
        style.configure("Bold.TLabel", font=self.f_bold)
        # accent only (the state is always spelled out in text as well)
        style.configure("Rec.TLabel", font=self.f_big, foreground="#c62828")
        style.configure("State.TLabel", font=self.f_big)
        style.configure("Prompt.TFrame", background="#ffe082")
        style.configure("Prompt.TLabel", background="#ffe082", font=self.f_big)
        style.configure("PromptDetail.TLabel", background="#ffe082")
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Treeview", rowheight=int(line * 1.45))

    def _build_menu(self) -> None:
        """Native menu bar (Alt / F10 on Windows) for everything that is not about one recording."""
        bar = tk.Menu(self.win)
        app = self.app
        datei = tk.Menu(bar, tearoff=False)
        datei.add_command(label="Aufnahmeordner öffnen", underline=0, command=lambda: app.open_folder(None))
        datei.add_command(label="Protokolldatei öffnen", underline=0, command=app.open_log)
        datei.add_separator()
        datei.add_command(label="Fenster schließen (läuft im Infobereich weiter)", underline=0, command=self.hide)
        datei.add_command(label="CallRecorder beenden", underline=12, command=app.quit)
        extras = tk.Menu(bar, tearoff=False)
        extras.add_command(label="Einstellungen …", underline=0, command=lambda: app.settings(""))
        extras.add_command(label="Stimmprofile verwalten …", underline=0, command=app.manage_profiles)
        extras.add_command(label="Bei Microsoft anmelden …", underline=4,
                           command=lambda: app.login(done=lambda text: self.post(self.run_checks)))
        extras.add_command(label="Einrichtung prüfen", underline=10, command=self.open_setup)
        extras.add_command(label="Zusammenfassungs-Prompt bearbeiten …", underline=0,
                           command=lambda: app.edit_prompts(self.selected_recording()))
        hilfe = tk.Menu(bar, tearoff=False)
        hilfe.add_command(label="Anleitung (README) öffnen", underline=0, command=app.open_readme)
        hilfe.add_command(label="Nach Updates suchen …", underline=5, command=lambda: app.check_updates())
        hilfe.add_command(label="Über CallRecorder", underline=0, command=self.about)
        bar.add_cascade(label="Datei", menu=datei, underline=0)
        bar.add_cascade(label="Extras", menu=extras, underline=0)
        bar.add_cascade(label="Hilfe", menu=hilfe, underline=0)
        self.win.configure(menu=bar)
        self.menus = {"Datei": datei, "Extras": extras, "Hilfe": hilfe}

    def about(self) -> None:
        from tkinter import messagebox

        from callrec.updater import version_text

        messagebox.showinfo("Über CallRecorder", f"{version_text()}\nLokale Gesprächsaufnahme mit Transkript, "
                            "Sprechererkennung und Zusammenfassung.", parent=self.win)

    def open_setup(self) -> None:
        self.set_compact(False)
        self.nb.select(self.tab_setup)
        self.run_checks(force_audio=True)

    def _build(self) -> None:
        w = self.win
        self._build_menu()
        w.columnconfigure(0, weight=1)
        w.rowconfigure(1, weight=1)
        self._build_record(w).grid(row=0, column=0, sticky="we", padx=self.px(8), pady=(self.px(8), self.px(4)))
        self.nb = ttk.Notebook(w)
        self.nb.grid(row=1, column=0, sticky="nsew", padx=self.px(8))
        self.compact_frame = self._build_compact(w)
        self.compact_frame.grid(row=1, column=0, sticky="nsew", padx=self.px(8))
        self.nb.enable_traversal()  # Ctrl+Tab / Ctrl+Shift+Tab
        self.tab_recordings = self._build_recordings(self.nb)
        self.tab_setup = self._build_setup(self.nb)
        self.nb.add(self.tab_recordings, text="Aufnahmen", underline=0)
        self.nb.add(self.tab_setup, text="Einrichtung", underline=0)
        self._build_status(w).grid(row=2, column=0, sticky="we", padx=self.px(8), pady=(self.px(4), self.px(8)))
        self.set_compact(self.state.compact)
        w.bind("<Control-f>", lambda e: self.focus_search())
        w.bind("<F5>", lambda e: self.refresh_all())
        w.bind("<Escape>", lambda e: self.hide())

    # -- record panel ---------------------------------------------------
    def _build_record(self, parent: tk.Misc) -> ttk.Labelframe:
        f = ttk.LabelFrame(parent, text="Aufnahme")
        f.columnconfigure(2, weight=1)
        self.rec_btn = ttk.Button(f, text="●  Aufnahme starten …", style="Big.TButton", command=self.toggle_record)
        self.rec_btn.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=self.px(8), pady=self.px(6))
        state = ttk.Frame(f)
        state.grid(row=0, column=1, rowspan=2, sticky="nw", padx=self.px(8), pady=self.px(4))
        self.rec_state = tk.StringVar(value="Bereit")
        self.rec_state_lbl = ttk.Label(state, textvariable=self.rec_state, style="State.TLabel")
        self.rec_state_lbl.grid(row=0, column=0, columnspan=2, sticky="w")
        self.level_bars: dict[str, ttk.Progressbar] = {}
        for i, (key, label) in enumerate((("mic", "Ich"), ("remote", "Gegenseite")), start=1):
            ttk.Label(state, text=label + ":").grid(row=i, column=0, sticky="w", padx=(0, self.px(6)))
            bar = ttk.Progressbar(state, maximum=1.0, length=self.px(150), mode="determinate")
            bar.grid(row=i, column=1, sticky="w", pady=1)
            self.level_bars[key] = bar
        ev = ttk.Frame(f)
        ev.grid(row=0, column=2, rowspan=2, sticky="nwe", padx=self.px(8), pady=self.px(4))
        ev.columnconfigure(1, weight=1)
        ttk.Label(ev, text="Termin:").grid(row=0, column=0, sticky="w")
        self.event_var = tk.StringVar(value="")
        self.event_combo = ttk.Combobox(ev, textvariable=self.event_var, state="readonly", values=[MANUAL_EVENT])
        self.event_combo.grid(row=0, column=1, sticky="we", padx=self.px(4))
        self.event_btn = ttk.Button(ev, text="Termine aktualisieren", command=lambda: self.refresh_calendar(force=True))
        self.event_btn.grid(row=0, column=2, sticky="e")
        self.view_btn = ttk.Button(ev, text="Mehr …", command=lambda: self.set_compact(not self.compact))
        self.view_btn.grid(row=0, column=3, sticky="e", padx=(self.px(4), 0))
        self.event_info = tk.StringVar(value="Termine werden geladen … Vor dem Start kommt immer die Frage vor der Aufnahme.")
        self.event_info_lbl = ttk.Label(ev, textvariable=self.event_info, wraplength=self.px(520))
        self.event_info_lbl.grid(row=1, column=0, columnspan=3, sticky="w", pady=(self.px(2), 0))
        ev.bind("<Configure>", lambda e: self._rewrap(ev, e.width))
        # automatic end: what will happen (always) and the countdown with its two buttons (when pending)
        self.autostop_info = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.autostop_info, foreground="#555").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=self.px(8))
        self.autostop_frame = ttk.Frame(f, style="Prompt.TFrame", padding=self.px(6))
        self.autostop_frame.grid(row=3, column=0, columnspan=3, sticky="we", padx=self.px(8), pady=(self.px(4), self.px(6)))
        self.autostop_frame.columnconfigure(0, weight=1)
        self.autostop_head = tk.StringVar(value="")
        self.autostop_detail = tk.StringVar(value="")
        ttk.Label(self.autostop_frame, textvariable=self.autostop_head, style="Prompt.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(self.autostop_frame, textvariable=self.autostop_detail, style="PromptDetail.TLabel").grid(
            row=1, column=0, sticky="w")
        self.autostop_keep_btn = ttk.Button(self.autostop_frame, text="Weiter aufnehmen", command=self.autostop_keep)
        self.autostop_keep_btn.grid(row=0, column=1, rowspan=2, padx=self.px(4))
        self.autostop_stop_btn = ttk.Button(self.autostop_frame, text="Jetzt beenden", command=self.autostop_stop)
        self.autostop_stop_btn.grid(row=0, column=2, rowspan=2, padx=self.px(4))
        self.autostop_close_btn = ttk.Button(self.autostop_frame, text="Ausblenden", command=self.autostop_dismiss)
        self.autostop_close_btn.grid(row=0, column=3, rowspan=2, padx=self.px(4))
        self.autostop_frame.grid_remove()
        self._autostop_kind = ""
        return f

    # -- automatic end ----------------------------------------------------
    @property
    def autostop(self) -> Any:
        return getattr(self.app, "autostop", None)

    def autostop_keep(self) -> None:
        if self.autostop is not None:
            self.autostop.keep_recording()
        self._update_autostop()

    def autostop_stop(self) -> None:
        if self.autostop is not None:
            self.autostop.stop_now()
            self.app._changed(None)
        self._update_record_panel()

    def autostop_dismiss(self) -> None:
        if self.autostop is not None:
            self.autostop.dismiss_message()
        self._update_autostop()

    def _update_autostop(self) -> None:
        a = self.autostop
        if a is None:
            self.autostop_info.set("")
            self.autostop_frame.grid_remove()
            return
        self.autostop_info.set(a.status_text())
        view = a.view() if self.controller.is_recording else None
        message = a.message() if view is None else ""
        if view is None and not message:
            self.autostop_frame.grid_remove()
            self._autostop_kind = ""
            return
        if view is not None:
            self.autostop_head.set(view.headline)
            self.autostop_detail.set(view.detail)
            self.autostop_keep_btn.configure(text=view.keep_label)
            self.autostop_keep_btn.grid()
            self.autostop_stop_btn.grid()
            self.autostop_close_btn.grid_remove()
            kind = view.kind
        else:
            self.autostop_head.set(message)
            self.autostop_detail.set("")
            self.autostop_keep_btn.grid_remove()
            self.autostop_stop_btn.grid_remove()
            self.autostop_close_btn.grid()
            kind = "message"
        self.autostop_frame.grid()
        self._autostop_kind = kind

    @staticmethod
    def _rewrap(frame: tk.Misc, width: int) -> None:
        for child in frame.winfo_children():
            if isinstance(child, ttk.Label) and str(child.cget("wraplength")) not in ("", "0"):
                child.configure(wraplength=max(200, width - 8))

    # -- compact view -------------------------------------------------------
    COMPACT_ROWS = 5

    def _build_compact(self, parent: tk.Misc) -> ttk.Frame:
        f = ttk.Frame(parent)
        f.columnconfigure(0, weight=1)
        self.compact_setup = tk.StringVar(value="")
        self.compact_setup_row = ttk.Frame(f)
        self.compact_setup_row.grid(row=0, column=0, sticky="we", pady=(0, self.px(4)))
        self.compact_setup_row.columnconfigure(0, weight=1)
        ttk.Label(self.compact_setup_row, textvariable=self.compact_setup, wraplength=self.px(560)).grid(
            row=0, column=0, sticky="w")
        ttk.Button(self.compact_setup_row, text="Einrichtung öffnen", command=self.open_setup).grid(row=0, column=1)
        self.compact_setup_row.grid_remove()
        box = ttk.LabelFrame(f, text="Letzte Aufnahmen")
        box.grid(row=1, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        self.compact_rows = ttk.Frame(box)
        self.compact_rows.grid(row=0, column=0, sticky="nsew", padx=self.px(8), pady=self.px(4))
        self.compact_rows.columnconfigure(0, weight=1)
        bar = ttk.Frame(box)
        bar.grid(row=1, column=0, sticky="e", padx=self.px(8), pady=(0, self.px(6)))
        ttk.Button(bar, text="Alle Aufnahmen …", command=lambda: self.set_compact(False)).pack(side="right")
        self.compact_items: list[tuple[str, str, str]] = []  # (folder name, status, action key) as shown
        return f

    @property
    def compact(self) -> bool:
        return bool(self.state.compact)

    def set_compact(self, compact: bool) -> None:
        """Compact default view <-> full view (list with details, setup tab)."""
        self.state.compact = compact
        if compact:
            self.nb.grid_remove()
            self.compact_frame.grid()
            self.view_btn.configure(text="Mehr …")
            self._fill_compact()
        else:
            self.compact_frame.grid_remove()
            self.nb.grid()
            self.view_btn.configure(text="Kompakt")

    def _compact_command(self, key: str, folder: Path) -> Callable[[], None]:
        rec = Recording(folder)
        app = self.app
        actions: dict[str, Callable[[], object]] = {
            "process_now": lambda: app.process_now(rec), "review": lambda: app.review(rec),
            "resummarize": lambda: app.resummarize(rec), "handoff": lambda: app.handoff(rec),
        }
        fn = actions.get(key)

        def run() -> None:
            if fn is not None:
                fn()
            self.refresh_list(rec)

        return run

    def _open_in_full_view(self, folder_name: str) -> None:
        self.set_compact(False)
        self.nb.select(self.tab_recordings)
        self.select(folder_name)

    def _fill_compact(self) -> None:
        if not self.compact:
            return
        infos = [i for i in self.infos if not i.error][: self.COMPACT_ROWS]
        cur = self.controller.current
        shown: list[tuple[str, str, str]] = []
        rows: list[tuple[RecordingInfo, str, str, str]] = []
        for info in infos:
            recording_now = cur is not None and cur.folder == info.folder
            busy = self.queue.is_busy_with(info.folder)
            status, label, key = compact_next_action(info, busy=busy, recording_now=recording_now)
            shown.append((info.id, status, key))
            rows.append((info, status, label, key))
        if shown == self.compact_items and self.compact_rows.winfo_children():
            return
        self.compact_items = shown
        for child in self.compact_rows.winfo_children():
            child.destroy()
        if not rows:
            ttk.Label(self.compact_rows, text="Noch keine Aufnahmen.").grid(row=0, column=0, sticky="w")
            return
        for r, (info, status, label, key) in enumerate(rows):
            when = f"{info.started_at:%d.%m. %H:%M}" if info.started_at else ""
            title = info.title if len(info.title) <= 48 else info.title[:47] + "…"
            ttk.Label(self.compact_rows, text=f"{when}  {title}").grid(row=r, column=0, sticky="w", pady=1)
            ttk.Label(self.compact_rows, text=status, foreground="#555").grid(row=r, column=1, sticky="w",
                                                                           padx=self.px(8))
            if label:
                ttk.Button(self.compact_rows, text=label, command=self._compact_command(key, info.folder)).grid(
                    row=r, column=2, sticky="e", padx=self.px(2))
            ttk.Button(self.compact_rows, text="Öffnen", command=functools.partial(self._open_in_full_view, info.id)
                       ).grid(row=r, column=3, sticky="e", padx=self.px(2))

    # -- recordings tab -------------------------------------------------
    def _build_recordings(self, parent: tk.Misc) -> ttk.Frame:
        tab = ttk.Frame(parent)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        self.paned = ttk.PanedWindow(tab, orient="horizontal")
        self.paned.grid(row=0, column=0, sticky="nsew", pady=self.px(4))
        left = ttk.Frame(self.paned)
        left.columnconfigure(1, weight=1)
        left.rowconfigure(1, weight=1)
        ttk.Label(left, text="Suchen:").grid(row=0, column=0, sticky="w", padx=(self.px(4), self.px(2)))
        self.search_var = tk.StringVar()
        self.search = ttk.Entry(left, textvariable=self.search_var)
        self.search.grid(row=0, column=1, sticky="we", padx=self.px(2), pady=self.px(4))
        self.search_var.trace_add("write", lambda *a: self.apply_filter())
        self.search.bind("<Escape>", self._clear_search)
        self.search.bind("<Down>", lambda e: self._focus_tree())
        self.filter_var = tk.StringVar(value=FILTERS.get(self.state.filter, FILTERS["all"]))
        self.filter_combo = ttk.Combobox(left, textvariable=self.filter_var, state="readonly",
                                         values=list(FILTERS.values()), width=30)
        self.filter_combo.grid(row=0, column=2, sticky="e", padx=self.px(4))
        self.filter_combo.bind("<<ComboboxSelected>>", lambda e: self.apply_filter())
        tf = ttk.Frame(left)
        tf.grid(row=1, column=0, columnspan=3, sticky="nsew")
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(tf, columns=[c[0] for c in COLUMNS], show="headings", selectmode="extended")
        for key, head, width, anchor in COLUMNS:
            self.tree.heading(key, text=head, anchor=anchor)  # type: ignore[call-overload]
            self.tree.column(key, width=self.px(width), minwidth=self.px(90 if key == "title" else 40),  # type: ignore[call-overload]
                             anchor=anchor,
                             stretch=key == "title")
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="we")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Return>", lambda e: self._focus_text())
        self.tree.bind("<Delete>", lambda e: self.delete_selected())
        self.tree.bind("<Button-3>", self._context_menu)
        self.tree.bind("<Shift-F10>", self._context_menu)
        if self.win.tk.call("tk", "windowingsystem") == "win32":
            self.tree.bind("<KeyPress-App>", self._context_menu)  # the context-menu key (Windows keysym)
        self.tree.bind("<Double-1>", lambda e: self._focus_text())
        self.tree.bind("<Configure>", lambda e: self._fit_columns(e.width))
        foot = ttk.Frame(left)
        foot.grid(row=2, column=0, columnspan=3, sticky="we")
        self.count_var = tk.StringVar(value="Aufnahmen werden geladen …")
        ttk.Label(foot, textvariable=self.count_var).pack(side="left", padx=self.px(4))
        self.multi_btn = ttk.Button(foot, text="Auswahl löschen …", command=self.delete_selected)
        self.retention_var = tk.StringVar(value="")
        ret = ttk.Frame(left)
        ret.grid(row=3, column=0, columnspan=3, sticky="we")
        ttk.Label(ret, textvariable=self.retention_var, foreground="#8a4b00", wraplength=self.px(420)).pack(
            side="left", padx=self.px(4))
        self.ret_keep_btn = ttk.Button(ret, text="Behalten", command=self.retention_keep)
        self.ret_now_btn = ttk.Button(ret, text="Jetzt löschen …", command=self.retention_now)
        self._ret_next: list[str] = []
        ttk.Button(foot, text="Aufnahmeordner öffnen", command=lambda: self.app.open_folder(None)).pack(
            side="right", padx=self.px(2), pady=self.px(2))
        ttk.Button(foot, text="Aktualisieren", command=self.refresh_all).pack(side="right", padx=self.px(2))
        self.paned.add(left, weight=1)
        self.paned.add(self._build_details(self.paned), weight=1)
        return tab

    def _build_details(self, parent: tk.Misc) -> ttk.Frame:
        d = ttk.Frame(parent)
        d.columnconfigure(0, weight=1)
        d.rowconfigure(5, weight=1)
        self.d_title = tk.StringVar(value="Keine Aufnahme ausgewählt")
        self.d_meta = tk.StringVar(value="Links eine Aufnahme auswählen.")
        self.d_status = tk.StringVar(value="")
        self.d_handoff = tk.StringVar(value="")
        self.d_labels = [
            ttk.Label(d, textvariable=self.d_title, style="Title.TLabel"),
            ttk.Label(d, textvariable=self.d_meta),
            ttk.Label(d, textvariable=self.d_status),
            ttk.Label(d, textvariable=self.d_handoff),
        ]
        for i, lbl in enumerate(self.d_labels):
            lbl.configure(wraplength=self.px(420))
            lbl.grid(row=i, column=0, sticky="w", padx=self.px(6), pady=(self.px(4) if i == 0 else 0, 0))
        d.bind("<Configure>", lambda e: [lbl.configure(wraplength=max(200, e.width - self.px(16)))
                                         for lbl in self.d_labels])
        self.actions_frame = FlowFrame(d, gap=self.px(4))
        self.actions_frame.grid(row=4, column=0, sticky="we", padx=self.px(6), pady=self.px(6))
        # frequent actions as buttons, the rest in a keyboard-accessible menu (Space/Enter opens it)
        self.action_commands: dict[str, Callable[[], None]] = {
            "handoff": self._a(self.app.handoff),
            "review": self._a(self.app.review),
            "resummarize": self._a(self.app.resummarize),
            "play": self._a(self.app.play),
            "folder": self._a(self.app.open_folder),
            "retranscribe": self._a(self.app.ask_retranscribe),
            "assign_event": self._a(self.app.assign_event),
            "participants": self._a(self.app.edit_participants),
            "recheck": self._a(self.app.recheck_speakers),
            "rerender": self._a(self.app.rerender),
            "speaker_limit": self._a(self.app.toggle_speaker_limit),
            "process_now": self._a(self.app.process_now),
            "reprocess": self.reprocess_selected,
            "rename": self.rename_selected,
            "delete": self.delete_selected,
            "delete_audio": functools.partial(self.delete_selected, "audio"),
            "delete_material": functools.partial(self.delete_selected, "audio_transcript"),
            "pin": self.toggle_pin,
        }
        self.buttons: dict[str, ttk.Button] = {}
        for key, text in (
            ("process_now", "Jetzt verarbeiten"),
            ("handoff", "An SharePoint übergeben …"),
            ("review", "Sprecher zuordnen …"),
            ("resummarize", "Zusammenfassung neu erstellen"),
            ("play", "Anhören"),
            ("delete", "Löschen …"),
        ):
            b = ttk.Button(self.actions_frame, text=text, command=self.action_commands[key])
            b.state(["disabled"])
            self.actions_frame.add(b)
            self.buttons[key] = b
        self.more_btn = ttk.Menubutton(self.actions_frame, text="Weitere …")
        self.more_menu = tk.Menu(self.more_btn, tearoff=False)
        self.menu_items = (
            ("folder", "Ordner öffnen"),
            ("assign_event", "Termin zuordnen …"),
            ("participants", "Teilnehmende bearbeiten …"),
            ("recheck", "Sprecherzuordnung neu prüfen …"),
            ("rerender", "Transkript neu darstellen"),
            ("speaker_limit", "Weitere Sprecher zulassen / begrenzen"),
            ("retranscribe", "Neu transkribieren …"),
            ("reprocess", "Komplett neu verarbeiten …"),
            ("rename", "Umbenennen …"),
            ("pin", "Behalten (nicht automatisch löschen)"),
            ("delete_audio", "Nur Audio löschen …"),
            ("delete_material", "Audio und Transkript löschen (Zusammenfassung bleibt) …"),
            ("delete", "Aufnahme vollständig löschen …"),
        )
        self._menu_index: dict[str, int] = {}
        for key, text in self.menu_items:
            if key in ("retranscribe", "pin"):
                self.more_menu.add_separator()
            self.more_menu.add_command(label=text, command=self.action_commands[key], state="disabled")
            self._menu_index[key] = int(self.more_menu.index("end") or 0)
        self.more_btn.configure(menu=self.more_menu)
        self.more_btn.state(["disabled"])
        self.actions_frame.add(self.more_btn)
        self._enabled: dict[str, bool] = {}
        self.text_nb = ttk.Notebook(d)
        self.text_nb.grid(row=5, column=0, sticky="nsew", padx=self.px(6), pady=(0, self.px(4)))
        self.views: dict[str, dict[str, Any]] = {}
        for kind, label in (("summary", "Zusammenfassung"), ("transcript", "Transkript")):
            self.views[kind] = self._build_text_view(self.text_nb, kind)
            self.text_nb.add(self.views[kind]["frame"], text=label)
        return d

    # columns hidden first when the list is narrow (both are repeated in the details pane)
    OPTIONAL_COLUMNS = ("lang", "dur")

    def _columns_width(self, keys: list[str]) -> int:
        return sum(self.px(c[2]) for c in COLUMNS if c[0] in keys)

    def _fit_columns(self, width: int) -> None:
        keys = [c[0] for c in COLUMNS]
        for optional in self.OPTIONAL_COLUMNS:
            if self._columns_width(keys) <= width:
                break
            keys.remove(optional)
        current = [str(c) for c in self.tree.cget("displaycolumns")]
        if current == ["#all"]:
            current = [c[0] for c in COLUMNS]
        if current != keys:
            self.tree.configure(displaycolumns=keys)

    def _a(self, fn: Callable[[Recording], object]) -> Callable[[], None]:
        def run() -> None:
            rec = self.selected_recording()
            if rec is not None:
                fn(rec)
        return run

    def _build_text_view(self, parent: tk.Misc, kind: str) -> dict[str, Any]:
        f = ttk.Frame(parent)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        bar = ttk.Frame(f)
        bar.grid(row=0, column=0, columnspan=2, sticky="we", pady=self.px(2))
        ttk.Label(bar, text="Version:").pack(side="left", padx=(self.px(2), self.px(4)))
        var = tk.StringVar()
        ttk.Button(bar, text="Groß anzeigen", command=functools.partial(self.open_reader, kind)).pack(
            side="right", padx=(self.px(4), 0))
        combo = ttk.Combobox(bar, textvariable=var, state="readonly", width=16)
        combo.pack(side="left", fill="x", expand=True)
        combo.bind("<<ComboboxSelected>>", functools.partial(self._version_selected, kind))
        text = tk.Text(f, wrap="word", height=10, padx=self.px(8), pady=self.px(6), undo=False, takefocus=1,
                       borderwidth=1, relief="solid", highlightthickness=1, font="TkTextFont")
        sb = ttk.Scrollbar(f, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set, state="disabled")
        text.grid(row=1, column=0, sticky="nsew")
        sb.grid(row=1, column=1, sticky="ns")
        text.bind("<1>", lambda e: text.focus_set())
        for tag, opts in (
            ("h1", {"font": self.f_h1, "spacing1": self.px(8), "spacing3": self.px(4)}),
            ("h2", {"font": self.f_h2, "spacing1": self.px(10), "spacing3": self.px(3)}),
            ("h3", {"font": self.f_h3, "spacing1": self.px(6), "spacing3": self.px(2)}),
            ("bold", {"font": self.f_bold}),
            ("italic", {"font": self.f_italic}),
            ("code", {"font": self.f_fixed}),
            ("table", {"font": self.f_fixed, "lmargin1": self.px(8), "lmargin2": self.px(8)}),
            ("bullet", {"lmargin1": self.px(12), "lmargin2": self.px(26)}),
            ("bullet2", {"lmargin1": self.px(30), "lmargin2": self.px(44)}),
            ("quote", {"font": self.f_italic, "lmargin1": self.px(16), "lmargin2": self.px(16)}),
            ("rule", {"justify": "center"}),
        ):
            text.tag_configure(tag, **opts)
        # bold inside a heading keeps the heading size
        for h in ("h1", "h2", "h3"):
            text.tag_raise(h)
        return {"frame": f, "combo": combo, "var": var, "text": text, "choices": []}

    # -- setup tab --------------------------------------------------------
    def _build_setup(self, parent: tk.Misc) -> ttk.Frame:
        tab = ttk.Frame(parent)
        tab.columnconfigure(0, weight=1)
        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky="we", padx=self.px(8), pady=self.px(8))
        top.columnconfigure(0, weight=1)
        self.setup_summary = tk.StringVar(value="Prüfe Einrichtung …")
        self.setup_summary_lbl = ttk.Label(top, textvariable=self.setup_summary, style="Bold.TLabel",
                                           wraplength=self.px(700))
        self.setup_summary_lbl.grid(row=0, column=0, sticky="w")
        ttk.Button(top, text="Erneut prüfen", command=lambda: self.run_checks(force_audio=True)).grid(
            row=0, column=1, sticky="e")
        self.setup_rows = ttk.Frame(tab)
        self.setup_rows.grid(row=1, column=0, sticky="nsew", padx=self.px(8))
        self.setup_rows.columnconfigure(1, weight=1)
        tab.bind("<Configure>", lambda e: self._rewrap_setup(e.width))
        return tab

    def _rewrap_setup(self, width: int) -> None:
        self.setup_summary_lbl.configure(wraplength=max(300, width - self.px(160)))
        for child in self.setup_rows.winfo_children():
            if getattr(child, "_detail", False):
                cast(ttk.Label, child).configure(wraplength=max(250, width - self.px(420)))

    # -- status panel ---------------------------------------------------
    def _build_status(self, parent: tk.Misc) -> ttk.Labelframe:
        f = ttk.LabelFrame(parent, text="Verarbeitung")
        f.columnconfigure(0, weight=1)
        self.job_var = tk.StringVar(value="Keine Verarbeitung aktiv.")
        self.job_lbl = ttk.Label(f, textvariable=self.job_var)
        self.job_lbl.grid(row=0, column=0, sticky="we", padx=self.px(8), pady=self.px(4))
        self.job_bar = ttk.Progressbar(f, maximum=1.0, length=self.px(200), mode="determinate")
        self.job_bar.grid(row=0, column=1, padx=self.px(4))
        self.queue_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.queue_var).grid(row=0, column=2, padx=self.px(4))
        self.cancel_btn = ttk.Button(f, text="Abbrechen", command=self.cancel_job)
        self.cancel_btn.grid(row=0, column=3, padx=self.px(4))
        self.cancel_btn.state(["disabled"])
        self.clear_queue_btn = ttk.Button(f, text="Wartende entfernen", command=self.cancel_pending)
        self.clear_queue_btn.grid(row=0, column=4, padx=(0, self.px(8)))
        self.clear_queue_btn.grid_remove()
        self.msg_frame = ttk.Frame(f)
        self.msg_frame.grid(row=1, column=0, columnspan=5, sticky="we", padx=self.px(8), pady=(0, self.px(4)))
        self.msg_frame.columnconfigure(1, weight=1)
        self.msg_icon = tk.StringVar(value="")
        ttk.Label(self.msg_frame, textvariable=self.msg_icon, style="Bold.TLabel").grid(row=0, column=0, sticky="nw")
        self.msg_var = tk.StringVar(value="")
        self.msg_lbl = ttk.Label(self.msg_frame, textvariable=self.msg_var, wraplength=self.px(700))
        self.msg_lbl.grid(row=0, column=1, sticky="we", padx=self.px(6))
        self.msg_btn = ttk.Button(self.msg_frame, text="", command=self._run_msg_action)
        self.msg_btn.grid(row=0, column=2, padx=self.px(2))
        ttk.Button(self.msg_frame, text="Ausblenden", command=self.clear_message).grid(row=0, column=3)
        self.msg_frame.bind("<Configure>", lambda e: self.msg_lbl.configure(
            wraplength=max(250, e.width - self.px(320))))
        self.msg_frame.grid_remove()
        return f

    # ------------------------------------------------------------------
    # show / hide / geometry
    # ------------------------------------------------------------------
    def _min_size(self) -> tuple[int, int]:
        return self.px(760), self.px(500)

    def _apply_geometry(self) -> None:
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        mw, mh = self._min_size()
        self.win.minsize(min(mw, sw - 20), min(mh, sh - 80))
        geom = parse_geometry(self.state.geometry) or default_geometry(sw, sh, self.scale)
        self.win.geometry(format_geometry(clamp_geometry(geom, sw, sh, mw, mh)))
        tab = {"Einrichtung": self.tab_setup}.get(self.state.tab)
        if tab is not None:
            self.nb.select(tab)

    def show(self, activate: bool = True) -> None:
        """``activate=False``: show without taking the keyboard focus (e.g. the auto-end countdown
        during a call - typing must not end up in this window)."""
        first = not self.visible and self._last_list_refresh == 0.0
        self.win.deiconify()
        if self.state.zoomed:
            try:
                self.win.state("zoomed")
            except tk.TclError:
                pass
        self.win.lift()
        try:  # bring to the front when opened from the tray
            self.win.attributes("-topmost", True)
            self.win.after(300, lambda: self.win.attributes("-topmost", False))
        except tk.TclError:
            pass
        if activate:
            self.win.focus_force()
        self.visible = True
        self.win.after(50, self._restore_sash)
        if first:
            self.rec_btn.focus_set()
            self.run_checks()
        self.refresh_list()
        self.refresh_calendar()

    def _restore_sash(self) -> None:
        """Remembered list width, else wide enough for all status columns (the rest is for reading)."""
        try:
            width = self.paned.winfo_width()
            if width <= 1:
                return
            without_optional = [c[0] for c in COLUMNS if c[0] not in self.OPTIONAL_COLUMNS]
            wanted = self.state.sash or self._columns_width(without_optional) + self.px(22)
            if not self.state.sash and width - wanted > self.px(560) + self._columns_width(list(self.OPTIONAL_COLUMNS)):
                wanted += self._columns_width(list(self.OPTIONAL_COLUMNS))  # wide window: show all columns
            self.paned.sashpos(0, max(self.px(300), min(wanted, width - self.px(380))))
        except tk.TclError:
            pass

    def save_state(self) -> None:
        try:
            zoomed = self.win.state() == "zoomed"
            if not zoomed and self.win.winfo_viewable():
                self.state.geometry = self.win.geometry()
            self.state.zoomed = zoomed
            self.state.sash = int(self.paned.sashpos(0))
            self.state.tab = str(self.nb.tab(self.nb.select(), "text"))
            self.state.filter = self._filter_key()
        except tk.TclError:
            pass
        self.state.save(self.state_path)

    def hide(self) -> None:
        """Closing the window keeps CallRecorder running in the tray."""
        self.save_state()
        self.win.withdraw()
        self.visible = False

    def destroy(self) -> None:
        self.save_state()
        self.closed = True
        self.win.destroy()

    def _clear_search(self, _event: Any = None) -> str:
        self.search_var.set("")
        return "break"

    def focus_search(self) -> None:
        self.set_compact(False)
        self.nb.select(self.tab_recordings)
        self.search.focus_set()
        self.search.select_range(0, "end")

    def _focus_tree(self) -> str:
        self.tree.focus_set()
        children = self.tree.get_children()
        if children and not self.tree.selection():
            self.tree.selection_set(children[0])
            self.tree.focus(children[0])
        return "break"

    def _focus_text(self) -> str:
        view = self.views[self._current_text_kind()]
        view["text"].focus_set()
        return "break"

    def _current_text_kind(self) -> str:
        return "summary" if self.text_nb.index(self.text_nb.select()) == 0 else "transcript"

    def refresh_all(self) -> None:
        self.refresh_list()
        self.refresh_calendar(force=True)
        if str(self.nb.select()) == str(self.tab_setup):
            self.run_checks(force_audio=True)

    # ------------------------------------------------------------------
    # periodic tick: recording state, levels, job progress
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        if self.closed:
            return
        self.win.after(self.POLL_MS, self._tick)
        try:
            self._update_record_panel()
            self._update_jobs()
            if self.visible and self.compact:
                self._fill_compact()  # cheap; rebuilds only when a status changed
            now = time.monotonic()
            if self.visible and now - self._last_list_refresh > self.LIST_REFRESH_S:
                self.refresh_list()
            if self.visible and not self.controller.is_recording and now - self._last_calendar > self.CALENDAR_REFRESH_S:
                self.refresh_calendar()
        except tk.TclError:
            log.debug("Tick nach Schließen", exc_info=True)

    def _update_record_panel(self) -> None:
        recording = self.controller.is_recording
        if recording:
            self.rec_btn.configure(text="■  Aufnahme beenden")
            title = self.controller.current_title()
            self.rec_state.set(f"● Aufnahme läuft  {fmt_clock(self.controller.elapsed())}" + (f"\n{title}" if title else ""))
            self.rec_state_lbl.configure(style="Rec.TLabel")
            levels = self.controller.levels()
            for key, bar in self.level_bars.items():
                bar.configure(value=levels.get(key, 0.0))
            self.event_combo.state(["disabled"])
            self.event_btn.state(["disabled"])
        else:
            self.rec_btn.configure(text="●  Aufnahme starten …")
            self.rec_state.set("Bereit – keine Aufnahme")
            self.rec_state_lbl.configure(style="State.TLabel")
            for bar in self.level_bars.values():
                bar.configure(value=0.0)
            self.event_combo.state(["!disabled", "readonly"])
            self.event_btn.state(["!disabled"])
        self._update_autostop()

    def _update_jobs(self) -> None:
        snap = self.queue.snapshot()
        cur = snap.current
        if cur is None:
            self.job_var.set("Keine Verarbeitung aktiv.")
            self._set_bar(0.0)
            self.cancel_btn.state(["disabled"])
        else:
            text = cur.status_line()
            if cur.cancel_requested:
                text += " – Abbruch angefordert"
            self.job_var.set(text)
            self._set_bar(cur.fraction)
            self.cancel_btn.state(["disabled"] if cur.cancel_requested else ["!disabled"])
        n = len(snap.pending)
        self.queue_var.set(f"{n} weitere wartend" if n else "")
        if n:
            self.clear_queue_btn.grid()
        else:
            self.clear_queue_btn.grid_remove()
        for job in snap.recent:  # also catches jobs whose listener call was missed
            if job.id not in self._seen_jobs and job.finished_at and job.finished_at > self._started_at:
                self._job_finished(job)

    def _set_bar(self, frac: float) -> None:
        if frac < 0:
            if not self._indeterminate:
                self.job_bar.configure(mode="indeterminate")
                self.job_bar.start(80)
                self._indeterminate = True
            return
        if self._indeterminate:
            self.job_bar.stop()
            self.job_bar.configure(mode="determinate")
            self._indeterminate = False
        self.job_bar.configure(value=max(0.0, min(1.0, frac)))

    def cancel_job(self) -> None:
        snap = self.queue.snapshot()
        if snap.current is None:
            return
        text = self.app.cancel_processing(snap.current.id)
        self.show_message(text, "info")

    def cancel_pending(self) -> None:
        snap = self.queue.snapshot()
        if not snap.pending:
            return
        names = "\n".join(f"– {j.title}" for j in snap.pending)
        if messagebox.askyesno("Wartende Verarbeitungen entfernen", f"Diese Verarbeitungen nicht mehr ausführen?\n\n{names}",
                               parent=self.win):
            for j in snap.pending:
                self.queue.cancel(j.id)

    def _job_finished(self, job: Job) -> None:
        if job.id in self._seen_jobs:
            return
        self._seen_jobs.add(job.id)
        rec = job.recording
        name = rec.folder.name
        if job.state == CANCELLED:
            self.show_message(f"Verarbeitung von {name} abgebrochen.", "info")
        elif job.state == FAILED:
            self.show_message(f"Verarbeitung von {name} fehlgeschlagen: {job.error}", "error",
                              hint=hints.explain(str(job.error)), rec=rec)
        elif job.state == DONE and job.result is not None:
            r = job.result
            if r.summary_failed or (r.summary_skipped and "summarize" in job.steps):
                self.show_message(f"{name}: Transkript fertig, aber keine Zusammenfassung – {r.summary_skipped}", "error",
                                  hint=hints.explain(r.summary_skipped), rec=rec)
            elif r.unassigned_speakers and "identify" in r.steps_done:
                self.show_message(f"Fertig: {name}. {r.unassigned_speakers} Stimme(n) noch keiner Person zugeordnet.",
                                  "info", hint=hints.Hint("", "", "review", "Sprecher zuordnen …"), rec=rec)
            else:
                done = "Zusammenfassung erstellt" if "summarize" in r.steps_done else "fertig"
                self.show_message(f"Fertig: {name} – {done}.", "info",
                                  hint=hints.Hint("", "", "handoff", "An SharePoint übergeben …")
                                  if "summarize" in r.steps_done else None, rec=rec)
        self.refresh_list(rec)

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------
    def show_message(self, text: str, level: str = "info", hint: hints.Hint | None = None,
                     rec: Recording | None = None) -> None:
        self.msg_icon.set({"error": "Problem:", "warn": "Hinweis:"}.get(level, "Info:"))
        body = text
        if hint is not None and hint.next_step:
            body += f"\nNächster Schritt: {hint.next_step}"
        self.msg_var.set(body)
        action = self._action_for(hint.action, rec) if hint is not None and hint.action else None
        self._msg_action = action
        if action is not None and hint is not None:
            self.msg_btn.configure(text=hint.action_label or "Beheben")
            self.msg_btn.grid()
        else:
            self.msg_btn.grid_remove()
        self.msg_frame.grid()

    def clear_message(self) -> None:
        self.msg_frame.grid_remove()
        self._msg_action = None

    def _run_msg_action(self) -> None:
        if self._msg_action is not None:
            self._msg_action()

    def notify(self, title: str, message: str) -> None:
        """Tray notifications that matter in the window (called on the Tk thread)."""
        if title.startswith(_SKIP_NOTIFY):
            return
        level = "error" if any(k in title for k in _ERROR_NOTIFY) else "info"
        self.show_message(f"{title}: {message}", level, hint=hints.explain(message) if level == "error" else None)

    def _action_for(self, key: str, rec: Recording | None) -> Callable[[], object] | None:
        app = self.app
        if key.startswith("settings:"):
            return lambda: app.settings(key.split(":", 1)[1])
        simple: dict[str, Callable[[], object]] = {"open_log": app.open_log, "login": app.login,
                                                   "readme": app.open_readme}
        if key in simple:
            return simple[key]
        per_rec: dict[str, Callable[[Recording], object]] = {
            "resummarize": app.resummarize, "reprocess": app.reprocess, "retranscribe": app.ask_retranscribe,
            "review": app.review, "handoff": app.handoff,
        }
        if key in per_rec and rec is not None:
            fn = per_rec[key]
            return lambda: fn(rec)
        return None

    # ------------------------------------------------------------------
    # recording control
    # ------------------------------------------------------------------
    def selected_suggestion(self) -> Suggestion | None:
        """Suggestion for the consent dialog from the event picker (None = let the controller ask the calendar)."""
        values = list(self.event_combo.cget("values"))
        choice = self.event_var.get()
        if not values or not choice:
            return None
        if choice == MANUAL_EVENT:
            return Suggestion(None, [], "Ohne Termin – bitte Titel und Teilnehmende eingeben.", "manual")
        try:
            ev = self._events[values.index(choice)]
        except (ValueError, IndexError):
            return None
        return Suggestion(ev, ev.participants(), source=self._event_source if self._event_source != "manual" else "graph")

    def toggle_record(self) -> None:
        if self.controller.is_recording:
            self.app.stop_recording()
        elif not self.controller.just_stopped():  # second click of a double click on "stop"
            self.app.start_recording(self.selected_suggestion())
        self._update_record_panel()

    def refresh_calendar(self, force: bool = False) -> None:
        if self.controller.is_recording and not force:
            return
        self._last_calendar = time.monotonic()
        self.event_info.set("Termine werden geladen …")
        self.bg(self.controller.calendar.candidates, self._apply_calendar, "calendar")

    def _apply_calendar(self, res: Any) -> None:
        if isinstance(res, Exception):
            events, message, source = [], f"Kalender nicht verfügbar: {res}", "manual"
        else:
            events, message, source = res
        previous = self.event_var.get()
        self._events = list(events)
        self._event_source = source
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        labels = [event_label(e, now) for e in self._events] + [MANUAL_EVENT]
        self.event_combo.configure(values=labels)
        # pre-select only a real meeting running now (never a blocker/all-day entry or a past meeting)
        is_current = getattr(self.controller.calendar, "is_current", None)
        current = bool(self._events) and (is_current(self._events[0]) if callable(is_current) else True)
        self.event_var.set(previous if previous in labels else (labels[0] if current else MANUAL_EVENT))
        consent = ("Vor dem Start kommt immer die Frage vor der Aufnahme (Titel/Teilnehmende dort änderbar)."
                   if getattr(getattr(self.cfg, "consent", None), "mode", "") == "ask" else
                   "Vor dem Start kommt immer die Einwilligungsabfrage (Titel/Teilnehmende dort änderbar).")
        if not message and self._events:
            message = (f"{len(self._events)} Termin(e) heute" + (", der laufende ist vorausgewählt." if current
                       else " – zurzeit läuft keiner; bitte wählen."))
        self.event_info.set(f"{message} {consent}".strip() if message else consent)

    # ------------------------------------------------------------------
    # recordings list
    # ------------------------------------------------------------------
    def refresh_list(self, rec: Recording | None = None) -> None:
        if rec is not None:
            self.index.invalidate(rec.folder)
        self._last_list_refresh = time.monotonic()
        if self._loading:
            self._reload_pending = True
            return
        self._loading = True

        def partial(infos: list[RecordingInfo]) -> None:
            self.post(lambda: self._apply_infos(infos, final=False))

        self.bg(lambda: self.index.scan(batch=100, on_batch=partial), self._list_loaded, "library")
        plan_fn = getattr(self.app, "retention_plan", None)
        if callable(plan_fn):
            self.bg(plan_fn, lambda p: self.show_retention(None if isinstance(p, Exception) else p), "retention")

    def _list_loaded(self, res: Any) -> None:
        self._loading = False
        if isinstance(res, Exception):
            self.count_var.set(f"Aufnahmen nicht lesbar: {res}")
        else:
            self._apply_infos(res, final=True)
        if self._reload_pending:
            self._reload_pending = False
            self.refresh_list()

    def _filter_key(self) -> str:
        label = self.filter_var.get()
        return next((k for k, v in FILTERS.items() if v == label), "all")

    def _apply_infos(self, infos: list[RecordingInfo], final: bool) -> None:
        self.infos = infos
        self.by_id = {i.id: i for i in infos}
        self.apply_filter(final=final)
        self._fill_compact()
        if self.selected is not None:
            fresh = self.by_id.get(self.selected.id)
            if fresh is not None and final:
                self.show_details(fresh, reload_texts=False)
            elif fresh is None and final:
                self.selected = None
                self._clear_details()

    def apply_filter(self, final: bool = True) -> None:
        shown = filter_infos(self.infos, self.search_var.get(), self._filter_key())
        self._fill_tree(shown)
        total = len(self.infos)
        if not final:
            self.count_var.set(f"{total} Aufnahmen geladen …")
        elif len(shown) == total:
            self.count_var.set(f"{total} Aufnahmen")
        else:
            self.count_var.set(f"{len(shown)} von {total} Aufnahmen")

    @staticmethod
    def row_values(i: RecordingInfo) -> tuple[str, ...]:
        date = f"{i.started_at:%d.%m.%y %H:%M}" if i.started_at else i.folder.name[:10]
        t, s, z, h = stage_cells(i)
        badge = " · Behalten" if i.pinned else (" · Löschen unvollständig" if i.removed_text.startswith(
            "Löschen unvollständig") else " · Nur Zusammenfassung" if i.reduced else
                                                 " · Audio gelöscht" if i.removed else "")
        return (date, i.title + badge, fmt_duration(i.duration_s), fmt_language(i.language), t, s, z, h)

    def _fill_tree(self, shown: list[RecordingInfo]) -> None:
        tree = self.tree
        wanted = [i.id for i in shown]
        wanted_set = set(wanted)
        for iid in tree.get_children():
            if iid not in wanted_set:
                tree.delete(iid)
        existing = set(tree.get_children())
        for pos, info in enumerate(shown):
            values = self.row_values(info)
            if info.id in existing:
                if tuple(tree.item(info.id, "values")) != values:
                    tree.item(info.id, values=values)
                if tree.index(info.id) != pos:
                    tree.move(info.id, "", pos)
            else:
                tree.insert("", pos, iid=info.id, values=values)
        if self.selected is not None and self.selected.id in wanted_set and tuple(tree.selection()) != (self.selected.id,):
            tree.selection_set(self.selected.id)
            tree.see(self.selected.id)

    def _on_select(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        info = self.by_id.get(sel[0])
        if info is None or (self.selected is not None and self.selected.id == info.id):
            return
        self.show_details(info, reload_texts=True)

    def selected_recording(self) -> Recording | None:
        return Recording(self.selected.folder) if self.selected is not None else None

    def select(self, folder_name: str) -> None:
        """Select a recording by folder name (used by tests and after actions)."""
        if self.tree.exists(folder_name):
            self.tree.selection_set(folder_name)
            self.tree.focus(folder_name)
            self.tree.see(folder_name)
            self._on_select()

    # ------------------------------------------------------------------
    # details
    # ------------------------------------------------------------------
    def is_enabled(self, key: str) -> bool:
        return bool(self._enabled.get(key)) and self.selected is not None

    def invoke_action(self, key: str) -> None:
        """Run a details action (button or menu entry) if it is enabled - like a click."""
        if self.is_enabled(key):
            self.action_commands[key]()

    def _clear_details(self) -> None:
        self.d_title.set("Keine Aufnahme ausgewählt")
        self.d_meta.set("Links eine Aufnahme auswählen.")
        self.d_status.set("")
        self.d_handoff.set("")
        self._enabled = {}
        for b in self.buttons.values():
            b.state(["disabled"])
        self.more_btn.state(["disabled"])
        for kind in self.views:
            self._set_text(kind, [("", ())])
            self.views[kind]["combo"].configure(values=[])
            self.views[kind]["var"].set("")

    def show_details(self, info: RecordingInfo, reload_texts: bool = True) -> None:
        self.selected = info
        self.d_title.set(info.title)
        when = (f"{WEEKDAYS[info.started_at.weekday()]}, {info.started_at:%d.%m.%Y, %H:%M} Uhr"
                if info.started_at else info.folder.name)
        names = [p for p in info.participants if p]
        people = ", ".join(names[:3]) + (f" und {len(names) - 3} weitere" if len(names) > 3 else "") if names else "–"
        self.d_meta.set(f"{when} · {fmt_duration(info.duration_s)} · {fmt_language(info.language)} · {people}")
        status = status_description(info)
        due = "Behalten – wird nie automatisch gelöscht." if info.pinned else retention_due_text(info, self.cfg.retention)
        status = " ".join(x for x in (status, due) if x)
        self.d_status.set(status)
        if status:
            self.d_labels[2].grid()
        else:
            self.d_labels[2].grid_remove()
        self.d_handoff.set(handoff_description(info))
        self._update_buttons(info)
        if reload_texts:
            self._text_keys.clear()
        self._load_texts(info)

    def _update_buttons(self, info: RecordingInfo) -> None:
        rec = Recording(info.folder)
        recording_now = self.controller.current is not None and self.controller.current.folder == info.folder
        busy = self.controller.is_busy(rec)
        enabled = action_states(info, recording_now, busy, self.controller.calendar.source is not None)
        self._enabled = enabled
        for key, b in self.buttons.items():
            b.state(["!disabled"] if enabled.get(key) else ["disabled"])
        for key, _text in self.menu_items:
            self.more_menu.entryconfigure(self._menu_index[key], state="normal" if enabled.get(key) else "disabled")
        self.more_menu.entryconfigure(self._menu_index["pin"], label="Nicht mehr behalten" if info.pinned
                                      else "Behalten (nicht automatisch löschen)")
        self.more_btn.state(["!disabled"])
        # the most useful next step stands out by its wording
        self.buttons["review"].configure(
            text=f"Sprecher zuordnen ({info.speakers_unassigned} offen) …" if info.speakers_state() == OPEN
            else "Sprecher zuordnen …")
        self.buttons["handoff"].configure(
            text="Erneut an SharePoint übergeben …" if info.handoff and not info.handoff_current
            else "An SharePoint übergeben …")
        self.actions_frame.relayout()

    def _load_texts(self, info: RecordingInfo) -> None:
        self._detail_token += 1
        token = self._detail_token
        rec = Recording(info.folder)

        def work() -> dict[str, list[VersionChoice]]:
            try:
                versions = rec.load_meta().get("versions") or {}
            except (OSError, ValueError):
                versions = {}
            return {k: version_choices(info.folder, k, list(versions.get(k) or [])) for k in ("summary", "transcript")}

        def done(res: Any) -> None:
            if token != self._detail_token or isinstance(res, Exception):
                return
            for kind, choices in res.items():
                view = self.views[kind]
                labels = [c.label for c in choices]
                keep = view["var"].get()
                view["choices"] = choices
                view["combo"].configure(values=labels)
                if not choices:
                    view["var"].set("")
                    empty = {"summary": "Noch keine Zusammenfassung. " + (info.summary_problem or ""),
                             "transcript": "Noch kein Transkript."}[kind]
                    self._set_text(kind, [(empty.strip(), ("italic",))])
                    self._text_keys.pop(kind, None)
                    continue
                if keep not in labels or self._text_keys.get(kind) is None:
                    view["var"].set(labels[0])
                self._load_version(kind)

        self.bg(work, done, "details")

    def _version_selected(self, kind: str, _event: Any = None) -> None:
        self._load_version(kind)

    def _load_version(self, kind: str) -> None:
        view = self.views[kind]
        label = view["var"].get()
        choice = next((c for c in view["choices"] if c.label == label), None)
        if choice is None:
            return
        try:
            mtime = choice.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        key = (str(choice.path), mtime)
        if self._text_keys.get(kind) == key:
            return
        token = self._detail_token

        def work() -> list[tuple[str, tuple[str, ...]]]:
            return markdown_spans(choice.path.read_text(encoding="utf-8"))

        def done(res: Any) -> None:
            if token != self._detail_token:
                return
            if isinstance(res, Exception):
                self._set_text(kind, [(f"Datei nicht lesbar: {res}", ("italic",))])
                return
            self._text_keys[kind] = key
            self._set_text(kind, res)

        self.bg(work, done, "read-text")

    def _set_text(self, kind: str, spans: list[tuple[str, tuple[str, ...]]]) -> None:
        text: tk.Text = self.views[kind]["text"]
        self.views[kind]["spans"] = spans
        top = text.yview()[0]
        text.configure(state="normal")
        text.delete("1.0", "end")
        for chunk, tags in spans:
            text.insert("end", chunk, tags)
        text.configure(state="disabled")
        text.yview_moveto(top)

    def open_reader(self, kind: str) -> tk.Toplevel | None:
        """The selected summary/transcript in a large window of its own (for small screens)."""
        if self.selected is None:
            return None
        src: tk.Text = self.views[kind]["text"]
        top = tk.Toplevel(self.win)
        label = {"summary": "Zusammenfassung", "transcript": "Transkript"}[kind]
        top.title(f"{label} – {self.selected.title}")
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        top.geometry(f"{min(self.px(900), sw - 40)}x{max(300, sh - self.px(160))}+{self.px(20)}+{self.px(10)}")
        text = tk.Text(top, wrap="word", padx=self.px(14), pady=self.px(10), font="TkTextFont", takefocus=1)
        sb = ttk.Scrollbar(top, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        for tag in src.tag_names():
            if tag != "sel":
                opts = {k: src.tag_cget(tag, k) for k in ("font", "lmargin1", "lmargin2", "spacing1", "spacing3", "justify")}
                text.tag_configure(tag, **{k: v for k, v in opts.items() if v not in ("", None)})
        for chunk, tags in self.views[kind].get("spans", []):
            text.insert("end", chunk, tags)
        text.configure(state="disabled")
        top.bind("<Escape>", lambda e: top.destroy())
        text.focus_set()
        return top

    def text_content(self, kind: str) -> str:
        return str(self.views[kind]["text"].get("1.0", "end-1c"))

    # -- actions with UI of their own -------------------------------------
    def reprocess_selected(self) -> None:
        rec = self.selected_recording()
        if rec is None:
            return
        if messagebox.askyesno(
            "Komplett neu verarbeiten",
            f"„{rec.title()}“ komplett neu verarbeiten?\n\nAudio, Transkription, Sprechertrennung und Zusammenfassung "
            "werden neu berechnet. Es entstehen neue Versionen, die bisherigen bleiben erhalten; manuelle "
            "Sprecherzuordnungen werden übernommen, soweit die Stimmen wiedererkannt werden.", parent=self.win):
            self.app.reprocess(rec)

    def rename_selected(self) -> None:
        rec = self.selected_recording()
        if rec is None:
            return
        title = simpledialog.askstring("Umbenennen", "Neuer Titel der Aufnahme:\n(Der Ordnername bleibt gleich, "
                                       "damit SharePoint die Aufnahme wiedererkennt.)",
                                       initialvalue=rec.title(), parent=self.win)
        if title is None or title.strip() == rec.title():
            return
        try:
            self.app.rename(rec, title)
        except LibraryError as exc:
            messagebox.showerror("Umbenennen nicht möglich", str(exc), parent=self.win)

    def _on_tree_select(self, _event: Any = None) -> None:
        self._on_select()
        self._update_multi()

    def _update_multi(self) -> None:
        n = len(self.tree.selection())
        if n > 1:
            self.multi_btn.configure(text=f"Auswahl löschen ({n}) …")
            self.multi_btn.pack(side="right", padx=self.px(2))
        else:
            self.multi_btn.pack_forget()

    def selected_infos(self) -> list[RecordingInfo]:
        """All recordings selected in the list (Strg/Umschalt+Klick), else the shown one."""
        out = [self.by_id[i] for i in self.tree.selection() if i in self.by_id]
        if not out and self.selected is not None:
            out = [self.selected]
        return out

    def delete_selected(self, scope: str = "recording") -> None:
        """ "Löschen …" (button, menu, Entf, context menu) for the selected recording(s). ``scope``:
        recording | audio_transcript | audio - the same confirmation text as the web UI."""
        infos = self.selected_infos()
        if not infos or (len(infos) == 1 and not self.is_enabled("delete")):
            return
        busy = [i.title for i in infos if self.controller.is_busy(Recording(i.folder))]
        if busy:
            messagebox.showinfo("Löschen nicht möglich", "Wird gerade aufgenommen oder verarbeitet – Löschen ist erst "
                                "danach möglich: " + ", ".join(busy), parent=self.win)
            return
        permanent = scope != "recording" or not recycle_bin_available()
        title, message, _button = delete_question(infos, scope, permanent)
        if not messagebox.askyesno(title, message, icon="warning", parent=self.win):
            return
        errors: list[str] = []
        for info in infos:
            rec = Recording(info.folder)
            try:
                if scope == "recording":
                    try:
                        self.app.delete(rec, permanent=permanent)
                    except LibraryError as exc:
                        # asked for this recording only: the others still go to the recycle bin as confirmed
                        if permanent or "Papierkorb" not in str(exc) or not messagebox.askyesno(
                                "Papierkorb nicht möglich", f"„{info.title}“: {exc}\n\nDiese Aufnahme stattdessen "
                                "ENDGÜLTIG löschen? Das lässt sich nicht rückgängig machen.", icon="warning",
                                parent=self.win):
                            raise
                        self.app.delete(rec, permanent=True)
                else:
                    self.app.remove(rec, scope, permanent=True)
            except (LibraryError, OSError) as exc:
                errors.append(f"{info.title}: {exc}")
        if errors:
            messagebox.showerror("Löschen nicht möglich", "\n".join(errors), parent=self.win)
        if scope == "recording":
            self.selected = None
            self._clear_details()
        self.refresh_all()

    def toggle_pin(self) -> None:
        infos = self.selected_infos()
        if not infos:
            return
        on = not all(i.pinned for i in infos)
        for info in infos:
            self.app.set_pinned(Recording(info.folder), on)
        self.show_message("Behalten: wird nie automatisch gelöscht." if on else
                          "Nicht mehr behalten: wird gelöscht, sobald sie alt genug ist.")
        self.refresh_all()

    def _context_menu(self, event: Any) -> str:
        iid = self.tree.identify_row(getattr(event, "y", 0)) if getattr(event, "y", None) else self.tree.focus()
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
            self._on_select()
        infos = self.selected_infos()
        if not infos:
            return "break"
        m = tk.Menu(self.win, tearoff=False)
        pinned = all(i.pinned for i in infos)
        m.add_command(label="Nicht mehr behalten" if pinned else "Behalten (nicht automatisch löschen)",
                      command=self.toggle_pin)
        m.add_separator()
        if any(i.has_audio for i in infos):
            m.add_command(label="Nur Audio löschen …", command=functools.partial(self.delete_selected, "audio"))
        if any(not i.reduced for i in infos):
            m.add_command(label="Audio und Transkript löschen (Zusammenfassung bleibt) …",
                          command=functools.partial(self.delete_selected, "audio_transcript"))
        m.add_command(label=("Auswahl" if len(infos) > 1 else "Aufnahme") + " vollständig löschen …",
                      command=self.delete_selected)
        x = getattr(event, "x_root", None) or self.tree.winfo_rootx() + 40
        y = getattr(event, "y_root", None) or self.tree.winfo_rooty() + 40
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()
        return "break"

    def show_retention(self, plan: Any) -> None:
        """Pre-notice under the list: what the automatic deletion removes in the next 24 h."""
        from datetime import datetime

        now = datetime.now().astimezone()
        text = plan.notice(now) if plan is not None else ""
        try:  # a refresh also runs while the window is in the tray (after processing, a deletion): not "shown"
            seen = bool(self.visible) and bool(self.win.winfo_viewable())
        except tk.TclError:
            seen = False
        if text and seen:
            from callrec.retention import Plan, mark_surfaced

            mark_surfaced(Plan(plan.enabled, plan.scope, upcoming=plan.next_24h(now)), now)  # shown now
        self._ret_next = [i.id for i in plan.next_24h(now)] if plan is not None else []
        from callrec.retention import BUSY_TEXT

        # "Jetzt löschen": not what is recorded, processed or open in a dialog right now
        self._ret_now = [i.id for i in plan.next_24h(now) if i.waiting != BUSY_TEXT] if plan is not None else []
        warn = [i for i in (plan.next_24h(now) if plan is not None else []) if i.warning]
        if warn:
            text += " – " + "; ".join(f"{i.title}: {i.warning}" for i in warn[:3])
        self.retention_var.set(text)
        for b in (self.ret_keep_btn, self.ret_now_btn):
            if text:
                b.pack(side="right", padx=self.px(2))
            else:
                b.pack_forget()

    def retention_keep(self) -> None:
        for rid in self._ret_next:
            info = self.by_id.get(rid)
            if info is not None:
                self.app.set_pinned(Recording(info.folder), True)
        self.refresh_all()

    def retention_now(self) -> None:
        rt = self.cfg.retention
        infos = [self.by_id[i] for i in getattr(self, "_ret_now", []) if i in self.by_id]
        if not infos:
            return
        title, message, _b = delete_question(infos, rt.scope, rt.delete_mode == "permanent")
        if not messagebox.askyesno(title, message, icon="warning", parent=self.win):
            return
        for info in infos:
            try:
                self.app.remove(Recording(info.folder), rt.scope, permanent=rt.delete_mode == "permanent")
            except (LibraryError, OSError) as exc:
                messagebox.showerror("Löschen nicht möglich", f"{info.title}: {exc}", parent=self.win)
        self.refresh_all()

    # ------------------------------------------------------------------
    # setup checklist
    # ------------------------------------------------------------------
    def run_checks(self, force_audio: bool = False) -> None:
        cfg = self.cfg
        recording = self.controller.is_recording
        previous_audio = self._audio_item
        summary_test = self._summary_test
        resolve: Any = "real"
        if recording or (previous_audio is not None and not force_audio):
            resolve = None  # keep the previous result; never open devices while recording

        def work() -> list[setupcheck.CheckItem]:
            items = setupcheck.run_all(cfg, summary_test, resolve=resolve, autostart=getattr(self.app, "autostart", None))
            if resolve is None:
                items[0] = previous_audio or setupcheck.CheckItem(
                    "audio", "Audiogeräte (Mikrofon und Gegenseite)", setupcheck.UNKNOWN,
                    "Wird während einer Aufnahme nicht geprüft – danach „Erneut prüfen“.")
            return items

        self.setup_summary.set("Prüfe Einrichtung …")
        self.bg(work, self._show_checks, "setup-checks")
        if cfg.summary.enabled and cfg.summary.provider != "anthropic" and summary_test is None:
            self.test_summary()  # local server: free, so test right away

    def _show_checks(self, res: Any) -> None:
        if isinstance(res, Exception):
            self.setup_summary.set(f"Prüfung fehlgeschlagen: {res}")
            return
        items: list[setupcheck.CheckItem] = res
        self._checks = items
        if items and items[0].key == "audio" and items[0].state != setupcheck.UNKNOWN:
            self._audio_item = items[0]
        for child in self.setup_rows.winfo_children():
            child.destroy()
        width = max(self.setup_rows.winfo_width(), self.px(700))
        for row, item in enumerate(items):
            state = setupcheck.STATE_LABEL.get(item.state, item.state)
            if not item.required and item.state == setupcheck.OPTIONAL:
                state = "○ optional"
            ttk.Label(self.setup_rows, text=state, style="Bold.TLabel").grid(
                row=row * 2, column=0, sticky="nw", padx=(0, self.px(10)), pady=(self.px(8), 0))
            ttk.Label(self.setup_rows, text=item.title + ("" if item.required else " (optional)"),
                      style="Bold.TLabel").grid(row=row * 2, column=1, sticky="nw", pady=(self.px(8), 0))
            detail = item.detail
            if item.key == "whisper" and self._download_text:
                detail += "\n" + self._download_text
            lbl = ttk.Label(self.setup_rows, text=detail, wraplength=max(250, width - self.px(420)))
            lbl._detail = True  # type: ignore[attr-defined]
            if item.key == "whisper":
                self._whisper_label = (lbl, item.detail)
            lbl.grid(row=row * 2 + 1, column=1, sticky="nw")
            bar = ttk.Frame(self.setup_rows)
            bar.grid(row=row * 2, column=2, rowspan=2, sticky="ne", pady=(self.px(6), 0))
            for text, key in item.actions:
                ttk.Button(bar, text=text, command=functools.partial(self.setup_action, key)).pack(
                    side="left", padx=self.px(2))
        ready, sentence = setupcheck.summary_line(items)
        self.setup_summary.set(sentence)
        self.nb.tab(self.tab_setup, text="Einrichtung" if ready else "Einrichtung (!)")
        self.compact_setup.set("" if ready else sentence)
        if ready:
            self.compact_setup_row.grid_remove()
        else:
            self.compact_setup_row.grid()
        if not ready and not self.state.tab:
            self.nb.select(self.tab_setup)

    def setup_action(self, key: str) -> None:
        if key == setupcheck.A_TEST_SUMMARY:
            self.test_summary()
        elif key == setupcheck.A_DOWNLOAD_WHISPER:
            self.download_whisper()
        elif key == setupcheck.A_LOGIN:
            self.app.login(done=lambda text: self.post(lambda: self.run_checks()))
        elif key == setupcheck.A_README:
            self.app.open_readme()
        elif key == setupcheck.A_RECHECK:
            self.run_checks(force_audio=True)
        elif key == setupcheck.A_UPDATES:
            self.app.check_updates()
        elif key in (setupcheck.A_AUTOSTART_ON, setupcheck.A_AUTOSTART_OFF):
            self.app.set_autostart(key == setupcheck.A_AUTOSTART_ON)
            self.run_checks()
        elif key.startswith("settings:"):
            self.app.settings(key.split(":", 1)[1])

    def test_summary(self) -> None:
        cfg = self.cfg
        self.setup_summary.set("Teste die Verbindung zum Sprachmodell … (Claude: höchstens ca. 20 s)")

        def done(res: Any) -> None:
            self._summary_test = res if not isinstance(res, Exception) else (str(res), False)
            self.run_checks()

        self.bg(lambda: setupcheck.probe_summary_provider(cfg), done, "summary-test")

    def download_whisper(self) -> None:
        self._download_text = "Download startet …"
        self._show_checks(self._checks)

        def report(text: str) -> None:
            self.post(lambda: self._download_progress(text))

        def done(ok: bool, text: str) -> None:
            def ui() -> None:
                self._download_text = text if not ok else ""
                self.run_checks()
                self.show_message(("Whisper-Modell geladen. " if ok else "Modell-Download fehlgeschlagen: ") + text,
                                  "info" if ok else "error")
            self.post(ui)

        self.app.download_whisper(report, done)

    def _download_progress(self, text: str) -> None:
        self._download_text = text
        if self._whisper_label is not None:
            lbl, base = self._whisper_label
            try:
                lbl.configure(text=f"{base}\n{text}")
            except tk.TclError:
                pass

    def after_settings(self) -> None:
        self._summary_test = None
        self.run_checks(force_audio=True)
        self.refresh_calendar(force=True)
        self.refresh_list()

