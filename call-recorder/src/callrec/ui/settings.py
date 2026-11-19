"""Tabbed settings dialog (all tuning options, presets, LLM model management, SharePoint)."""

from __future__ import annotations

import copy
import logging
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

from callrec import paths
from callrec.config import (
    GRAPH_FILE_SCOPES,
    TIMESTAMP_FORMATS,
    Config,
    ConfigError,
    config_from_dict,
    config_to_dict,
    list_presets,
    save_preset,
)
from callrec.userfiles import PROMPT_FILES, ensure_user_files

log = logging.getLogger(__name__)

PAD: dict[str, Any] = {"padx": 6, "pady": 3}
WHISPER_MODELS = ["large-v3", "large-v3-turbo", "distil-large-v3.5", "medium", "small", "base"]
AUTO_DEVICE = "auto (Windows-Standard für Kommunikation)"
NO_PRESET = "(kein Preset)"


def edit_text_file(parent: tk.Misc, path: Path, title: str, hint: str = "") -> bool:
    """Small editor for the plain tuning files (glossary, replacements, prompts)."""
    ensure_user_files()
    win = tk.Toplevel(parent)
    win.title(title)
    if hint:
        ttk.Label(win, text=hint, wraplength=640, foreground="#444").pack(anchor="w", **PAD)
    txt = tk.Text(win, width=90, height=24, wrap="word", undo=True)
    txt.pack(fill="both", expand=True, **PAD)
    txt.insert("1.0", path.read_text(encoding="utf-8") if path.exists() else "")
    saved = {"v": False}

    def save() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(txt.get("1.0", "end-1c"), encoding="utf-8")
        saved["v"] = True
        win.destroy()

    bar = ttk.Frame(win)
    bar.pack(fill="x", **PAD)
    ttk.Label(bar, text=str(path), foreground="#777").pack(side="left")
    ttk.Button(bar, text="Speichern", command=save).pack(side="right", padx=4)
    ttk.Button(bar, text="Abbrechen", command=win.destroy).pack(side="right", padx=4)
    win.transient(parent.winfo_toplevel())
    win.grab_set()
    parent.wait_window(win)
    return saved["v"]


class SettingsDialog:
    def __init__(
        self,
        root: tk.Misc,
        cfg: Config,
        mic_names: list[str],
        render_names: list[str],
        save: Callable[[Config], None],
        provider_factory: Callable[[Config], Any] | None = None,
    ) -> None:
        self.root = root
        self.cfg = copy.deepcopy(cfg)
        self.save_fn = save
        self.provider_factory = provider_factory
        self.vars: dict[tuple[str, str], tk.Variable] = {}
        self.widgets: dict[tuple[str, str], Any] = {}
        self.saved = False
        self._q: queue.Queue[Callable[[], None]] = queue.Queue()
        self.win = tk.Toplevel(root)
        self.win.title("Einstellungen")
        self.nb = ttk.Notebook(self.win)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)
        self._general(mic_names, render_names)
        self._transcription()
        self._speakers()
        self._rendering()
        self._summary()
        self._handoff()
        bar = ttk.Frame(self.win)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Button(bar, text="Als Preset speichern …", command=self.save_as_preset).pack(side="left")
        ttk.Button(bar, text="Speichern", command=self.do_save).pack(side="right", padx=4)
        ttk.Button(bar, text="Abbrechen", command=self.win.destroy).pack(side="right", padx=4)
        self.win.after(150, self._pump)

    # -- helpers ---------------------------------------------------------
    def _pump(self) -> None:
        try:
            while True:
                self._q.get_nowait()()
        except queue.Empty:
            pass
        if self.win.winfo_exists():
            self.win.after(150, self._pump)

    def _tab(self, title: str) -> ttk.Frame:
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=title)
        f.columnconfigure(1, weight=1)
        return f

    def field(self, tab: ttk.Frame, row: int, label: str, section: str, key: str,
              values: list[str] | None = None, width: int = 50) -> tk.Variable:
        value = getattr(getattr(self.cfg, section), key)
        ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", **PAD)
        var: tk.Variable
        w: Any
        if isinstance(value, bool):
            var = tk.BooleanVar(value=value)
            w = ttk.Checkbutton(tab, variable=var)
            w.grid(row=row, column=1, sticky="w", **PAD)
        else:
            var = tk.StringVar(value=str(value))
            if values is not None:
                w = ttk.Combobox(tab, textvariable=var, values=values, width=width)
            else:
                w = ttk.Entry(tab, textvariable=var, width=width + 3)
            w.grid(row=row, column=1, sticky="we", **PAD)
        self.vars[(section, key)] = var
        self.widgets[(section, key)] = w
        return var

    def collect(self) -> Config:
        """Build and validate a Config from the widgets."""
        data = config_to_dict(self.cfg)
        for (section, key), var in self.vars.items():
            default = getattr(getattr(Config(), section), key)
            raw = var.get()
            if isinstance(default, bool):
                val: Any = bool(raw)
            elif isinstance(default, int):
                try:
                    val = int(str(raw).strip())
                except ValueError:
                    raise ConfigError(f"{section}.{key}: ganze Zahl erwartet") from None
            elif isinstance(default, float):
                try:
                    val = float(str(raw).replace(",", ".").strip())
                except ValueError:
                    raise ConfigError(f"{section}.{key}: Zahl erwartet") from None
            else:
                val = str(raw).strip()
            data[section][key] = val
        g = data["general"]
        if g["preset"] == NO_PRESET:
            g["preset"] = ""
        a = data["audio"]
        for k in ("mic_device", "loopback_device"):
            if a[k] == AUTO_DEVICE:
                a[k] = "auto"
        return config_from_dict(data)

    # -- tabs ------------------------------------------------------------
    def _general(self, mics: list[str], outs: list[str]) -> None:
        t = self._tab("Allgemein")
        mic = self.field(t, 0, "Mikrofon:", "audio", "mic_device", [AUTO_DEVICE, *mics])
        out = self.field(t, 1, "Ausgabegerät (Gegenseite):", "audio", "loopback_device", [AUTO_DEVICE, *outs])
        for v in (mic, out):
            if v.get() == "auto":
                v.set(AUTO_DEVICE)
        self.field(t, 2, "Ihr Name (leer = Microsoft-Konto):", "general", "user_name")
        d = self.field(t, 3, "Aufnahmeordner (leer = Standard):", "general", "recordings_dir")
        ttk.Button(t, text="…", width=3, command=lambda: self._browse(d)).grid(row=3, column=2, **PAD)
        self.field(t, 4, "Tastenkürzel Start/Stopp:", "hotkey", "toggle")
        p = self.field(t, 5, "Aktives Preset:", "general", "preset", [NO_PRESET, *list_presets()])
        if not p.get():
            p.set(NO_PRESET)
        ttk.Label(t, text="Presets enthalten nur Transkriptions-, Sprecher-, Darstellungs- und Zusammenfassungs-"
                  "Einstellungen und liegen als TOML-Dateien in " + str(paths.presets_dir()),
                  wraplength=520, foreground="#555").grid(row=6, column=0, columnspan=3, sticky="w", **PAD)

    def _browse(self, var: tk.Variable) -> None:
        d = filedialog.askdirectory(parent=self.win)
        if d:
            var.set(d)

    def _transcription(self) -> None:
        t = self._tab("Transkription")
        rows = [
            ("Whisper-Modell:", "model", WHISPER_MODELS),
            ("Sprache (de / auto / en …):", "language", ["de", "auto", "en"]),
            ("Rechenwerk:", "device", ["auto", "cuda", "cpu"]),
            ("Beam-Größe (Genauigkeit vs. Tempo):", "beam_size", None),
            ("Zusatz-Prompt (Kontext):", "initial_prompt", None),
            ("Teilnehmernamen als Prompt:", "use_participant_names_as_prompt", None),
            ("Glossar als Hotwords:", "use_glossary", None),
            ("Sprachaktivitätserkennung (VAD):", "vad_filter", None),
            ("VAD-Schwelle (0..1):", "vad_threshold", None),
            ("VAD-Mindestpause (ms):", "vad_min_silence_ms", None),
            ("VAD-Polster (ms):", "vad_speech_pad_ms", None),
            ("Halluzinationsschutz in Pausen (s, 0=aus):", "hallucination_silence_threshold", None),
            ("Vorherigen Text als Kontext:", "condition_on_previous_text", None),
        ]
        for i, (label, key, values) in enumerate(rows):
            self.field(t, i, label, "transcription", key, values)
        bar = ttk.Frame(t)
        bar.grid(row=len(rows), column=0, columnspan=3, sticky="w", **PAD)
        ttk.Button(bar, text="Glossar bearbeiten …", command=lambda: edit_text_file(
            self.win, paths.glossary_file(), "Glossar", "Ein Begriff pro Zeile: Namen, Firmen, Produkte, Fachbegriffe.")).pack(side="left", padx=2)
        ttk.Button(bar, text="Korrekturliste bearbeiten …", command=lambda: edit_text_file(
            self.win, paths.replacements_file(), "Korrekturliste",
            "Eine Regel pro Zeile: 'falsch => richtig' oder 're: Ausdruck => Ersatz'.")).pack(side="left", padx=2)

    def _speakers(self) -> None:
        t = self._tab("Sprecher")
        rows = [
            ("diarization", "cluster_threshold", "Clusterschwelle (kleiner = mehr Sprecher):"),
            ("diarization", "num_speakers", "Feste Sprecheranzahl Gegenseite (0 = auto):"),
            ("diarization", "min_duration_on", "Mindestdauer Sprechabschnitt (s):"),
            ("diarization", "min_duration_off", "Mindestpause zwischen Abschnitten (s):"),
            ("speakers", "match_threshold", "Schwelle Wiedererkennung (0..1):"),
            ("speakers", "invitee_bonus", "Bonus für Eingeladene:"),
            ("speakers", "auto_update_on_match", "Profile bei sicherer Erkennung nachschärfen:"),
            ("speakers", "auto_update_threshold", "… ab Ähnlichkeit:"),
        ]
        for i, (sec, key, label) in enumerate(rows):
            self.field(t, i, label, sec, key)

    def _rendering(self) -> None:
        t = self._tab("Darstellung")
        self.field(t, 0, "Zeitstempel:", "transcript", "timestamp_format", list(TIMESTAMP_FORMATS))
        self.field(t, 1, "Beiträge derselben Person zusammenfassen:", "transcript", "merge_same_speaker")
        self.field(t, 2, "… bis zu einer Pause von (s):", "transcript", "merge_max_gap_s")
        self.field(t, 3, "Neuer Absatz nach Pause (s):", "transcript", "split_pause_s")
        self.field(t, 4, "Maximale Absatzlänge (s):", "transcript", "max_paragraph_s")
        self.field(t, 5, "Korrekturliste anwenden:", "transcript", "apply_replacements")
        ttk.Label(t, text="Geänderte Einstellungen erzeugen beim erneuten Verarbeiten eine neue Version "
                  "(transcript.v2.md …); frühere Versionen bleiben zum Vergleich erhalten.",
                  wraplength=520, foreground="#555").grid(row=6, column=0, columnspan=2, sticky="w", **PAD)

    def _summary(self) -> None:
        from callrec.llm import OLLAMA_UPDATE_HINT

        t = self._tab("Zusammenfassung")
        self.field(t, 0, "Zusammenfassung erstellen:", "summary", "enabled")
        self.field(t, 1, "Anbieter:", "summary", "provider", ["ollama", "openai"])
        self.field(t, 2, "Ollama-Adresse:", "summary", "ollama_url")
        self.field(t, 3, "OpenAI-kompatible Adresse (…/v1):", "summary", "openai_url")
        self.model_var = self.field(t, 4, "Modell:", "summary", "model", [])
        self.model_combo: Any = self.widgets[("summary", "model")]
        self.field(t, 5, "Temperatur:", "summary", "temperature")
        self.field(t, 6, "Kontextlänge (num_ctx, nur Ollama):", "summary", "num_ctx")
        self.status_var = tk.StringVar(value="")
        bar = ttk.Frame(t)
        bar.grid(row=7, column=0, columnspan=2, sticky="w", **PAD)
        ttk.Button(bar, text="Installierte Modelle abfragen", command=self.refresh_models).pack(side="left", padx=2)
        self.pull_btn = ttk.Button(bar, text="Modell laden / aktualisieren", command=self.pull_model)
        self.pull_btn.pack(side="left", padx=2)
        ttk.Button(bar, text="Vorlagen bearbeiten …", command=self.edit_prompts).pack(side="left", padx=2)
        self.progress = ttk.Progressbar(t, mode="determinate", maximum=1.0, length=420)
        self.progress.grid(row=8, column=0, columnspan=2, sticky="we", **PAD)
        ttk.Label(t, textvariable=self.status_var, wraplength=520).grid(row=9, column=0, columnspan=2, sticky="w", **PAD)
        ttk.Label(t, text=OLLAMA_UPDATE_HINT, wraplength=520, foreground="#555").grid(row=10, column=0, columnspan=2, sticky="w", **PAD)
        if self.cfg.summary.allow_remote_endpoint_data_leaves_this_computer:
            ttk.Label(t, text="ACHTUNG: In der Konfiguration sind entfernte KI-Server erlaubt "
                      "(allow_remote_endpoint_data_leaves_this_computer). Gesprächsinhalte können dann "
                      "diesen Rechner verlassen.", foreground="#b00020", wraplength=520).grid(
                row=11, column=0, columnspan=2, sticky="w", **PAD)

    def _provider(self) -> Any:
        cfg = self.collect()
        if self.provider_factory is not None:
            return self.provider_factory(cfg)
        from callrec.llm import make_provider

        return make_provider(cfg.summary)

    def _bg(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        def run() -> None:
            try:
                res: Any = work()
            except Exception as exc:  # shown to the user
                res = exc
            self._q.put(lambda: done(res))

        threading.Thread(target=run, name="settings-bg", daemon=True).start()

    def refresh_models(self) -> None:
        try:
            p = self._provider()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        self.status_var.set("Frage Modelle ab …")

        def done(res: Any) -> None:
            if isinstance(res, Exception):
                self.status_var.set(str(res))
                return
            models, version = res
            self.model_combo.configure(values=models)
            ver = f"Ollama-Version {version}. " if version else ""
            self.status_var.set(f"{ver}{len(models)} Modell(e) installiert: {', '.join(models) or '-'}")

        self._bg(lambda: (p.list_models(), p.version()), done)

    def pull_model(self) -> None:
        model = str(self.model_var.get()).strip()
        try:
            p = self._provider()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        if not p.can_pull:
            self.status_var.set("Dieser Anbieter unterstützt das Laden von Modellen nicht - bitte in dessen Oberfläche laden.")
            return
        if not model or not messagebox.askyesno(
            "Modell laden", f"Modell {model!r} jetzt herunterladen bzw. aktualisieren? (mehrere GB möglich)", parent=self.win
        ):
            return
        self.pull_btn.state(["disabled"])

        def progress(status: str, frac: float | None) -> None:
            def ui() -> None:
                self.status_var.set(status)
                if frac is not None:
                    self.progress.configure(value=frac)

            self._q.put(ui)

        def done(res: Any) -> None:
            self.pull_btn.state(["!disabled"])
            self.status_var.set(str(res) if isinstance(res, Exception) else f"Modell {model} ist aktuell.")
            if not isinstance(res, Exception):
                self.progress.configure(value=1.0)
                self.refresh_models()

        self._bg(lambda: p.pull(model, progress), done)

    def edit_prompts(self) -> None:
        names = {"System-Anweisung": "system", "Protokoll-Vorlage": "final", "Teilzusammenfassung (lange Gespräche)": "chunk"}
        choice = simpledialog.askstring(
            "Vorlage wählen", "Welche Vorlage? " + " / ".join(names), initialvalue="Protokoll-Vorlage", parent=self.win
        )
        if not choice or choice not in names:
            return
        fname = PROMPT_FILES[names[choice]][0]
        edit_text_file(self.win, paths.prompts_dir() / fname, choice,
                       "Platzhalter: {title} {date} {participants} {material_label} {material} bzw. {i} {n} {chunk}.")

    def _handoff(self) -> None:
        t = self._tab("SharePoint")
        ttk.Label(t, text="Übergabe NUR der Zusammenfassung (nie Transkript, Audio oder Stimmprofile) - "
                  "immer nur auf Klick. Dieser Schritt verlässt bewusst den rein lokalen Bereich.",
                  wraplength=520, foreground="#b00020").grid(row=0, column=0, columnspan=3, sticky="w", **PAD)
        self.field(t, 1, "Verfahren:", "handoff", "mode", ["folder", "graph"])
        f = self.field(t, 2, "OneDrive-synchronisierter Ordner:", "handoff", "folder")
        ttk.Button(t, text="…", width=3, command=lambda: self._browse(f)).grid(row=2, column=2, **PAD)
        self.field(t, 3, "Graph: Laufwerks-ID der Bibliothek:", "handoff", "graph_drive_id")
        self.field(t, 4, "Graph: Ordner in der Bibliothek:", "handoff", "graph_folder")
        self.field(t, 5, "Graph: Berechtigung:", "handoff", "graph_scope", list(GRAPH_FILE_SCOPES))
        self.field(t, 6, "E-Mail-Adressen mit übergeben:", "handoff", "include_participant_emails")

    # -- actions ---------------------------------------------------------
    def save_as_preset(self) -> None:
        name = simpledialog.askstring("Preset speichern", "Name des Presets:", parent=self.win)
        if not name:
            return
        try:
            path = save_preset(self.collect(), name)
        except ConfigError as exc:
            messagebox.showerror("Preset", str(exc), parent=self.win)
            return
        messagebox.showinfo("Preset gespeichert", f"{path}\nAktivieren unter Allgemein > Aktives Preset.", parent=self.win)

    def do_save(self) -> None:
        from callrec.hotkey import HotkeyError, parse_hotkey

        try:
            cfg = self.collect()
            parse_hotkey(cfg.hotkey.toggle)
        except (ConfigError, HotkeyError) as exc:
            messagebox.showerror("Ungültige Einstellung", str(exc), parent=self.win)
            return
        self.save_fn(cfg)
        self.cfg = cfg
        self.saved = True
        messagebox.showinfo("Gespeichert", "Einstellungen gespeichert. Sie gelten für neue Verarbeitungen; "
                            "Tastenkürzel und Geräte nach einem Neustart des Programms.", parent=self.win)
        self.win.destroy()

    def run(self) -> bool:
        self.win.attributes("-topmost", True)
        self.win.grab_set()
        self.root.wait_window(self.win)
        return self.saved


def edit_settings(root: tk.Misc, cfg: Config, mic_names: list[str], render_names: list[str],
                  save: Callable[[Config], None], provider_factory: Callable[[Config], Any] | None = None) -> bool:
    return SettingsDialog(root, cfg, mic_names, render_names, save, provider_factory).run()


__all__ = ["SettingsDialog", "edit_settings", "edit_text_file"]
