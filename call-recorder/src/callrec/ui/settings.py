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
WHISPER_MODELS = ["auto", "large-v3", "large-v3-turbo", "distil-large-v3.5", "medium", "small", "base"]
PROVIDERS = ["ollama", "openai", "anthropic"]
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
        gpu_status_fn: Callable[[bool], Any] | None = None,
        anthropic_factory: Callable[[Config, str], Any] | None = None,
    ) -> None:
        self.root = root
        self.gpu_status_fn = gpu_status_fn
        self.anthropic_factory = anthropic_factory
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
        self._language_quality()
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

    def field(self, tab: ttk.Frame | ttk.Labelframe, row: int, label: str, section: str, key: str,
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
            var = tk.StringVar(value=", ".join(value) if isinstance(value, list) else str(value))
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
            elif isinstance(default, list):
                val = [x.strip() for x in str(raw).replace(";", ",").split(",") if x.strip()]
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
            ("Sprache (auto = je Aufnahme erkennen; de/en = erzwingen):", "language", ["auto", "de", "en"]),
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
        self.gpu_btn = ttk.Button(bar, text="GPU erneut prüfen", command=lambda: self.refresh_gpu(True))
        self.gpu_btn.pack(side="left", padx=2)
        self.effective_var = tk.StringVar(value="Effektiv: wird ermittelt …")
        ttk.Label(t, textvariable=self.effective_var, wraplength=520, foreground="#555").grid(
            row=len(rows) + 1, column=0, columnspan=3, sticky="w", **PAD)
        ttk.Label(t, text="Modell „auto“: large-v3 auf einer NVIDIA-GPU, ohne GPU large-v3-turbo (int8). "
                  "Der Echtzeitfaktor jeder Transkription steht in meta.json und im Protokoll.",
                  wraplength=520, foreground="#555").grid(row=len(rows) + 2, column=0, columnspan=3, sticky="w", **PAD)
        self._installed_models(t, len(rows) + 3)
        self._gpu: Any = None
        for key in ("model", "device", "compute_type"):
            var = self.vars.get(("transcription", key))
            if var is not None:
                var.trace_add("write", lambda *a: self._show_effective())
        self.refresh_gpu(False)

    def _language_quality(self) -> None:
        t = self._tab("Sprache & Qualität")
        rows = [
            ("Mögliche Sprachen (Kommas; erste = bei Zweifel):", "transcription", "language_candidates"),
            ("Andere Sprache erst ab Wahrscheinlichkeit (0,5–1):", "transcription", "language_switch_threshold"),
            ("Mindestens so viel Sprache für einen Wechsel (s):", "transcription", "language_min_speech_s"),
            ("Messabschnitte je Spur (je 30 s):", "transcription", "language_detection_windows"),
            ("Sprachwechsel innerhalb einer Spur (experimentell):", "transcription", "multilingual"),
            ("Schleifenschutz: max. Kompressionsrate (0 = aus):", "transcription", "compression_ratio_threshold"),
            ("Schleifenschutz: min. log-Wahrscheinlichkeit (0 = aus):", "transcription", "log_prob_threshold"),
            ("Stille-Schwelle no_speech (0 = aus):", "transcription", "no_speech_threshold"),
            ("Neu dekodieren mit höherer Temperatur:", "transcription", "temperature_fallback"),
            ("Wiederholungs-Strafe (1 = aus):", "transcription", "repetition_penalty"),
            ("Wiederholte n-Gramme sperren (0 = aus):", "transcription", "no_repeat_ngram_size"),
            ("Warnung ab Wiederholungen in Folge (0 = aus):", "transcription", "repetition_warn_min_repeats"),
            ("Sprache der Zusammenfassung:", "summary", "language"),
        ]
        for i, (label, sec, key) in enumerate(rows):
            self.field(t, i, label, sec, key, ["de", "en"] if key == "language" else None)
        ttk.Label(t, text="Bei Sprache „auto“ wird je Aufnahme an mehreren Stellen beider Spuren gemessen; die erste "
                  "Sprache der Liste gilt, solange keine andere eindeutig erkannt wird. Ergebnis und Wahrscheinlichkeiten "
                  "stehen im Transkriptkopf und in meta.json. Die Zusammenfassung wird immer in der eingestellten "
                  "Sprache geschrieben. „Sprachwechsel innerhalb einer Spur“ nutzt die Erkennung von faster-whisper je "
                  "30-s-Abschnitt unter allen Sprachen (nicht nur der Liste) – nur bei echten Mischgesprächen.",
                  wraplength=520, foreground="#555").grid(row=len(rows), column=0, columnspan=3, sticky="w", **PAD)

    def _installed_models(self, tab: ttk.Frame, row: int) -> None:
        f = ttk.LabelFrame(tab, text="Installierte Whisper-Modelle")
        f.grid(row=row, column=0, columnspan=3, sticky="we", padx=6, pady=6)
        f.columnconfigure(0, weight=1)
        self.models_list = tk.Listbox(f, height=4, width=80)
        self.models_list.grid(row=0, column=0, columnspan=2, sticky="we", **PAD)
        self.models_info = tk.StringVar(value="wird ermittelt …")
        ttk.Label(f, textvariable=self.models_info, foreground="#555", wraplength=520).grid(
            row=1, column=0, sticky="w", **PAD)
        ttk.Button(f, text="Ausgewähltes Modell löschen …", command=self.remove_selected_model).grid(
            row=1, column=1, sticky="e", **PAD)
        self._models: list[Any] = []
        self.refresh_installed_models()

    def _models_root(self) -> Path:
        return paths.models_dir() / "whisper"

    def refresh_installed_models(self) -> None:
        from callrec.asr.download import fmt_bytes
        from callrec.asr.modelcache import installed_models

        root = self._models_root()

        def done(res: Any) -> None:
            self.models_list.delete(0, "end")
            if isinstance(res, Exception):
                self.models_info.set(f"Modellordner nicht lesbar: {res}")
                return
            self._models = list(res)
            for m in self._models:
                self.models_list.insert("end", m.describe())
            total = sum(m.size for m in self._models)
            self.models_info.set(f"{len(self._models)} Modell(e), zusammen {fmt_bytes(total)} in {root}"
                                 if self._models else f"Keine Whisper-Modelle in {root}")

        self._bg(lambda: installed_models(root), done)

    def remove_selected_model(self) -> None:
        from callrec.asr.download import fmt_bytes
        from callrec.asr.modelcache import remove_model

        sel = self.models_list.curselection()
        if not sel or sel[0] >= len(self._models):
            self.models_info.set("Bitte zuerst ein Modell in der Liste auswählen.")
            return
        m = self._models[sel[0]]
        if not messagebox.askyesno("Modell löschen", f"{m.name} ({fmt_bytes(m.size)}) löschen? Wird es noch "
                                   "verwendet, lädt CallRecorder es bei der nächsten Transkription erneut.",
                                   parent=self.win):
            return
        try:
            remove_model(m, self._models_root())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Modell löschen", f"Löschen fehlgeschlagen (wird das Modell gerade verwendet?): {exc}",
                                 parent=self.win)
        self.refresh_installed_models()

    def _gpu_fn(self) -> Callable[[bool], Any]:
        if self.gpu_status_fn is not None:
            return self.gpu_status_fn
        from callrec.asr.gpu import gpu_status

        return lambda force: gpu_status(force=force)

    def _show_effective(self) -> None:
        from callrec.asr.gpu import describe_effective
        from callrec.config import TranscriptionConfig

        tc = TranscriptionConfig(
            model=str(self.vars[("transcription", "model")].get()).strip() or "auto",
            device=str(self.vars[("transcription", "device")].get()).strip() or "auto",
            compute_type=self.cfg.transcription.compute_type,
        )
        self.effective_var.set(describe_effective(tc, self._gpu))

    def refresh_gpu(self, force: bool) -> None:
        fn = self._gpu_fn()
        if force:
            self.effective_var.set("Prüfe GPU …")
            self.gpu_btn.state(["disabled"])

        def done(res: Any) -> None:
            self.gpu_btn.state(["!disabled"])
            if isinstance(res, Exception):
                self.effective_var.set(f"GPU-Prüfung fehlgeschlagen: {res}")
                return
            self._gpu = res
            self._show_effective()

        self._bg(lambda: fn(force), done)

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
        self.field(t, 1, "Anbieter:", "summary", "provider", PROVIDERS)
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
        self._anthropic_frame(t, 12)

    def _anthropic_frame(self, tab: ttk.Frame, row: int) -> None:
        from callrec.llm_anthropic import DEFAULT_MODEL, MODEL_CHOICES

        f = ttk.LabelFrame(tab, text="Claude (Anthropic-API) – nur bei Anbieter „anthropic“")
        f.grid(row=row, column=0, columnspan=3, sticky="we", padx=6, pady=6)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Bei diesem Anbieter wird das TRANSKRIPT an Anthropic übertragen (verlässt diesen Rechner). "
                  "Kosten pro Token über Ihr Anthropic-Konto.", foreground="#b00020", wraplength=500).grid(
            row=0, column=0, columnspan=3, sticky="w", **PAD)
        self.field(f, 1, "Claude-Modell:", "summary", "anthropic_model", list(MODEL_CHOICES), width=40)
        self.anthropic_model_info = tk.StringVar()
        ttk.Label(f, textvariable=self.anthropic_model_info, foreground="#555", wraplength=500).grid(
            row=2, column=0, columnspan=3, sticky="w", **PAD)
        mvar = self.vars[("summary", "anthropic_model")]

        def show_model(*_a: Any) -> None:
            m = str(mvar.get()).strip()
            info = MODEL_CHOICES.get(m, "eigene Modell-ID (nicht geprüft)")
            self.anthropic_model_info.set(f"{info}" + (" – Standard" if m == DEFAULT_MODEL else ""))

        mvar.trace_add("write", show_model)
        show_model()
        self.field(f, 3, "Denktiefe (effort):", "summary", "anthropic_effort", ["low", "medium", "high", "xhigh", "max", ""], width=12)
        ttk.Label(f, text="API-Schlüssel:").grid(row=4, column=0, sticky="w", **PAD)
        self.key_var = tk.StringVar(value="")
        ttk.Entry(f, textvariable=self.key_var, show="•", width=44).grid(row=4, column=1, sticky="we", **PAD)
        kb = ttk.Frame(f)
        kb.grid(row=5, column=0, columnspan=3, sticky="w", **PAD)
        ttk.Button(kb, text="Schlüssel speichern", command=self.save_key).pack(side="left", padx=2)
        ttk.Button(kb, text="Schlüssel löschen", command=self.delete_key).pack(side="left", padx=2)
        self.test_btn = ttk.Button(kb, text="Verbindung testen", command=self.test_anthropic)
        self.test_btn.pack(side="left", padx=2)
        self.key_status = tk.StringVar()
        ttk.Label(f, textvariable=self.key_status, wraplength=500).grid(row=6, column=0, columnspan=3, sticky="w", **PAD)
        self._show_key_status()

    def _show_key_status(self, extra: str = "") -> None:
        from callrec import apikey

        key, source = apikey.load_key()
        state = f"Schlüssel: {apikey.mask(key)} ({source})" if key else "Kein Schlüssel hinterlegt (Anthropic-Konsole > API Keys)."
        self.key_status.set(state + (f"\n{extra}" if extra else ""))

    def save_key(self) -> None:
        from callrec import apikey

        try:
            apikey.store_key(self.key_var.get())
        except apikey.ApiKeyError as exc:
            messagebox.showerror("API-Schlüssel", str(exc), parent=self.win)
            return
        self.key_var.set("")
        self._show_key_status("Gespeichert (Windows-DPAPI-verschlüsselt, nicht in config.toml).")

    def delete_key(self) -> None:
        from callrec import apikey

        apikey.delete_key()
        self._show_key_status("Gespeicherter Schlüssel gelöscht.")

    def test_anthropic(self) -> None:
        try:
            cfg = self.collect()
        except ConfigError as exc:
            self.key_status.set(str(exc))
            return
        typed = self.key_var.get().strip()
        if self.anthropic_factory is not None:
            provider = self.anthropic_factory(cfg, typed)
        else:
            from callrec import apikey
            from callrec.llm_anthropic import AnthropicProvider

            key = typed or apikey.load_key()[0]
            from callrec.llm_anthropic import TEST_TIMEOUT_S

            provider = AnthropicProvider(key, cfg.summary.anthropic_model, effort=cfg.summary.anthropic_effort,
                                         max_attempts=1, timeout_s=TEST_TIMEOUT_S)
        self.test_btn.state(["disabled"])
        self.key_status.set("Teste Verbindung zu api.anthropic.com … (sendet nur „Antworte nur mit: OK“, "
                            "höchstens ca. 20 s)")

        def done(res: Any) -> None:
            self.test_btn.state(["!disabled"])
            self._show_key_status(str(res))

        self._bg(provider.test_connection, done)

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
        if not self.confirm_cloud(cfg):
            return
        self.save_fn(cfg)
        self.cfg = cfg
        self.saved = True
        messagebox.showinfo("Gespeichert", "Einstellungen gespeichert. Sie gelten für neue Verarbeitungen; "
                            "Tastenkürzel und Geräte nach einem Neustart des Programms.", parent=self.win)
        self.win.destroy()

    def confirm_cloud(self, cfg: Config) -> bool:
        """Selecting Claude (Anthropic) needs an explicit confirmation; switching away revokes it."""
        from callrec.llm_anthropic import ANTHROPIC_SETTINGS_WARNING

        s = cfg.summary
        if s.provider != "anthropic":
            s.anthropic_transcripts_leave_this_computer_confirmed = False
            return True
        if s.anthropic_transcripts_leave_this_computer_confirmed:
            return True
        if not messagebox.askyesno("Transkripte an Anthropic senden?", ANTHROPIC_SETTINGS_WARNING,
                                   icon="warning", parent=self.win):
            return False
        s.anthropic_transcripts_leave_this_computer_confirmed = True
        return True

    def run(self) -> bool:
        self.win.attributes("-topmost", True)
        self.win.grab_set()
        self.root.wait_window(self.win)
        return self.saved


def edit_settings(root: tk.Misc, cfg: Config, mic_names: list[str], render_names: list[str],
                  save: Callable[[Config], None], provider_factory: Callable[[Config], Any] | None = None) -> bool:
    return SettingsDialog(root, cfg, mic_names, render_names, save, provider_factory).run()


__all__ = ["SettingsDialog", "edit_settings", "edit_text_file"]
