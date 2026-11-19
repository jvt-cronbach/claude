"""Build real components from the configuration (heavy imports happen lazily)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from callrec import paths
from callrec.calendar.service import CalendarService
from callrec.config import Config
from callrec.pipeline import Components, Pipeline
from callrec.speakers.profiles import ProfileStore, secure_persistence


def profile_store() -> ProfileStore:
    return ProfileStore(secure_persistence(paths.voice_profiles_file()))


def build_components(cfg: Config, profiles: ProfileStore | None = None) -> Components:
    models = paths.models_dir()

    def transcriber():  # type: ignore[no-untyped-def]
        from callrec.asr.transcriber import WhisperSubprocessTranscriber

        return WhisperSubprocessTranscriber(cfg.transcription, models / "whisper")

    def diarizer():  # type: ignore[no-untyped-def]
        from callrec.speakers.diarize import SherpaDiarizer
        from callrec.speakers.modelstore import SEGMENTATION, embedding_model, ensure_model

        d = cfg.diarization
        return SherpaDiarizer(
            ensure_model(SEGMENTATION, models),
            ensure_model(embedding_model(d.embedding_model), models),
            cluster_threshold=d.cluster_threshold,
            num_speakers=d.num_speakers,
            min_duration_on=d.min_duration_on,
            min_duration_off=d.min_duration_off,
            num_threads=d.num_threads,
        )

    def embedder():  # type: ignore[no-untyped-def]
        from callrec.speakers.diarize import SherpaEmbedder
        from callrec.speakers.modelstore import embedding_model, ensure_model

        m = embedding_model(cfg.diarization.embedding_model)
        return SherpaEmbedder(ensure_model(m, models), m.key, cfg.diarization.num_threads)

    def summarizer():  # type: ignore[no-untyped-def]
        from callrec.summary import OllamaSummarizer

        return OllamaSummarizer(cfg.summary)

    def loader(path: Path, rate: int):  # type: ignore[no-untyped-def]
        from callrec.audio.finalize import load_mono

        return load_mono(path, rate)

    return Components(
        transcriber_factory=transcriber,
        diarizer_factory=diarizer if cfg.diarization.enabled else None,
        embedder_factory=embedder if cfg.diarization.enabled else None,
        profiles=profiles or profile_store(),
        summarizer_factory=summarizer if cfg.summary.enabled else None,
        audio_loader=loader,
    )


def build_pipeline(cfg: Config, profiles: ProfileStore | None = None) -> Pipeline:
    return Pipeline(cfg, build_components(cfg, profiles))


def build_calendar_service(cfg: Config) -> CalendarService:
    source = None
    if cfg.calendar.client_id.strip():
        from callrec.calendar.graph import GraphAuth, GraphClient

        auth = GraphAuth(cfg.calendar.client_id.strip(), cfg.calendar.tenant_id.strip(), paths.token_cache_file())
        source = GraphClient(auth.token_silent, timeout=cfg.calendar.request_timeout_s)
    return CalendarService(
        source,
        paths.data_dir() / "calendar_cache.json",
        timedelta(minutes=cfg.calendar.tolerance_before_min),
        timedelta(minutes=cfg.calendar.tolerance_after_min),
    )


def graph_auth(cfg: Config):  # type: ignore[no-untyped-def]
    from callrec.calendar.graph import GraphAuth

    return GraphAuth(cfg.calendar.client_id.strip(), cfg.calendar.tenant_id.strip(), paths.token_cache_file())
