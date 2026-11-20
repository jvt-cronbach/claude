"""Post-processing pipeline: finalize -> transcribe -> diarize -> identify -> render -> summarize.

All heavy components are injected, so the orchestration is testable with fakes.
Each step persists its result in the recording folder, which makes every step
re-runnable on its own (re-transcribe, re-diarize, re-summarise, re-render after
speaker assignment).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from callrec.asr import quality
from callrec.audio.finalize import finalize_recording, find_audio, has_unfinished_parts
from callrec.config import Config
from callrec.consent import cloud_summary_covered
from callrec.models import (
    SELF_SPEAKER_ID,
    SOURCE_AUTO,
    SOURCE_SELF,
    SOURCE_SELF_ECHO,
    USER_DECIDED,
    AsrResult,
    Person,
    SpeakerInfo,
    SpeakerTurn,
)
from callrec.recording import Recording
from callrec.review import carry_over_assignments, load_turns
from callrec.speakers.assign import UNKNOWN, assign_remote, assign_single
from callrec.speakers.diarize import Diarizer, Embedder, speaker_embeddings
from callrec.speakers.profiles import ProfileStore
from callrec.summary import Summarizer, SummaryUnavailable, missing_sections, render_summary_md
from callrec.transcript import fmt_ts, merge_utterances, render_lines, render_markdown, speaker_lists
from callrec.userfiles import Rule, apply_replacements
from callrec.versions import adopt_legacy, fingerprint, latest, write_version

log = logging.getLogger(__name__)

IDENTIFY_RULE = "participants-v2"  # meta.json processing.identification.rule of this version
STEPS = ("finalize", "transcribe", "diarize", "identify", "render", "summarize")
ALL_STEPS = frozenset(STEPS)
# Named step sets, shared by the CLI, the tray and the main window (one source of truth).
STEP_PRESETS: dict[str, frozenset[str]] = {
    "all": ALL_STEPS,
    "retranscribe": frozenset({"transcribe", "identify", "render", "summarize"}),
    "rediarize": frozenset({"diarize", "identify", "render", "summarize"}),
    "resummarize": frozenset({"summarize"}),
    "render": frozenset({"render"}),
}


class ProcessingCancelled(RuntimeError):
    """The user cancelled a queued/running job; raised between two steps."""

    def __init__(self, message: str = "Verarbeitung abgebrochen") -> None:
        super().__init__(message)

ProgressFn = Callable[[str, float, str], None]  # (step, fraction 0..1, message)
AudioLoader = Callable[[Any, int], tuple[np.ndarray, int]]


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


@dataclass
class Components:
    transcriber_factory: Callable[[], Any]  # -> Transcriber (lazy: heavy)
    diarizer_factory: Callable[[], Diarizer] | None
    embedder_factory: Callable[[], Embedder] | None
    profiles: ProfileStore
    summarizer_factory: Callable[[], Summarizer] | None
    audio_loader: AudioLoader
    glossary: list[str] = field(default_factory=list)
    replacements: list[Rule] = field(default_factory=list)


@dataclass
class ProcessResult:
    steps_done: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    summary_skipped: str = ""
    summary_failed: bool = False  # an error, not a deliberate skip (the transcript is still fine)
    unassigned_speakers: int = 0
    language_note: str = ""
    transcript_warnings: list[str] = field(default_factory=list)


def is_cancellation(exc: BaseException) -> bool:
    from callrec.asr.transcriber import TranscriptionCancelled

    return isinstance(exc, ProcessingCancelled | TranscriptionCancelled)


def summary_retry_hint(rec: Recording) -> str:
    return ("Das Transkript ist fertig (transcript.md). Zusammenfassung später nachholen: im Hauptfenster "
            f"„Zusammenfassung neu erstellen“ oder callrec summarize \"{rec.folder}\"")


def _accepts(fn: Any, name: str) -> bool:
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def initial_prompt_for(participants: list[Person], self_person: Person) -> str:
    names = [p.name for p in [self_person, *participants] if p.name and "@" not in p.name]
    names = list(dict.fromkeys(names))
    if not names:
        return ""
    return "Gespräch mit " + ", ".join(names) + "."


def self_track_line(utterances: list[Any], speakers: dict[str, SpeakerInfo]) -> str:
    """ "Joost (Mikrofonspur): 12 Beiträge, 04:31 Sprechzeit" for the transcript header."""
    mine = [u for u in utterances if u.speaker == SELF_SPEAKER_ID]
    me = speakers.get(SELF_SPEAKER_ID)
    name = me.display if me is not None else "Ich"
    talk = sum(u.end - u.start for u in mine)
    return f"{name} (Mikrofonspur, nicht in der Sprecherzuordnung): {len(mine)} Beiträge, {fmt_ts(talk, 'mm:ss')} Sprechzeit"


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


class Pipeline:
    def __init__(self, cfg: Config, components: Components) -> None:
        self.cfg = cfg
        self.c = components
        self._language: str | None = None  # one-off override for run(): "de", "en", ... or "auto"
        self._should_cancel: Callable[[], bool] = lambda: False

    # -- settings snapshots (recorded in meta.json for before/after comparison) --
    def effective_prompt(self, rec: Recording) -> str:
        parts = []
        if self.cfg.transcription.initial_prompt.strip():
            parts.append(self.cfg.transcription.initial_prompt.strip())
        if self.cfg.transcription.use_participant_names_as_prompt:
            names = initial_prompt_for(rec.participants(), rec.self_person())
            if names:
                parts.append(names)
        return " ".join(parts)

    def hotwords(self) -> str:
        return ", ".join(self.c.glossary) if self.cfg.transcription.use_glossary else ""

    def asr_settings(self, rec: Recording) -> dict[str, Any]:
        import dataclasses

        return {
            **dataclasses.asdict(self.cfg.transcription),
            "effective_initial_prompt": self.effective_prompt(rec),
            "hotwords": self.hotwords(),
            "preset": self.cfg.active_preset,
        }

    def render_settings(self) -> dict[str, Any]:
        import dataclasses

        return {
            **dataclasses.asdict(self.cfg.transcript),
            "replacements": [r.source for r in self.c.replacements] if self.cfg.transcript.apply_replacements else [],
        }

    def diarization_settings(self) -> dict[str, Any]:
        import dataclasses

        return {"diarization": dataclasses.asdict(self.cfg.diarization), "speakers": dataclasses.asdict(self.cfg.speakers)}

    def run(
        self,
        rec: Recording,
        steps: Iterable[str] = ALL_STEPS,
        progress: ProgressFn = lambda s, f, m: None,
        language: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ProcessResult:
        """``language`` overrides transcription.language for this run ("auto" = detect again).

        ``should_cancel`` is checked before every step and, inside the transcription, before every
        worker process (language detection, each track); a running worker is stopped separately
        via ``cancel_running_workers``."""
        self._language = language or None
        self._should_cancel = should_cancel or (lambda: False)
        wanted = set(steps)
        unknown = wanted - ALL_STEPS
        if unknown:
            raise ValueError(f"Unbekannte Schritte: {', '.join(sorted(unknown))}")
        result = ProcessResult()
        rec.update_meta(["processing", "status"], "running")
        try:
            for step in STEPS:
                if step not in wanted:
                    continue
                if should_cancel is not None and should_cancel():
                    raise ProcessingCancelled()
                progress(step, 0.0, "")
                getattr(self, f"_step_{step}")(rec, result, lambda f, m="", s=step: progress(s, f, m))
                progress(step, 1.0, "")
                result.steps_done.append(step)
        except Exception as exc:
            rec.update_meta(["processing", "status"], "cancelled" if is_cancellation(exc) else f"error: {exc}")
            raise
        rec.update_meta(["processing", "status"], "done")
        rec.update_meta(["processing", "last_run"], _now_iso())
        return result

    # -- steps ---------------------------------------------------------
    def _step_finalize(self, rec: Recording, result: ProcessResult, progress: Callable[..., None]) -> None:
        if not has_unfinished_parts(rec.folder) and find_audio(rec.folder):
            return
        fa = finalize_recording(rec.folder, self.cfg.audio.format, self.cfg.audio.write_mix, self.cfg.audio.keep_wav)
        files = {"mic": fa.mic.name, "remote": fa.remote.name}
        if fa.mix:
            files["mix"] = fa.mix.name
        rec.update_meta(["audio"], {"format": self.cfg.audio.format, "files": files})
        meta = rec.load_meta()
        if (meta.get("recording") or {}).get("status") == "recording":
            # interrupted recording recovered after a crash
            import soundfile as sf

            rec.update_meta(["recording", "status"], "recovered")
            rec.update_meta(["recording", "duration_s"], float(sf.info(str(fa.mic)).duration))
            result.messages.append("Unterbrochene Aufnahme wurde wiederhergestellt.")

    def _audio(self, rec: Recording) -> Any:
        fa = find_audio(rec.folder)
        if fa is None:
            raise FileNotFoundError(f"Keine fertigen Audiodateien in {rec.folder} (Schritt 'finalize' ausführen)")
        return fa

    def _step_transcribe(self, rec: Recording, result: ProcessResult, progress: Callable[..., None]) -> None:
        fa = self._audio(rec)
        transcriber = self.c.transcriber_factory()
        prompt = self.effective_prompt(rec)
        hotwords = self.hotwords()
        old = (rec.load_meta().get("processing") or {}).get("transcription") or {}
        # keep the previous ASR result for before/after comparison
        archive = rec.data_dir / "archive"
        archived: dict[str, Any] = {}
        for track in ("mic", "remote"):
            p = rec.asr_path(track)
            if p.exists():
                archive.mkdir(parents=True, exist_ok=True)
                archived[track] = archive / f"asr_{track}.{old.get('run_id', 'alt')}.json"
                p.replace(archived[track])
        try:
            self._transcribe_tracks(rec, result, progress, fa, transcriber, prompt, hotwords)
        except BaseException:
            # cancelled or failed: put the previous result back, so the recording stays consistent with
            # meta.json (render, speaker review, rename and a later retry keep working on it)
            for track, src in archived.items():
                try:
                    if src.exists():
                        src.replace(rec.asr_path(track))
                except OSError:
                    log.warning("Vorherige Transkription %s nicht wiederhergestellt", track, exc_info=True)
            raise

    def _check_cancel(self) -> None:
        if self._should_cancel():
            raise ProcessingCancelled()

    def _transcribe_tracks(self, rec: Recording, result: ProcessResult, progress: Callable[..., None], fa: Any,
                           transcriber: Any, prompt: str, hotwords: str) -> None:
        run_id = _run_id()
        asr_meta: dict[str, Any] = {}
        selection = str(getattr(transcriber, "selection_note", "") or "")
        if selection:
            result.messages.append(selection)
            log.info("Transkription: %s", selection)
        self._check_cancel()
        decision = self._decide_language(rec, transcriber, {"mic": fa.mic, "remote": fa.remote}, progress)
        result.language_note = str(decision.get("reason") or "")
        log.info("Sprache %s: %s", rec.folder.name, result.language_note)
        result.messages.append(f"Sprache: {result.language_note}")
        tcfg = self.cfg.transcription
        for i, (track, path) in enumerate((("mic", fa.mic), ("remote", fa.remote))):

            def track_progress(f: float, msg: str = "", i: int = i, track: str = track) -> None:
                # f < 0: indeterminate phase (model download/loading) - report the message, not "0 %"
                label = f"Spur {track}: {msg}" if msg else f"Spur {track}"
                progress(-1.0 if f < 0 else (i + f) / 2, label)

            self._check_cancel()  # the worker of the previous track is done; do not start the next one
            lang = str((decision.get("tracks") or {}).get(track, {}).get("language") or "") or None
            kwargs: dict[str, Any] = {"language": lang} if _accepts(transcriber.transcribe, "language") else {}
            asr: AsrResult = transcriber.transcribe(
                path,
                rec.asr_path(track),
                initial_prompt=prompt,
                hotwords=hotwords,
                progress=track_progress,
                **kwargs,
            )
            rec.save_asr(track, asr)
            asr_meta[track] = {"model": asr.model, "device": asr.device, "compute_type": asr.compute_type, "language": asr.language}
            q = quality.check(asr.segments, tcfg.repetition_warn_min_repeats, tcfg.compression_ratio_threshold)
            asr_meta[track]["quality"] = q
            for r in q["repetitions"]:
                log.warning("Spur %s: mögliche Wiederholungsschleife bei %.0f s: %r x%d (%s)",
                            track, r["start"], r["phrase"], r["repeats"], rec.folder.name)
            stats = getattr(transcriber, "last_stats", None)
            if isinstance(stats, dict) and stats:
                asr_meta[track]["timing"] = dict(stats)
                if stats.get("rtf") is not None:
                    log.info("Transkription Spur %s: %.1f s Audio in %.1f s (Echtzeitfaktor %.2f, Modell %s, %s/%s, Laden %.1f s)",
                             track, stats.get("audio_s", 0.0), stats.get("transcribe_s", 0.0), stats["rtf"],
                             asr.model, asr.device, asr.compute_type, stats.get("load_s", 0.0))
        tmeta: dict[str, Any] = {**asr_meta, "run_id": run_id, "settings": self.asr_settings(rec),
                                 "language_detection": decision, "finished_at": _now_iso()}
        result.transcript_warnings = quality.warning_lines({t: asr_meta[t]["quality"] for t in ("mic", "remote")},
                                                           lambda x: fmt_ts(x))
        for w in result.transcript_warnings:
            result.messages.append(f"Warnung: {w}")
        if selection:
            tmeta["selection_note"] = selection
        rec.update_meta(["processing", "transcription"], tmeta)

    def _decide_language(self, rec: Recording, transcriber: Any, tracks: dict[str, Any],
                         progress: Callable[..., None]) -> dict[str, Any]:
        from callrec.asr import language as lang

        tcfg = self.cfg.transcription
        names = list(tracks)
        override = (self._language or "").strip().lower()
        if override and override != "auto":
            return lang.forced(override, names, "für diese Verarbeitung gewählt")
        if not override and tcfg.language != "auto":
            return lang.forced(tcfg.language, names, "fest eingestellt")
        candidates = list(tcfg.language_candidates)
        prior = candidates[0]
        detect = getattr(transcriber, "detect_languages", None)
        if detect is None:
            return {**lang.forced(prior, names, "Vorgabe – Spracherkennung nicht verfügbar"), "mode": "prior"}
        from callrec.asr.transcriber import TranscriptionCancelled

        # Detection is an optimisation: whatever goes wrong in it (worker, its output, the decision)
        # falls back to the prior language - only a user cancellation stops the transcription.
        try:
            raw = detect(tracks, rec.data_dir / "language_detect.json",
                         progress=lambda f, m="": progress(-1.0, m or "Sprache wird erkannt"))
            if not raw.get("multilingual_model", True):
                return lang.forced("en", names, "das gewählte Modell kann nur Englisch")
            windows = {t: list(((raw.get("tracks") or {}).get(t) or {}).get("windows") or []) for t in names}
            decision = lang.decide(windows, candidates, tcfg.language_switch_threshold, tcfg.language_min_speech_s)
            decision["detect_s"] = raw.get("detect_s")
            return decision
        except TranscriptionCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - see above
            log.warning("Spracherkennung fehlgeschlagen (%s: %s) - verwende %s", exc.__class__.__name__, exc, prior)
            return {**lang.forced(prior, names, "Vorgabe – Spracherkennung fehlgeschlagen"), "mode": "prior"}

    def _transcript_language(self, rec: Recording) -> str:
        """Detected/forced language of the call ("" if unknown, "de+en" if the tracks differ)."""
        tmeta = (rec.load_meta().get("processing") or {}).get("transcription") or {}
        langs = [str((tmeta.get(t) or {}).get("language") or "") for t in ("mic", "remote")]
        langs = [x for x in dict.fromkeys(langs) if x]
        return "+".join(langs)

    def _step_diarize(self, rec: Recording, result: ProcessResult, progress: Callable[..., None]) -> None:
        import json
        import time

        old_turns = load_turns(rec)
        old_speakers = rec.load_speakers()
        turns: list[SpeakerTurn] = []
        embeddings: dict[str, np.ndarray] = {}
        self_embedding: np.ndarray | None = None
        model_id = ""
        timing: dict[str, Any] = {}
        signal: dict[str, Any] = {}
        if self.cfg.diarization.enabled and self.c.diarizer_factory is not None:
            fa = self._audio(rec)
            t0 = time.monotonic()
            samples, sr = self.c.audio_loader(fa.remote, 16000)
            timing["audio_s"] = round(samples.size / sr, 1) if sr else 0.0
            if samples.size and float(np.max(np.abs(samples))) > 1e-4:
                t1 = time.monotonic()
                diarizer = self.c.diarizer_factory()
                timing["num_threads"] = getattr(diarizer, "num_threads", None)
                t2 = time.monotonic()
                turns = diarizer.diarize(samples, sr, lambda f: progress(0.8 * f, "Sprechertrennung"))
                t3 = time.monotonic()
                timing.update(load_s=round(t2 - t1, 2), diarize_s=round(t3 - t2, 2))
                embedder = None
                if self.c.embedder_factory is not None and turns:
                    embedder = self.c.embedder_factory()
                    model_id = embedder.model_id
                    progress(0.85, "Stimmmerkmale")
                    embeddings = speaker_embeddings(turns, samples, sr, embedder)
                timing["embed_s"] = round(time.monotonic() - t3, 2)
                t4 = time.monotonic()
                progress(0.92, "Mikrofonspur prüfen")
                self_embedding, signal = self._track_signals(rec, fa, samples, sr, embedder)
                timing["trackcheck_s"] = round(time.monotonic() - t4, 2)
            else:
                result.messages.append("Gegenseite ist stumm - keine Sprechertrennung.")
            del samples
            timing["total_s"] = round(time.monotonic() - t0, 2)
            if timing.get("audio_s"):
                timing["rtf"] = round(timing["total_s"] / timing["audio_s"], 3)
            log.info("Sprechertrennung %s: %s", rec.folder.name, timing)
        rec.data_dir.mkdir(parents=True, exist_ok=True)
        rec.turns_path.write_text(
            json.dumps([t.__dict__ for t in turns], ensure_ascii=False), encoding="utf-8"
        )
        rec.save_embeddings(embeddings, model_id, self_embedding)
        rec.save_trackcheck({"signal": signal} if signal else {})
        # Speaker ids of the earlier diarization are no longer valid; manual assignments are
        # carried over to the new clusters where the speaking times clearly overlap.
        carried = carry_over_assignments(old_turns, old_speakers, turns)
        if rec.speakers_path.exists():
            rec.speakers_path.unlink()
        # automatic contributions of the old clusters to voice profiles are no longer valid
        self.c.profiles.forget(rec.uid, sources=("auto",))
        if carried:
            rec.save_speakers(carried)
            result.messages.append(f"{len(carried)} manuelle Sprecherzuordnung(en) übernommen.")
        rec.update_meta(
            ["processing", "diarization"],
            {
                "backend": "sherpa-onnx" if turns else "none",
                "embedding_model": model_id,
                "speakers": len({t.speaker for t in turns}),
                "run_id": _run_id(),
                "settings": self.diarization_settings(),
                "timing": timing,
                "finished_at": _now_iso(),
            },
        )

    def _track_signals(self, rec: Recording, fa: Any, remote: np.ndarray | None, sr: int,
                       embedder: Embedder | None) -> tuple[np.ndarray | None, dict[str, Any]]:
        """Loudness-envelope correlation of both tracks and the embedding of the mic track.

        Computed once per diarization (both need the audio) and cached in the recording; the
        identification only reads them, so re-checking the assignment never touches the audio."""
        from callrec.speakers import trackcheck
        from callrec.speakers.diarize import embed_spans

        try:
            if remote is None:
                remote, sr = self.c.audio_loader(fa.remote, 16000)
            remote_env = trackcheck.envelope(remote, sr)
            mic, msr = self.c.audio_loader(fa.mic, 16000)
            corr, lag = trackcheck.envelope_correlation(trackcheck.envelope(mic, msr), remote_env)
            signal = {"envelope_corr": round(corr, 3), "lag_s": round(lag, 2)}
            emb = None
            if embedder is not None:
                spans = trackcheck.speech_spans(rec.load_asr("mic"))
                if spans:
                    emb = embed_spans(spans, mic, msr, embedder, max_total_s=60.0, label="Mikrofonspur")
            return emb, signal
        except Exception:  # noqa: BLE001 - a diagnostic must never fail the processing
            log.warning("Prüfung der Mikrofonspur fehlgeschlagen (%s)", rec.folder.name, exc_info=True)
            return None, {}

    # -- identification ------------------------------------------------------
    def self_name(self, rec: Recording) -> str:
        from callrec.selfname import display_name

        return display_name(self.cfg, rec.self_person())

    def compute_identification(self, rec: Recording) -> tuple[dict[str, SpeakerInfo], dict[str, Any], list[Any]]:
        """(speakers, identification meta, profile updates) - reads only, changes nothing.

        Rule ("participants", default): a remote speaker is assigned automatically only to a voice
        profile of a participant of THIS recording (calendar invitation, entered at the start, via
        "Teilnehmende bearbeiten"/"Termin zuordnen", or assigned by you). Without any participant
        nothing is assigned automatically. Assignments you made (source "manual"/"self_echo")
        are always kept."""
        from callrec.selfname import self_people
        from callrec.speakers import trackcheck
        from callrec.speakers.diarize import speech_seconds
        from callrec.speakers.names import persons_match

        turns = load_turns(rec)
        embeddings, model_id = rec.load_embeddings()
        old = rec.load_speakers()
        me = rec.self_person()
        sc = self.cfg.speakers
        speakers: dict[str, SpeakerInfo] = {
            SELF_SPEAKER_ID: SpeakerInfo(id=SELF_SPEAKER_ID, label="Ich", name=self.self_name(rec), email=me.email,
                                         source=SOURCE_SELF)
        }
        ids = list(dict.fromkeys(t.speaker for t in sorted(turns, key=lambda t: t.start)))
        for n, sid in enumerate(ids, 1):
            speakers[sid] = SpeakerInfo(id=sid, label=f"Sprecher {n}")
        for sid, info in old.items():
            if sid in speakers and sid != SELF_SPEAKER_ID and info.source in USER_DECIDED:
                info.label = speakers[sid].label
                info.clear_hints()
                speakers[sid] = info
        meta = rec.load_meta()
        report = trackcheck.check(rec.load_asr("mic"), rec.load_asr("remote"), turns, embeddings,
                                  rec.load_self_embedding(), rec.load_trackcheck().get("signal"), meta.get("devices"))
        speech = speech_seconds(turns)
        me_all = self_people(self.cfg, me)
        participants = [p for p in rec.participants() if not any(persons_match(p, x) for x in me_all)]
        strict = sc.match_scope == "participants"
        open_ids = [sid for sid in ids if speakers[sid].source not in USER_DECIDED]
        not_eligible: dict[str, str] = {}
        for sid in open_ids:
            if sid in report.self_suspects:
                speakers[sid].self_suspect = True
                not_eligible[sid] = "vermutlich Ihre eigene Stimme (auf der Spur der Gegenseite)"
            elif speech.get(sid, 0.0) < sc.min_match_speech_s:
                not_eligible[sid] = (f"zu wenig Sprache für eine automatische Erkennung ({speech.get(sid, 0.0):.0f} s, "
                                     f"mindestens {sc.min_match_speech_s:.0f} s)")
        to_match = {sid: embeddings[sid] for sid in open_ids if sid in embeddings}
        decisions: dict[str, Any] = {}
        if strict and not participants:
            for sid in open_ids:
                speakers[sid].note = ("keine Teilnehmerliste für diese Aufnahme – nichts automatisch zugeordnet "
                                      "(Teilnehmende eintragen oder Termin zuordnen)")
        elif to_match and model_id:
            decisions = self.c.profiles.decide(
                to_match, model_id, participants=participants if strict else None, threshold=sc.match_threshold,
                margin=sc.match_margin, exclude=me_all, not_eligible=not_eligible,
                bonus=0.0 if strict else sc.invitee_bonus, preferred=None if strict else participants,
                ignore_recording=rec.uid,
            )
        learn: list[Any] = []
        for sid in open_ids:
            info, d = speakers[sid], decisions.get(sid)
            if d is None:
                info.note = info.note or not_eligible.get(sid, "")
                continue
            if d.match is not None:
                m = d.match
                info.name, info.email, info.profile_id = m.profile.name, m.profile.email, m.profile.id
                info.score, info.source = round(m.similarity, 4), SOURCE_AUTO
                if (sc.auto_update_on_match and m.similarity >= sc.auto_update_threshold
                        and speech.get(sid, 0.0) >= sc.min_learn_speech_s):
                    learn.append((m.profile, sid, embeddings[sid], speech.get(sid, 0.0)))
            else:
                info.note = d.reason
                if d.suggestion is not None:
                    s = d.suggestion
                    info.suggested_name, info.suggested_email = s.profile.name, s.profile.email
                    info.suggested_score = round(s.similarity, 4)
        ident = {
            "rule": IDENTIFY_RULE,
            "scope": sc.match_scope,
            "participants": len(participants),
            "auto": sum(1 for s in speakers.values() if s.source == SOURCE_AUTO),
            "manual": sum(1 for s in speakers.values() if s.source in USER_DECIDED),
            "speech_s": speech,
            "decisions": {sid: {"match": d.match.profile.name if d.match else "", "reason": d.reason,
                                "candidates": d.candidates} for sid, d in decisions.items()},
            "trackcheck": report.to_dict(),
        }
        return speakers, ident, learn

    def _step_identify(self, rec: Recording, result: ProcessResult, progress: Callable[..., None]) -> None:
        import time

        t0 = time.monotonic()
        speakers, ident, learn = self.compute_identification(rec)
        rid = rec.uid
        # automatic contributions of an earlier run of this recording are replaced, never repeated
        self.c.profiles.forget(rid, sources=("auto",))
        _emb, model_id = rec.load_embeddings()
        for prof, sid, emb, secs in learn:
            self.c.profiles.learn(prof.name, prof.email, emb, model_id, recording=rid, speaker=sid, source="auto",
                                  seconds=secs, title=rec.title(), folder=rec.folder.name)
        rec.save_speakers(speakers)
        ident["timing"] = {"identify_s": round(time.monotonic() - t0, 3)}
        ident["at"] = _now_iso()
        rec.update_meta(["processing", "identification"], ident)
        for w in ident["trackcheck"]["warnings"]:
            result.messages.append(f"Hinweis: {w}")
            log.warning("%s: %s", rec.folder.name, w)
        result.unassigned_speakers = sum(1 for s in speakers.values() if not s.name)

    def preview_identification(self, rec: Recording) -> list[tuple[str, str, str]]:
        """ "Sprecherzuordnung neu prüfen": what would change, as (label, before, after); nothing is saved."""
        new, _ident, _learn = self.compute_identification(rec)
        old = rec.load_speakers()
        out: list[tuple[str, str, str]] = []
        for sid, info in new.items():
            if sid == SELF_SPEAKER_ID:
                continue
            before = old.get(sid)
            b = before.display if before is not None else info.label
            if b != info.display:
                out.append((info.label, b, info.display + (f" – {info.note}" if info.note and not info.name else "")))
        return out

    def _step_render(self, rec: Recording, result: ProcessResult, progress: Callable[..., None]) -> None:
        mic = rec.load_asr("mic")
        remote = rec.load_asr("remote")
        if mic is None or remote is None:
            raise FileNotFoundError("Transkription fehlt (Schritt 'transcribe' ausführen)")
        turns = load_turns(rec)
        speakers = rec.load_speakers()
        if not speakers:
            self._step_identify(rec, result, progress)
            speakers = rec.load_speakers()
        tc = self.cfg.transcript
        gap, max_len = tc.split_pause_s, tc.max_paragraph_s
        join_gap = tc.merge_max_gap_s if tc.merge_same_speaker else None
        self._apply_self_name(rec, speakers)
        mic_u = assign_single(mic, SELF_SPEAKER_ID, "mic", max_gap=gap, max_len=max_len)
        self_ids = {sid for sid, s in speakers.items() if s.source == SOURCE_SELF_ECHO}
        if turns:
            from callrec.speakers.assign import words_of

            remote_u = assign_remote(remote, turns, max_gap=gap, max_len=max_len, self_ids=self_ids,
                                     self_speaker=SELF_SPEAKER_ID, mic_words=words_of(mic) if self_ids else None)
        else:
            remote_u = assign_single(remote, "S1", "remote", max_gap=gap, max_len=max_len)
            speakers.setdefault("S1", SpeakerInfo(id="S1", label="Gegenseite"))
        if any(u.speaker == UNKNOWN for u in remote_u):
            speakers.setdefault(UNKNOWN, SpeakerInfo(id=UNKNOWN, label="Unbekannt"))
        rec.save_speakers(speakers)
        utterances = merge_utterances(mic_u, remote_u, join_gap=join_gap)
        if tc.apply_replacements and self.c.replacements:
            for u in utterances:
                u.text = apply_replacements(u.text, self.c.replacements)
        rec.save_utterances(utterances)
        meta = rec.load_meta()
        proc = meta.get("processing") or {}
        tmeta = proc.get("transcription") or {}
        dmeta = proc.get("diarization") or {}
        note = ""
        if tmeta.get("mic"):
            m = tmeta["mic"]
            note = f"faster-whisper {m.get('model', '')} ({m.get('device', '')}"
            note += f", {m['compute_type']})" if m.get("compute_type") and m.get("device") == "cpu" else ")"
            rtf = (m.get("timing") or {}).get("rtf")
            if rtf is not None:
                note += f", Echtzeitfaktor {rtf:.2f}"
            if tmeta.get("selection_note"):
                note += f" – {tmeta['selection_note']}"
        settings = {
            "transcription": tmeta.get("settings", {}),
            "diarization": dmeta.get("settings", {}),
            "rendering": self.render_settings(),
            "preset": self.cfg.active_preset,
        }
        fp = fingerprint({"asr": tmeta.get("run_id"), "diar": dmeta.get("run_id"), "render": settings["rendering"]})
        adopt_legacy(rec, "transcript")
        prev = latest(rec, "transcript")
        n = int(prev["version"]) + (0 if prev and prev.get("fingerprint") == fp else 1) if prev else 1
        preset = f", Preset {self.cfg.active_preset}" if self.cfg.active_preset else ""
        from callrec.asr.language import header_text, language_name

        lang_note = header_text(tmeta.get("language_detection") or {})
        if not lang_note and mic.language:
            lang_note = language_name(mic.language) if mic.language == remote.language else \
                f"Ich: {language_name(mic.language)}, Gegenseite: {language_name(remote.language)}"
        warnings = quality.warning_lines({t: (tmeta.get(t) or {}).get("quality") or {} for t in ("mic", "remote")},
                                         lambda x: fmt_ts(x))
        ident = proc.get("identification") or {}
        md = render_markdown(
            rec.title(), rec.started_at(), rec.duration() or mic.duration, rec.participants(), utterances, speakers, note,
            ts_format=tc.timestamp_format, join_gap=join_gap, version_note=f"v{n}{preset}",
            language_note=lang_note, warnings=warnings, self_line=self_track_line(utterances, speakers),
            capture_notes=list((ident.get("trackcheck") or {}).get("warnings") or []),
        )
        spoken, unassigned, _silent = speaker_lists(utterances, speakers)
        result.unassigned_speakers = len(unassigned)
        # for the recording list of the main window (cheap to read, no utterances needed)
        rec.update_meta(["processing", "speakers"], {"spoken": len(spoken), "unassigned": len(unassigned),
                                                     "at": _now_iso()})
        n2, new = write_version(rec, "transcript", md, fp, settings)
        if new and n2 > 1:
            result.messages.append(f"Neue Transkript-Version v{n2} erstellt (vorherige bleibt als transcript.v{n2 - 1}.md erhalten).")

    def _apply_self_name(self, rec: Recording, speakers: dict[str, SpeakerInfo]) -> None:
        """Your own contributions under the configured/derived name (transcript.self_display_name)."""
        name = self.self_name(rec)
        me = speakers.get(SELF_SPEAKER_ID)
        if me is None:
            speakers[SELF_SPEAKER_ID] = SpeakerInfo(id=SELF_SPEAKER_ID, label="Ich", name=name, source=SOURCE_SELF)
        else:
            me.name = name
        for s in speakers.values():
            if s.source == SOURCE_SELF_ECHO:
                s.name = name

    def _step_summarize(self, rec: Recording, result: ProcessResult, progress: Callable[..., None]) -> None:
        if not self.cfg.summary.enabled or self.c.summarizer_factory is None:
            result.summary_skipped = "Zusammenfassung ist deaktiviert."
            rec.update_meta(["processing", "summary"], {"skipped": result.summary_skipped})
            return
        utterances = rec.load_utterances()
        if not utterances:
            result.summary_skipped = "Kein Transkript vorhanden."
            rec.update_meta(["processing", "summary"], {"skipped": result.summary_skipped})
            return
        speakers = rec.load_speakers()
        self._apply_self_name(rec, speakers)  # the same name as in the transcript, also after a settings change
        tc = self.cfg.transcript
        lines = render_lines(utterances, speakers, "hh:mm:ss", tc.merge_max_gap_s if tc.merge_same_speaker else None)
        names = list(dict.fromkeys(s.display for s in speakers.values() if s.id != UNKNOWN))
        start = rec.started_at()
        date = f"{start:%d.%m.%Y %H:%M}" if start else ""
        provider_name = self.cfg.summary.provider
        if not cloud_summary_covered(rec.load_meta(), provider_name):
            result.summary_skipped = (
                "Die Einwilligung dieser Aufnahme erwähnt die Übertragung an Anthropic nicht - Zusammenfassung "
                "nicht erstellt. Nachholen mit Bestätigung: im Hauptfenster „Zusammenfassung neu erstellen“ oder "
                "'callrec summarize <Ordner> --confirm-anthropic'."
            )
            result.messages.append(result.summary_skipped)
            rec.update_meta(["processing", "summary"], {"skipped": result.summary_skipped, "at": _now_iso()})
            log.warning("Zusammenfassung übersprungen: Einwilligung deckt Anthropic nicht ab (%s)", rec.folder.name)
            return
        started = datetime.now(UTC)
        try:
            summarizer = self.c.summarizer_factory()
            if hasattr(summarizer, "progress"):
                summarizer.progress = lambda f, m="": progress(f, m)
            kwargs: dict[str, Any] = {}
            call_lang = self._transcript_language(rec)
            if call_lang and _accepts(summarizer.summarize, "transcript_language"):
                kwargs["transcript_language"] = call_lang
            body = summarizer.summarize(rec.title(), names, lines, date=date, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a failed summary never fails the recording
            unexpected = not isinstance(exc, SummaryUnavailable)
            waited = int((datetime.now(UTC) - started).total_seconds())
            reason = str(exc) if not unexpected else f"unerwarteter Fehler ({exc.__class__.__name__}: {exc})"
            result.summary_skipped = reason
            result.summary_failed = True
            hint = summary_retry_hint(rec)
            result.messages.append(f"Zusammenfassung nicht erstellt (nach {waited} s): {reason} {hint}")
            rec.update_meta(["processing", "summary"], {"skipped": reason, "status": "failed", "waited_s": waited,
                                                        "at": _now_iso()})
            if unexpected:
                log.exception("Zusammenfassung fehlgeschlagen (%s)", rec.folder.name)
            else:
                log.warning("Zusammenfassung übersprungen nach %d s: %s", waited, exc)
            return
        created = datetime.now().strftime("%d.%m.%Y %H:%M")
        describe = getattr(summarizer, "describe", None)
        llm = describe() if callable(describe) else {"model": summarizer.model_name}
        tv = latest(rec, "transcript")
        settings = {"llm": llm, "transcript_version": tv["version"] if tv else None}
        fp = fingerprint({**settings, "input": fingerprint(lines)})
        adopt_legacy(rec, "summary")
        missing = missing_sections(body)
        if missing:
            msg = "Zusammenfassung ohne erwartete Abschnitte: " + ", ".join(missing)
            log.warning("%s (%s)", msg, rec.folder.name)
            result.messages.append(msg)
        served = str(llm.get("served_model") or summarizer.model_name)
        n, _new = write_version(rec, "summary", render_summary_md(
            rec.title(), body, served, created,
            local=bool(getattr(getattr(summarizer, "provider", None), "is_local", True)),
            provider=str(llm.get("provider", "")),
        ), fp, settings)
        rec.update_meta(["processing", "summary"], {**llm, "version": n, "missing_sections": missing, "finished_at": _now_iso()})
        log.info("Zusammenfassung v%d erstellt (%s, Modell %s)", n, llm.get("provider", "?"), served)
