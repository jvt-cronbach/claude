"""``callrec benchmark``: measure on THIS PC which thread layout and profile transcribe fastest.

A short clip (default 90 s, the part of an existing recording with the most speech) runs through
the candidate configurations in one worker process:

* thread layouts: CTranslate2's own default (1 lane x 4 threads = up to 0.3), 1 lane x all
  physical cores, 2 lanes x half the cores (3 lanes from 9 cores), and - from 6 cores - the
  layout that is left while the speaker separation runs alongside (cores - 2);
* profile "Ausgewogen" on every layout, then "Schnell", "Genau" and "Ausgewogen" in batches on
  the fastest layout. Agreement with "Genau" (same words in the same order) shows what the
  faster profiles cost on this audio.

The speaker separation is measured on the same clip (2 threads and the automatic count). The
result is stored in ``benchmark.json``; with automatic settings the app then uses the fastest
layout and decides from these numbers whether the speaker separation runs in parallel. Nothing
of the clip's content is stored - only timings and agreement figures.
"""

from __future__ import annotations

import difflib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from callrec import paths

log = logging.getLogger(__name__)

DEFAULT_SECONDS = 90.0
CT2_DEFAULT_THREADS = 4  # CTranslate2 4.9.0 with intra_threads=0 and no OMP_NUM_THREADS: min(4, cores)
DIAR_PARALLEL_THREADS = 2
# rough shape of a call for the parallel/sequential decision: Whisper works on the speech of both
# tracks (~0.9 x call length together), the speaker separation on the whole remote track (it is
# measured on dense speech here, a real remote track has pauses: ~0.7)
SPEECH_SHARE = 0.9
DIAR_SHARE = 0.7


def result_file() -> Path:
    return paths.data_dir() / "benchmark.json"


def load_result(path: Path | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads((path or result_file()).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def load_tuned(path: Path | None = None) -> dict[str, Any] | None:
    """{"cores", "lanes", "threads"} of the fastest layout measured on this PC (None = not measured)."""
    data = load_result(path)
    tuned = (data or {}).get("tuned")
    return dict(tuned) if isinstance(tuned, dict) else None


def parallel_decision(path: Path | None = None) -> bool | None:
    data = load_result(path)
    v = (data or {}).get("parallel_diarization")
    return v if isinstance(v, bool) else None


# -- candidates and choice (pure) -----------------------------------------------------------------
def thread_configs(cores: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(lanes: int, threads: int, label: str, budget: int) -> None:
        if threads >= 1 and not any(c["lanes"] == lanes and c["threads"] == threads for c in out):
            out.append({"lanes": lanes, "threads": threads, "label": label, "budget": budget})

    add(1, min(CT2_DEFAULT_THREADS, cores), "bisher (Standard von CTranslate2)", cores)
    add(1, cores, "1 Spur mit allen Kernen", cores)
    if cores >= 4:
        add(2, cores // 2, "2 parallele Spuren", cores)
    if cores >= 9:
        add(3, cores // 3, "3 parallele Spuren", cores)
    if cores >= 6:
        rest = cores - DIAR_PARALLEL_THREADS
        add(2, rest // 2, "2 Spuren neben der Sprechertrennung", rest)
    return out


def word_agreement(a: list[str], b: list[str]) -> float:
    if not a and not b:
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 4)


def choose(result: dict[str, Any], cores: int) -> dict[str, Any]:
    """Fastest full-PC layout, the reduced layout for parallel stages, and the parallel decision."""
    runs = [r for r in result.get("runs") or [] if r.get("profile") == "ausgewogen" and not r.get("batched")]
    full = [r for r in runs if int(r.get("budget", cores)) == cores]
    best = min(full, key=lambda r: r["rtf"]) if full else None
    reduced = [r for r in runs if int(r.get("budget", cores)) < cores]
    out: dict[str, Any] = {}
    if best is not None:
        out["tuned"] = {"cores": cores, "lanes": int(best["lanes"]), "threads": int(best["threads"]),
                        "rtf": best["rtf"]}
        base = next((r for r in full if r.get("label", "").startswith("bisher")), None)
        if base is not None and best["rtf"] > 0:
            out["speedup_vs_before"] = round(base["rtf"] / best["rtf"], 2)
    diar = result.get("diarization") or {}
    if best is not None and reduced and diar.get("rtf_parallel") and diar.get("rtf_full"):
        w_full = best["rtf"] * SPEECH_SHARE
        w_red = min(r["rtf"] for r in reduced) * SPEECH_SHARE
        seq = w_full + diar["rtf_full"] * DIAR_SHARE
        par = max(w_red, diar["rtf_parallel"] * DIAR_SHARE)
        out["parallel_diarization"] = par < 0.95 * seq
        out["estimate_rtf"] = {"sequential": round(seq, 3), "parallel": round(par, 3)}
    elif best is not None and diar.get("rtf_full"):
        out["parallel_diarization"] = False
        out["estimate_rtf"] = {"sequential": round(best["rtf"] * SPEECH_SHARE + diar["rtf_full"] * DIAR_SHARE, 3)}
    return out


def fmt_rtf(rtf: float | None) -> str:
    return "–" if rtf is None else f"{rtf:.2f}".replace(".", ",")


def report_lines(result: dict[str, Any]) -> list[str]:
    """German summary (CLI output and settings page)."""
    from callrec.asr.profiles import label

    lines: list[str] = []
    clip = result.get("clip") or {}
    lines.append(f"Messung vom {result.get('at', '')[:16].replace('T', ' ')}: {clip.get('seconds', 0):.0f} s Ausschnitt "
                 f"({clip.get('speech_s', 0):.0f} s Sprache), Modell {result.get('model', '')}, "
                 f"{result.get('cores', '?')} physische Kerne")
    if result.get("power_warning"):
        lines.append(f"Achtung: {result['power_warning']}")
    lines.append("Echtzeitfaktor = Rechenzeit / Länge des Ausschnitts (kleiner ist schneller):")
    for r in result.get("runs") or []:
        prof = label(str(r.get("profile", ""))) + (" (in Stapeln)" if r.get("batched") and r.get("profile") != "schnell" else "")
        agree = r.get("agreement")
        extra = (f", Übereinstimmung mit „Genau“ {agree * 100:.1f} %".replace(".", ",")
                 if isinstance(agree, float) and r.get("profile") != "genau" else "")
        lines.append(f"  {prof}, {r.get('label', '')} ({r['lanes']}×{r['threads']} Threads): {fmt_rtf(r.get('rtf'))}{extra}")
    d = result.get("diarization") or {}
    if d:
        lines.append(f"  Sprechertrennung: {fmt_rtf(d.get('rtf_full'))} mit {d.get('threads_full')} Threads, "
                     f"{fmt_rtf(d.get('rtf_parallel'))} mit {d.get('threads_parallel')} Threads")
    tuned = result.get("tuned") or {}
    if tuned:
        lines.append(f"Gewählt: {tuned['lanes']} Spur(en) × {tuned['threads']} Threads"
                     + (f" – {result['speedup_vs_before']:.1f}× so schnell wie bisher".replace(".", ",")
                        if result.get("speedup_vs_before") else ""))
    if result.get("not_applied"):
        lines.append(f"Ergebnis nicht übernommen ({result['not_applied'].get('why', '')}) – die automatische "
                     "Einstellung bleibt; bitte mit Netzteil und ohne laufende Aufnahme erneut messen.")
    if "parallel_diarization" in result:
        lines.append("Sprechertrennung: " + ("gleichzeitig mit der Transkription (schneller gemessen)"
                                             if result["parallel_diarization"] else "nach der Transkription"))
    est = (result.get("estimate_rtf") or {})
    if est:
        best = min(v for v in est.values())
        lines.append(f"Geschätzt für ein Gespräch von 60 min: etwa {best * 60:.0f} min für Transkription und "
                     "Sprechertrennung (ohne Zusammenfassung).")
    return lines


# -- worker side -----------------------------------------------------------------------------------
def densest_window(chunks: list[dict[str, int]], seconds: float, sr: int = 16000) -> tuple[int, float]:
    """(start sample, speech seconds) of the ``seconds`` window with the most VAD speech."""
    win = int(seconds * sr)
    best = (0, 0.0)
    for c in chunks:
        start = int(c["start"])
        end = start + win
        speech = sum(max(0, min(int(d["end"]), end) - max(int(d["start"]), start)) for d in chunks) / sr
        if speech > best[1]:
            best = (start, speech)
    return best


def clip_chunks(chunks: list[dict[str, int]], start: int, end: int) -> list[dict[str, int]]:
    return [{"start": max(int(c["start"]), start), "end": min(int(c["end"]), end)}
            for c in chunks if int(c["end"]) > start and int(c["start"]) < end]


def worker_main(job_path: str, out_path: str, model_factory: Any = None) -> int:
    """``python -m callrec.asr.worker --benchmark JOB --out RESULT``."""
    from callrec.asr import worker as w
    from callrec.asr.session import LaneGate, decoding_kwargs, plan_pieces, run_lanes, transcribe_piece

    w.lower_priority()
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    if model_factory is None:
        from faster_whisper import WhisperModel

        model_factory = WhisperModel
    w.disable_onnxruntime_telemetry()
    seconds = float(job.get("seconds", DEFAULT_SECONDS))
    profiles: dict[str, dict[str, Any]] = job["profiles"]
    w.emit_safe("STAGE prepare")
    samples: dict[str, Any] = {t: w._decode_track(p) for t, p in job["tracks"].items()}
    base_vad = profiles["ausgewogen"]["vad"]
    best_clip: tuple[str, int, float] = ("", 0, -1.0)
    for t, x in samples.items():
        start, speech = densest_window(w._vad_chunks(x, base_vad), seconds)
        if speech > best_clip[2]:
            best_clip = (t, start, speech)
    track, start, speech = best_clip
    end = min(len(samples[track]), start + int(seconds * 16000))
    x = samples[track]
    vad_cache: dict[str, list[dict[str, int]]] = {}

    def chunks_for(dec: dict[str, Any]) -> list[dict[str, int]]:
        key = json.dumps(dec["vad"], sort_keys=True)
        if key not in vad_cache:
            vad_cache[key] = clip_chunks(w._vad_chunks(x[start:end], dec["vad"]), 0, end - start)
            vad_cache[key] = [{"start": c["start"] + start, "end": c["end"] + start} for c in vad_cache[key]]
        return vad_cache[key]

    runs: list[dict[str, Any]] = []
    texts: dict[int, list[str]] = {}
    language = str(job.get("language") or "de")
    total_runs = len(job["configs"]) + 3
    loaded: dict[str, Any] = {}

    def model_for(cfg: dict[str, Any]) -> Any:
        key = f"{cfg['lanes']}x{cfg['threads']}"
        if key not in loaded:
            loaded.clear()
            kwargs: dict[str, Any] = {"num_workers": int(cfg["lanes"])}
            if job.get("device", "cpu") == "cpu":
                kwargs["cpu_threads"] = int(cfg["threads"])
            loaded[key] = w.load_model(model_factory, str(job["model"]), str(job.get("device", "cpu")),
                                       w.resolve_compute_type(str(job.get("device", "cpu")), str(job.get("compute_type") or "auto")),
                                       job.get("download_root") or None, **kwargs)
        return loaded[key]

    def one_run(cfg: dict[str, Any], profile: str, batched: bool) -> None:
        dec = dict(profiles[profile])
        dec["batched"] = batched or bool(dec.get("batched"))
        lanes = int(cfg["lanes"])
        model = model_for(cfg)
        if dec["batched"]:
            from faster_whisper import BatchedInferencePipeline

            engines = [BatchedInferencePipeline(model) for _ in range(lanes)]
        else:
            engines = [model] * lanes
        pieces = plan_pieces({track: chunks_for(dec)}, lanes, min_s=5.0, max_s=max(5.0, seconds / (2 * lanes)))
        kwargs = decoding_kwargs(dec, dict(job.get("options") or {}), dec["batched"])
        results: dict[int, list[dict[str, Any]]] = {}
        w.emit_safe(f"BENCHSTART {json.dumps({'label': cfg['label'], 'profile': profile, 'batched': dec['batched']})}")
        t0 = time.monotonic()

        def work(lane: int, piece: Any) -> float:
            results[piece.index] = transcribe_piece(engines[lane], x, piece, language, kwargs, dec["batched"])
            return 0.0

        run_lanes(pieces, lanes, LaneGate(lanes), work)
        wall = time.monotonic() - t0
        words = [wd["text"].strip().lower() for i in sorted(results) for s in results[i] for wd in s["words"]]
        run = {"label": cfg["label"], "lanes": lanes, "threads": int(cfg["threads"]), "budget": int(cfg["budget"]),
               "profile": profile, "batched": dec["batched"], "wall_s": round(wall, 2),
               "rtf": round(wall / max(1e-6, (end - start) / 16000), 3), "words": len(words)}
        texts[len(runs)] = words
        runs.append(run)
        w.emit_safe(f"BENCH {json.dumps(run)}")
        w.emit_safe(f"PROGRESS {len(runs) / total_runs:.4f}")

    w.emit_safe("STAGE transcribe")
    for cfg in job["configs"]:
        one_run(cfg, "ausgewogen", False)
    full = [r for r in runs if r["budget"] == max(c["budget"] for c in job["configs"])]
    best = min(full, key=lambda r: r["rtf"])
    best_cfg = next(c for c in job["configs"] if c["lanes"] == best["lanes"] and c["threads"] == best["threads"])
    one_run(best_cfg, "schnell", True)
    one_run(best_cfg, "ausgewogen", True)
    one_run(best_cfg, "genau", False)
    ref = texts[len(runs) - 1]
    for i, r in enumerate(runs):
        r["agreement"] = word_agreement(texts[i], ref)
    result = {"clip": {"track": track, "start_s": round(start / 16000, 1), "seconds": round((end - start) / 16000, 1),
                       "speech_s": round(speech, 1)}, "language": language, "runs": runs}
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


# -- parent side -----------------------------------------------------------------------------------
def pick_recording(root: Path) -> Path | None:
    """The newest recording with both finished tracks."""
    from callrec.audio.finalize import find_audio

    if not root.exists():
        return None
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        if find_audio(folder) is not None:
            return folder
    return None


def run_benchmark(cfg: Any, folder: Path | None = None, seconds: float = DEFAULT_SECONDS,
                  progress: Callable[[float, str], None] = lambda f, m: None,
                  diarize: Callable[[Path, int], float] | None = None, transcriber: Any = None,
                  save: bool = True) -> dict[str, Any]:
    """Run the measurement (several minutes) and store it; returns the result with the choice.

    ``diarize(clip_wav, threads) -> seconds`` measures the speaker separation (None = skipped)."""
    import soundfile as sf

    from callrec.asr.profiles import PROFILES
    from callrec.asr.transcriber import TranscriptionError, WhisperSubprocessTranscriber, WorkerOutput
    from callrec.audio.finalize import find_audio
    from callrec.power import power_state
    from callrec.speakers.threads import effective_threads, physical_cores

    folder = folder or pick_recording(cfg.recordings_path())
    fa = find_audio(folder) if folder is not None else None
    if fa is None:
        raise FileNotFoundError("Keine Aufnahme mit fertigem Audio gefunden – für die Messung wird eine bestehende "
                                "Aufnahme gebraucht (zuerst ein kurzes Gespräch aufnehmen).")
    tr = transcriber or WhisperSubprocessTranscriber(cfg.transcription, paths.models_dir() / "whisper")
    cores = physical_cores()
    device = "cpu"
    work = paths.data_dir() / "benchmark"
    work.mkdir(parents=True, exist_ok=True)
    lang = cfg.transcription.language if cfg.transcription.language != "auto" else cfg.transcription.language_candidates[0]
    t = cfg.transcription
    job = {"model": tr.plan().model_for(device), "device": device, "compute_type": "auto",
           "download_root": str(tr.download_root), "seconds": seconds, "language": lang,
           "tracks": {"mic": str(fa.mic), "remote": str(fa.remote)}, "configs": thread_configs(cores),
           "profiles": {k: v.to_dict() for k, v in PROFILES.items()},
           "options": {"compression_ratio_threshold": t.compression_ratio_threshold,
                       "log_prob_threshold": t.log_prob_threshold, "no_speech_threshold": t.no_speech_threshold,
                       "repetition_penalty": t.repetition_penalty, "no_repeat_ngram_size": t.no_repeat_ngram_size}}
    job_path, out_path = work / "job.json", work / "result.json"
    job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    if out_path.exists():
        out_path.unlink()
    lines: list[str] = []

    def on_progress(f: float, msg: str = "") -> None:
        progress(f, msg)

    class _Out(WorkerOutput):
        def feed(self, line: str) -> None:
            if line.startswith("BENCHSTART "):
                try:
                    d = json.loads(line.split(" ", 1)[1])
                    on_progress(-1.0, f"misst {d['profile']} – {d['label']}")
                except (ValueError, KeyError):
                    pass
                return
            if line.startswith("BENCH "):
                lines.append(line)
                return
            super().feed(line)

    aborted: list[str] = []

    def control(proc: Any) -> None:
        """A recording, a call or a processing job that starts meanwhile stops the measurement: it needs
        the CPU (the recording must not lose audio) and the numbers would be wrong anyway."""
        while proc.poll() is None:
            time.sleep(BUSY_POLL_S)
            why = interference(proc)
            if why:
                aborted.append(why)
                try:
                    proc.kill()
                except OSError:
                    pass
                return

    started = time.monotonic()
    try:
        rc = tr._run(tr.command_prefix + ["--benchmark", str(job_path), "--out", str(out_path)], on_progress,
                     work / "benchmark.log", _Out(on_progress), control=control)
    finally:
        job_path.unlink(missing_ok=True)  # names the recording's files
    if aborted:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(f"Messung abgebrochen: {aborted[0]} – nichts gespeichert, bitte später erneut messen.")
    if rc != 0 or not out_path.exists():
        raise TranscriptionError(f"Messung fehlgeschlagen (Exit-Code {rc}), Details: {work / 'benchmark.log'}")
    result = json.loads(out_path.read_text(encoding="utf-8"))
    out_path.unlink()
    result.update(at=datetime.now(UTC).astimezone().isoformat(timespec="seconds"), cores=cores,
                  model=job["model"], device=device, recording=folder.name if folder else "",
                  wall_s=round(time.monotonic() - started, 1))
    pw = power_state()
    result["power"] = pw.to_dict()
    if pw.warning():
        result["power_warning"] = pw.warning()
    if diarize is not None:
        clip = result["clip"]
        src = fa.mic if clip["track"] == "mic" else fa.remote
        info = sf.info(str(src))
        data, rate = sf.read(str(src), start=int(clip["start_s"] * info.samplerate),
                             stop=int((clip["start_s"] + clip["seconds"]) * info.samplerate), dtype="float32")
        wav = work / "clip.wav"
        sf.write(str(wav), data, rate, subtype="PCM_16")
        try:
            on_progress(-1.0, "misst die Sprechertrennung")
            full = effective_threads(cfg.diarization.num_threads, recording=False)
            s_full = diarize(wav, full)
            s_par = diarize(wav, DIAR_PARALLEL_THREADS) if full != DIAR_PARALLEL_THREADS else s_full
            result["diarization"] = {"threads_full": full, "rtf_full": round(s_full / clip["seconds"], 3),
                                     "threads_parallel": DIAR_PARALLEL_THREADS,
                                     "rtf_parallel": round(s_par / clip["seconds"], 3)}
        except Exception as exc:  # noqa: BLE001 - the transcription figures are still valid
            log.warning("Messung der Sprechertrennung fehlgeschlagen: %s", exc)
        finally:
            wav.unlink(missing_ok=True)
            # the speaker separation's result holds voice embeddings of the clip: nothing may stay
            import shutil

            shutil.rmtree(work / "diarize", ignore_errors=True)
    choice = choose(result, cores)
    if pw.slow or interference(None):
        # measured on battery / in energy-saver mode or while the PC got busy: shown, but not used
        # for the automatic layout (it would hold until the next measurement)
        result["not_applied"] = {**choice, "why": pw.warning() or interference(None)}
    else:
        result.update(choice)
    if save:
        tmp = result_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(result_file())
    return result


# -- app entry points (settings pages, CLI) ---------------------------------------------------------
BUSY_POLL_S = 2.0


def interference(own: Any) -> str:
    """What started during the measurement ("" = nothing): recording, call or another worker."""
    from callrec.asr.transcriber import _ACTIVE, _ACTIVE_LOCK
    from callrec.speakers.threads import busy_reason as call_reason

    reason = call_reason()
    if reason:
        return reason
    with _ACTIVE_LOCK:
        others = [p for p in _ACTIVE if p is not own]
    return "eine Verarbeitung hat begonnen" if own is not None and others else ""


def busy_reason() -> str:
    """Why measuring now would give wrong numbers ("" = fine)."""
    from callrec.asr.transcriber import _ACTIVE, _ACTIVE_LOCK
    from callrec.speakers.threads import busy_reason as call_reason

    reason = call_reason()
    if reason:
        return f"{reason} – bitte danach messen."
    with _ACTIVE_LOCK:
        running = bool(_ACTIVE)
    return "Gerade läuft eine Verarbeitung – bitte danach messen." if running else ""


def measure_now(cfg: Any, progress: Callable[[float, str], None] = lambda f, m: None) -> dict[str, Any]:
    reason = busy_reason()
    if reason:
        raise RuntimeError(reason)
    from callrec.factory import diarize_timer

    return run_benchmark(cfg, progress=progress, diarize=diarize_timer(cfg) if cfg.diarization.enabled else None)


def speed_overview(cfg: Any) -> dict[str, Any]:
    """What the settings pages show next to "Geschwindigkeit messen"."""
    from callrec.power import power_state
    from callrec.speakers.threads import asr_threads

    last = load_result()
    plan = asr_threads(cfg.transcription.cpu_threads, cfg.transcription.parallel_lanes, busy=False,
                       tuned=load_tuned())
    lines = report_lines(last) if last else ["Noch nicht gemessen. Die Messung dauert einige Minuten und nutzt einen "
                                             "Ausschnitt der neuesten Aufnahme (es wird nichts davon gespeichert)."]
    return {"plan": f"Whisper rechnet mit {plan.describe()}.", "lines": lines, "power": power_state().warning(),
            "measured": bool(last)}
