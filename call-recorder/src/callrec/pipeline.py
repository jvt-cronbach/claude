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

from callrec.audio.finalize import finalize_recording, find_audio, has_unfinished_parts
from callrec.config import Config
from callrec.models import SELF_SPEAKER_ID, AsrResult, Person, SpeakerInfo, SpeakerTurn
from callrec.recording import Recording, write_text_atomic
from callrec.review import load_turns
from callrec.speakers.assign import UNKNOWN, assign_remote, assign_single
from callrec.speakers.diarize import Diarizer, Embedder, speaker_embeddings
from callrec.speakers.profiles import ProfileStore
from callrec.summary import Summarizer, SummaryUnavailable, render_summary_md
from callrec.transcript import merge_utterances, render_lines, render_markdown

log = logging.getLogger(__name__)

STEPS = ("finalize", "transcribe", "diarize", "identify", "render", "summarize")
ALL_STEPS = frozenset(STEPS)

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


@dataclass
class ProcessResult:
    steps_done: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    summary_skipped: str = ""
    unassigned_speakers: int = 0


def initial_prompt_for(participants: list[Person], self_person: Person) -> str:
    names = [p.name for p in [self_person, *participants] if p.name and "@" not in p.name]
    names = list(dict.fromkeys(names))
    if not names:
        return ""
    return "Gespräch mit " + ", ".join(names) + "."


class Pipeline:
    def __init__(self, cfg: Config, components: Components) -> None:
        self.cfg = cfg
        self.c = components

    def run(
        self,
        rec: Recording,
        steps: Iterable[str] = ALL_STEPS,
        progress: ProgressFn = lambda s, f, m: None,
    ) -> ProcessResult:
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
                progress(step, 0.0, "")
                getattr(self, f"_step_{step}")(rec, result, lambda f, m="", s=step: progress(s, f, m))
                progress(step, 1.0, "")
                result.steps_done.append(step)
        except Exception as exc:
            rec.update_meta(["processing", "status"], f"error: {exc}")
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
        prompt = ""
        if self.cfg.transcription.use_participant_names_as_prompt:
            prompt = initial_prompt_for(rec.participants(), rec.self_person())
        asr_meta: dict[str, Any] = {}
        for i, (track, path) in enumerate((("mic", fa.mic), ("remote", fa.remote))):
            asr: AsrResult = transcriber.transcribe(
                path,
                rec.asr_path(track),
                initial_prompt=prompt,
                progress=lambda f, i=i, track=track: progress((i + f) / 2, f"Spur {track}"),
            )
            rec.save_asr(track, asr)
            asr_meta[track] = {"model": asr.model, "device": asr.device, "compute_type": asr.compute_type, "language": asr.language}
        rec.update_meta(["processing", "transcription"], {**asr_meta, "finished_at": _now_iso()})

    def _step_diarize(self, rec: Recording, result: ProcessResult, progress: Callable[..., None]) -> None:
        import json

        turns: list[SpeakerTurn] = []
        embeddings: dict[str, np.ndarray] = {}
        model_id = ""
        if self.cfg.diarization.enabled and self.c.diarizer_factory is not None:
            fa = self._audio(rec)
            samples, sr = self.c.audio_loader(fa.remote, 16000)
            if samples.size and float(np.max(np.abs(samples))) > 1e-4:
                diarizer = self.c.diarizer_factory()
                turns = diarizer.diarize(samples, sr, lambda f: progress(0.8 * f, "Sprechertrennung"))
                if self.c.embedder_factory is not None and turns:
                    embedder = self.c.embedder_factory()
                    model_id = embedder.model_id
                    embeddings = speaker_embeddings(turns, samples, sr, embedder)
            else:
                result.messages.append("Gegenseite ist stumm - keine Sprechertrennung.")
        rec.data_dir.mkdir(parents=True, exist_ok=True)
        rec.turns_path.write_text(
            json.dumps([t.__dict__ for t in turns], ensure_ascii=False), encoding="utf-8"
        )
        rec.save_embeddings(embeddings, model_id)
        # speaker ids of an earlier diarization (and assignments to them) are no longer valid
        if rec.speakers_path.exists():
            rec.speakers_path.unlink()
        rec.update_meta(
            ["processing", "diarization"],
            {
                "backend": "sherpa-onnx" if turns else "none",
                "embedding_model": model_id,
                "speakers": len({t.speaker for t in turns}),
                "finished_at": _now_iso(),
            },
        )

    def _step_identify(self, rec: Recording, result: ProcessResult, progress: Callable[..., None]) -> None:
        turns = load_turns(rec)
        embeddings, model_id = rec.load_embeddings()
        old = rec.load_speakers()
        me = rec.self_person()
        speakers: dict[str, SpeakerInfo] = {
            SELF_SPEAKER_ID: SpeakerInfo(id=SELF_SPEAKER_ID, label="Ich", name=me.name or "Ich", email=me.email, source="self")
        }
        ids = list(dict.fromkeys(t.speaker for t in sorted(turns, key=lambda t: t.start)))
        for n, sid in enumerate(ids, 1):
            speakers[sid] = SpeakerInfo(id=sid, label=f"Sprecher {n}")
        # keep manual assignments from an earlier review if the diarization is unchanged
        for sid, info in old.items():
            if sid in speakers and info.source == "manual":
                speakers[sid] = info
        to_match = {sid: e for sid, e in embeddings.items() if sid in speakers and speakers[sid].source != "manual"}
        if to_match and model_id:
            sc = self.cfg.speakers
            matches = self.c.profiles.match(
                to_match, model_id, rec.participants(), sc.match_threshold, sc.invitee_bonus, exclude=[me]
            )
            for sid, m in matches.items():
                info = speakers[sid]
                info.name, info.email, info.profile_id = m.profile.name, m.profile.email, m.profile.id
                info.score, info.source = round(m.similarity, 4), "auto"
                if sc.auto_update_on_match and m.similarity >= sc.auto_update_threshold:
                    self.c.profiles.add_sample(m.profile.name, m.profile.email, embeddings[sid], model_id)
        rec.save_speakers(speakers)
        result.unassigned_speakers = sum(1 for s in speakers.values() if not s.name)

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
        mic_u = assign_single(mic, SELF_SPEAKER_ID, "mic")
        if turns:
            remote_u = assign_remote(remote, turns)
        else:
            remote_u = assign_single(remote, "S1", "remote")
            speakers.setdefault("S1", SpeakerInfo(id="S1", label="Gegenseite"))
        if any(u.speaker == UNKNOWN for u in remote_u):
            speakers.setdefault(UNKNOWN, SpeakerInfo(id=UNKNOWN, label="Unbekannt"))
        rec.save_speakers(speakers)
        utterances = merge_utterances(mic_u, remote_u)
        rec.save_utterances(utterances)
        meta = rec.load_meta()
        tmeta = (meta.get("processing") or {}).get("transcription") or {}
        note = ""
        if tmeta.get("mic"):
            note = f"faster-whisper {tmeta['mic'].get('model', '')} ({tmeta['mic'].get('device', '')})"
        md = render_markdown(rec.title(), rec.started_at(), rec.duration() or mic.duration, rec.participants(), utterances, speakers, note)
        write_text_atomic(rec.transcript_path, md)

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
        lines = render_lines(utterances, speakers)
        names = list(dict.fromkeys(s.display for s in speakers.values() if s.id != UNKNOWN))
        try:
            summarizer = self.c.summarizer_factory()
            body = summarizer.summarize(rec.title(), names, lines)
        except SummaryUnavailable as exc:
            result.summary_skipped = str(exc)
            result.messages.append(f"Zusammenfassung übersprungen: {exc}")
            rec.update_meta(["processing", "summary"], {"skipped": str(exc), "at": _now_iso()})
            log.warning("Zusammenfassung übersprungen: %s", exc)
            return
        created = datetime.now().strftime("%d.%m.%Y %H:%M")
        write_text_atomic(rec.summary_path, render_summary_md(rec.title(), body, summarizer.model_name, created))
        rec.update_meta(["processing", "summary"], {"model": summarizer.model_name, "finished_at": _now_iso()})
